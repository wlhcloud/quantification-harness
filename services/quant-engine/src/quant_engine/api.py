from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from .jobs import JobManager
from .models import JobCreate, JobRecord
from .settings import settings


def create_router(manager: JobManager, check_backtest: Callable[[str, str | None, str | None], dict[str, Any]]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, object]:
        # authEnabled/authSource 供编排侧断言：进程若未加载 .quant.env（例如计划任务用了内联命令），
        # QUANT_ENGINE_API_KEY 为空会让鉴权静默失效——看门狗据此重启服务。
        # authSource 说明 key 到底来自哪个环境变量（QUANT_API_KEY 为收敛后的权威变量）。
        return {"status": "ok", "service": "dsh-quant-engine", "version": "0.1.0",
                "authEnabled": bool(settings.api_key), "authSource": settings.api_key_source}

    @router.post("/jobs", response_model=JobRecord, status_code=status.HTTP_202_ACCEPTED)
    def create_job(request: JobCreate) -> JobRecord:
        if request.type.value == "backtest":
            gate = check_backtest(str(request.parameters.get("kind", "monthly")),
                                  str(request.parameters.get("startDate")) if request.parameters.get("startDate") else None,
                                  str(request.parameters.get("endDate")) if request.parameters.get("endDate") else None)
            forced = bool(request.parameters.get("forceDataGate", False))
            force_reason = str(request.parameters.get("forceReason", "")).strip()
            # hardBlocked 的准入（如"功能已停用"）不允许带风险运行绕过
            if not gate["allowed"] and (gate.get("hardBlocked") or not forced):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"message": "回测数据准入未通过", "gate": gate})
            if forced and len(force_reason) < 10:
                # 原有契约：只要显式带风险运行，就必须给出不少于 10 字的原因
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="带风险运行必须填写至少 10 个字符的风险原因")
            # Persist the exact decision with the job: this is the audit trail for a forced run.
            request.parameters["dataGate"] = gate
            request.parameters["forcedWithRisk"] = forced and not gate["allowed"]
            if forced:
                request.parameters["riskAudit"] = {"reason": force_reason, "recordedAt": datetime.now(timezone.utc).isoformat()}
        try:
            return manager.submit(request)
        except ValueError as error:
            raise HTTPException(status_code=501, detail=str(error)) from error

    @router.get("/backtest/readiness")
    def backtest_readiness(kind: str = Query(default="monthly", pattern="^(monthly|short)$"),
                           start_date: str | None = Query(default=None, pattern="^\\d{8}$"),
                           end_date: str | None = Query(default=None, pattern="^\\d{8}$")) -> dict[str, Any]:
        return check_backtest(kind, start_date, end_date)

    @router.get("/jobs", response_model=list[JobRecord])
    def list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> list[JobRecord]:
        return manager.repository.list(limit)

    @router.get("/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        job = manager.repository.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.post("/jobs/{job_id}/cancel", response_model=JobRecord)
    def cancel_job(job_id: str) -> JobRecord:
        job = manager.cancel(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    return router
