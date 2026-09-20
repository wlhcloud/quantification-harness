from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from .models import JobCreate, JobRecord, JobStatus, JobType, utc_now
from .repository import JobRepository

JobHandler = Callable[[dict[str, Any], Callable[[float], None], Callable[[], bool]], dict[str, Any]]

# 每类 job 的超时（秒）。Python 无法强杀线程，因此超时以「协作取消 + 结束后标记 error」落地：
# 处理器轮询 cancelled() 时会拿到 True 并尽快退出；若它整段跑完才返回，则按超时记 error
# 且结果不采用（避免超时结果被当成有效结果写回）。
DEFAULT_JOB_TIMEOUTS: dict[JobType, float] = {
    JobType.FEATURES: 30 * 60,
    JobType.TRAINING: 60 * 60,
    JobType.BACKTEST: 60 * 60,
    JobType.INFERENCE: 30 * 60,
    JobType.ETF_FACTORS: 30 * 60,
    JobType.ETF_RESEARCH: 30 * 60,
    JobType.ETF_TRAIN: 60 * 60,
    JobType.ETF_BACKTEST: 30 * 60,
    JobType.ETF_WALKFORWARD: 90 * 60,
    JobType.STOCK_ML_FACTORS: 60 * 60,
    JobType.STOCK_INDUSTRY_FACTORS: 30 * 60,
    JobType.STOCK_MONEY_FLOW_FACTORS: 30 * 60,
    JobType.STOCK_WALKFORWARD: 2 * 60 * 60,
    JobType.STOCK_ML_BACKTEST: 60 * 60,
    JobType.STOCK_ML_PREDICT: 30 * 60,
}

# 共享资源互斥：同资源的 job 必须串行执行。
# 原实现只有 worker 数限制，例如 stock_ml_factors 会整表 DELETE/重建 factors.db，
# 而 stock_walkforward 同时读同一张因子表 → 可能读到半成品；ETF 侧同理。
DEFAULT_JOB_RESOURCES: dict[JobType, str] = {
    JobType.FEATURES: "features",
    JobType.STOCK_ML_FACTORS: "factors.db",
    JobType.STOCK_INDUSTRY_FACTORS: "factors.db",
    JobType.STOCK_MONEY_FLOW_FACTORS: "factors.db",
    JobType.STOCK_WALKFORWARD: "factors.db",
    # 两者都读全量因子表；predict 还会写回 selection_candidates → 与因子重建/训练互斥
    JobType.STOCK_ML_BACKTEST: "factors.db",
    JobType.STOCK_ML_PREDICT: "factors.db",
    JobType.TRAINING: "artifacts",
    JobType.INFERENCE: "artifacts",
    JobType.BACKTEST: "backtest.db",
    JobType.ETF_FACTORS: "etf-quant.db",
    JobType.ETF_RESEARCH: "etf-quant.db",
    JobType.ETF_TRAIN: "etf-quant.db",
    JobType.ETF_BACKTEST: "etf-quant.db",
    JobType.ETF_WALKFORWARD: "etf-quant.db",
}


class JobManager:
    def __init__(self, repository: JobRepository, max_workers: int = 2,
                 timeouts: dict[JobType, float] | None = None,
                 progress_interval: float = 1.0,
                 cancel_check_interval: float = 1.0,
                 resources: dict[JobType, str] | None = None,
                 resource_wait_timeout: float = 60 * 60):
        self.repository = repository
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="quant-job")
        self.handlers: dict[JobType, JobHandler] = {}
        self.futures: dict[str, Future[None]] = {}
        self.lock = Lock()
        self.timeouts: dict[JobType, float] = {**DEFAULT_JOB_TIMEOUTS, **(timeouts or {})}
        self.progress_interval = progress_interval
        self.cancel_check_interval = cancel_check_interval
        self.resource_of: dict[JobType, str] = {**DEFAULT_JOB_RESOURCES, **(resources or {})}
        self.resource_wait_timeout = resource_wait_timeout
        self._resource_locks: dict[str, threading.Lock] = {}
        self._resource_guard = Lock()

    def _resource_lock(self, job_type: JobType) -> threading.Lock | None:
        """取该 job 类型对应的共享资源锁（无名资源的 job 返回 None，不互斥）。"""
        name = self.resource_of.get(job_type)
        if not name:
            return None
        with self._resource_guard:
            return self._resource_locks.setdefault(name, threading.Lock())

    def register(self, job_type: JobType, handler: JobHandler) -> None:
        self.handlers[job_type] = handler

    def submit(self, request: JobCreate) -> JobRecord:
        if request.type not in self.handlers:
            raise ValueError(f"job type is not implemented: {request.type.value}")
        job = JobRecord(
            id=f"{request.type.value}-{uuid4()}", type=request.type, status=JobStatus.QUEUED,
            parameters=request.parameters, created_at=utc_now(),
        )
        self.repository.insert(job)
        future = self.executor.submit(self._run, job.id, request.type, request.parameters)
        with self.lock:
            self.futures[job.id] = future
        return job

    def _run(self, job_id: str, job_type: JobType, parameters: dict[str, Any]) -> None:
        """生命周期：占用共享资源 → 执行 → 释放。

        同资源（如 factors.db / etf-quant.db）的 job 串行执行：原实现只有 worker 数限制，
        整表重建与 walkforward 读取并发时可能读到半成品。等待不占用进 job 超时额度。
        """
        timeout = self.timeouts.get(job_type)
        resource = self.resource_of.get(job_type)
        resource_lock = self._resource_lock(job_type)
        acquired = False
        self.repository.update(job_id, status=JobStatus.RUNNING, started_at=utc_now().isoformat())
        try:
            if resource_lock is not None:
                acquired = resource_lock.acquire(timeout=self.resource_wait_timeout)
                if not acquired:
                    self.repository.update(
                        job_id, status=JobStatus.ERROR,
                        error=f"等待同资源任务超时（{self.resource_wait_timeout:.0f}s，resource={resource}）",
                        finished_at=utc_now().isoformat())
                    return
            self._execute(job_id, job_type, parameters, timeout)
        finally:
            if acquired and resource_lock is not None:
                resource_lock.release()
            with self.lock:
                self.futures.pop(job_id, None)

    def _execute(self, job_id: str, job_type: JobType, parameters: dict[str, Any],
                 timeout: float | None) -> None:
        # 超时从真正开始执行（已拿到资源）起算
        deadline = (time.monotonic() + timeout) if timeout else None
        state: dict[str, Any] = {"progress": 0.0, "written": -1.0, "written_at": 0.0,
                                 "cancel": False, "checked_at": 0.0, "timed_out": False}

        def progress(value: float) -> None:
            value = max(0.0, min(100.0, float(value)))
            state["progress"] = value
            now = time.monotonic()
            # 节流：原实现每次 progress() 都新开连接 + commit，一次训练/回测可产生数千次写事务
            if value - state["written"] >= 1.0 or value >= 100.0 or now - state["written_at"] >= self.progress_interval:
                state["written"] = value
                state["written_at"] = now
                self.repository.update(job_id, progress=value)

        def cancelled() -> bool:
            """超时或用户取消都通过这里返回 True，处理器应按协作式尽快退出。"""
            now = time.monotonic()
            if deadline is not None and now > deadline:
                state["timed_out"] = True
                return True
            if state["cancel"]:
                return True
            # 取消标记按 cancel_check_interval 缓存，避免紧凑循环把 SQLite 打满
            if now - state["checked_at"] >= self.cancel_check_interval:
                state["checked_at"] = now
                state["cancel"] = self.repository.cancel_requested(job_id)
            return bool(state["cancel"])

        try:
            result = self.handlers[job_type](parameters, progress, cancelled)
            # 超时判定不能只依赖处理器轮询 cancelled()：处理器整段跑完才返回时也要按超时处理
            overshot = deadline is not None and time.monotonic() > deadline
            latest = self.repository.get(job_id)
            if latest and latest.cancel_requested:
                status, error = JobStatus.CANCELLED, None
            elif state["timed_out"] or overshot:
                status = JobStatus.ERROR
                error = f"job 超时（>{timeout:.0f}s）：结果未采用"
            else:
                status, error = JobStatus.COMPLETE, None
            final_progress = 100 if status == JobStatus.COMPLETE else float(state["progress"])
            self.repository.update(
                job_id, status=status, progress=final_progress, error=error,
                result_json=json.dumps(result, ensure_ascii=False), finished_at=utc_now().isoformat(),
            )
        except Exception as error:
            overshot = deadline is not None and time.monotonic() > deadline
            latest = self.repository.get(job_id)
            if latest and latest.cancel_requested:
                status, message = JobStatus.CANCELLED, None
            elif state["timed_out"] or overshot:
                status, message = JobStatus.ERROR, f"job 超时（>{timeout:.0f}s）：{error}"
            else:
                status, message = JobStatus.ERROR, str(error)
            self.repository.update(job_id, status=status, error=message,
                                   finished_at=utc_now().isoformat())

    def cancel(self, job_id: str) -> JobRecord | None:
        job = self.repository.get(job_id)
        if not job or job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return job
        self.repository.update(job_id, cancel_requested=1)
        with self.lock:
            future = self.futures.get(job_id)
        if future and future.cancel():
            self.repository.update(job_id, status=JobStatus.CANCELLED, finished_at=utc_now().isoformat())
        return self.repository.get(job_id)
