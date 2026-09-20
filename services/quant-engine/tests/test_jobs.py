"""JobManager 治理测试：超时、进度写库节流、取消轮询缓存。

修复背景（job 治理，此前 jobs.py 零测试覆盖）：
  1. 无任何超时——长任务卡住时永远停在 running；
  2. progress() 每次都新开连接 + commit，一次训练/回测可产生数千次写事务；
  3. cancelled() 每次都 get() 整条记录，紧凑循环会把 SQLite 打满。
现在：超时以「协作取消 + 结束后标记 error 且结果不采用」落地；
progress 按百分点/时间间隔节流；取消标记按间隔缓存（只读单列）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from quant_engine.jobs import JobManager
from quant_engine.models import JobCreate, JobStatus, JobType
from quant_engine.repository import JobRepository


class CountingRepository(JobRepository):
    """统计落库与取消轮询次数，并可模拟外部取消请求。"""

    def __init__(self, path: Path):
        super().__init__(path)
        self.updates = 0
        self.cancel_reads = 0
        self.cancel_flag = False

    def update(self, job_id: str, **values: object) -> None:
        self.updates += 1
        return super().update(job_id, **values)

    def cancel_requested(self, job_id: str) -> bool:
        self.cancel_reads += 1
        return self.cancel_flag or super().cancel_requested(job_id)


def _wait_terminal(repo: JobRepository, job_id: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = repo.get(job_id)
        if job and job.status in {JobStatus.COMPLETE, JobStatus.ERROR, JobStatus.CANCELLED}:
            return job
        time.sleep(0.02)
    raise AssertionError("job 未在预期时间内结束")


class JobManagerTest(unittest.TestCase):
    def _manager(self, **kwargs) -> tuple[JobManager, CountingRepository]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = CountingRepository(Path(folder.name) / "quant-engine.db")
        return JobManager(repo, max_workers=1, **kwargs), repo

    def test_complete_sets_progress_100_and_persists_result(self):
        manager, repo = self._manager()
        manager.register(JobType.FEATURES, lambda p, progress, cancelled: {"rows": 7})
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        done = _wait_terminal(repo, job.id)
        self.assertEqual(done.status, JobStatus.COMPLETE)
        self.assertEqual(done.progress, 100)
        self.assertEqual(done.result, {"rows": 7})
        self.assertIsNone(done.error)

    def test_progress_writes_are_throttled(self):
        manager, repo = self._manager(progress_interval=60.0)

        def handler(_params, progress, _cancelled):
            for i in range(5000):
                progress(min(99.0, i * 0.001))  # 5000 次调用、约 99 次有效百分点变化
            return {"ok": True}

        manager.register(JobType.FEATURES, handler)
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        done = _wait_terminal(repo, job.id)
        self.assertEqual(done.status, JobStatus.COMPLETE)
        # 无节流时至少 5000 次写库；节流后应约为百分点数量级
        self.assertLess(repo.updates, 200, f"进度未节流：{repo.updates} 次写库")

    def test_cancel_polling_is_cached(self):
        manager, repo = self._manager(cancel_check_interval=60.0)

        def handler(_params, _progress, cancelled):
            hits = 0
            for _ in range(3000):
                if cancelled():
                    hits += 1
            return {"hits": hits}

        manager.register(JobType.FEATURES, handler)
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        done = _wait_terminal(repo, job.id)
        self.assertEqual(done.status, JobStatus.COMPLETE)
        self.assertEqual(done.result, {"hits": 0})
        self.assertLess(repo.cancel_reads, 10, f"取消轮询未缓存：{repo.cancel_reads} 次查询")

    def test_timeout_stops_cooperative_handler_and_marks_error(self):
        manager, repo = self._manager(timeouts={JobType.FEATURES: 0.2},
                                      cancel_check_interval=0.01)
        started = time.time()

        def handler(_params, _progress, cancelled):
            while not cancelled():  # 协作式循环：超时后 cancelled() 返回 True
                time.sleep(0.005)
            return {"stoppedAfterSec": round(time.time() - started, 3)}

        manager.register(JobType.FEATURES, handler)
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        done = _wait_terminal(repo, job.id)
        elapsed = time.time() - started
        self.assertEqual(done.status, JobStatus.ERROR)
        self.assertIn("超时", done.error or "")
        self.assertLess(elapsed, 5.0, "超时未能让处理器及时退出")
        self.assertLess(done.progress, 100)  # 超时不写 100，避免被当成完成

    def test_external_cancel_marks_cancelled(self):
        manager, repo = self._manager(cancel_check_interval=0.01)

        def handler(_params, _progress, cancelled):
            while not cancelled():
                time.sleep(0.005)
            return {"stopped": True}

        manager.register(JobType.FEATURES, handler)
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        time.sleep(0.15)
        manager.cancel(job.id)  # 走真实取消入口：写库 cancel_requested=1
        done = _wait_terminal(repo, job.id)
        self.assertEqual(done.status, JobStatus.CANCELLED)
        self.assertIsNone(done.error)

    def test_timeout_result_is_not_adopted(self):
        """处理器不理会 cancelled() 时，超时结果不得被当作有效结果。"""
        manager, repo = self._manager(timeouts={JobType.FEATURES: 0.1},
                                      cancel_check_interval=0.01)

        def handler(_params, _progress, _cancelled):
            time.sleep(0.25)  # 完全不听取消
            return {"suspicious": True}

        manager.register(JobType.FEATURES, handler)
        job = manager.submit(JobCreate(type=JobType.FEATURES))
        done = _wait_terminal(repo, job.id)
        self.assertEqual(done.status, JobStatus.ERROR)
        self.assertIn("超时", done.error or "")


class JobResourceLockTest(unittest.TestCase):
    """同资源 job 必须串行：原实现只有 worker 数限制，整表重建可与读取并发。"""

    def _manager(self, **kwargs) -> tuple[JobManager, CountingRepository]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = CountingRepository(Path(folder.name) / "quant-engine.db")
        return JobManager(repo, max_workers=2, **kwargs), repo

    @staticmethod
    def _overlap_tracker() -> tuple[dict, Callable[[str], None]]:
        import threading
        state = {"active": 0, "max_active": 0, "order": []}
        guard = threading.Lock()

        def track(tag: str) -> None:
            with guard:
                state["order"].append(tag)
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.25)  # 拉长临界区，确保并发时一定被观测到
            with guard:
                state["active"] -= 1

        return state, track

    def test_same_resource_jobs_are_serialized(self):
        manager, repo = self._manager()
        state, track = self._overlap_tracker()
        manager.register(JobType.STOCK_ML_FACTORS, lambda p, progress, cancelled: (track("rebuild"), {"ok": 1})[1])
        manager.register(JobType.STOCK_WALKFORWARD, lambda p, progress, cancelled: (track("walkforward"), {"ok": 2})[1])
        # 两者都在 factors.db 上（DEFAULT_JOB_RESOURCES）
        first = manager.submit(JobCreate(type=JobType.STOCK_ML_FACTORS))
        second = manager.submit(JobCreate(type=JobType.STOCK_WALKFORWARD))
        _wait_terminal(repo, first.id)
        _wait_terminal(repo, second.id)
        self.assertEqual(state["max_active"], 1, f"同资源 job 并发执行了：{state['order']}")

    def test_different_resources_run_concurrently(self):
        manager, repo = self._manager()
        state, track = self._overlap_tracker()
        # factor_mining 已随 legacy 因子选股退役（JobType 里不再有它），
        # 这里用 FEATURES（资源 features）与 ETF_RESEARCH（资源 etf-quant.db）验证"不同资源可并行"。
        manager.register(JobType.FEATURES, lambda p, progress, cancelled: (track("features"), {"ok": 1})[1])
        manager.register(JobType.ETF_RESEARCH, lambda p, progress, cancelled: (track("etf"), {"ok": 2})[1])
        first = manager.submit(JobCreate(type=JobType.FEATURES))
        second = manager.submit(JobCreate(type=JobType.ETF_RESEARCH))
        _wait_terminal(repo, first.id)
        _wait_terminal(repo, second.id)
        self.assertEqual(state["max_active"], 2, f"不同资源本应并行：{state['order']}")

    def test_resource_wait_timeout_is_reported(self):
        manager, repo = self._manager(resource_wait_timeout=0.2)
        manager.register(JobType.STOCK_ML_FACTORS,
                         lambda p, progress, cancelled: (time.sleep(2.0), {"ok": 1})[1])
        manager.register(JobType.STOCK_WALKFORWARD,
                         lambda p, progress, cancelled: {"ok": 2})
        holder = manager.submit(JobCreate(type=JobType.STOCK_ML_FACTORS))
        time.sleep(0.1)  # 确保 holder 先拿到资源
        waiter = manager.submit(JobCreate(type=JobType.STOCK_WALKFORWARD))
        done = _wait_terminal(repo, waiter.id)
        self.assertEqual(done.status, JobStatus.ERROR)
        self.assertIn("等待同资源任务超时", done.error or "")
        _wait_terminal(repo, holder.id, timeout=10)


if __name__ == "__main__":
    unittest.main()
