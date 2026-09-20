from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    FEATURES = "features"
    TRAINING = "training"
    BACKTEST = "backtest"
    INFERENCE = "inference"
    ETF_FACTORS = "etf_factors"
    ETF_RESEARCH = "etf_research"
    ETF_TRAIN = "etf_train"
    ETF_BACKTEST = "etf_backtest"
    ETF_WALKFORWARD = "etf_walkforward"
    STOCK_WALKFORWARD = "stock_walkforward"
    STOCK_ML_FACTORS = "stock_ml_factors"
    # 辅助因子表（原先只由 scripts/ 下的脚本在服务外产出，会静默过期）
    STOCK_INDUSTRY_FACTORS = "stock_industry_factors"
    STOCK_MONEY_FLOW_FACTORS = "stock_money_flow_factors"
    # 原先只有同步端点（点击后全程转圈、无进度）；改为 job 化以便前端展示进度
    STOCK_ML_BACKTEST = "stock_ml_backtest"
    STOCK_ML_PREDICT = "stock_ml_predict"


class JobCreate(BaseModel):
    type: JobType
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobRecord(BaseModel):
    id: str
    type: JobType
    status: JobStatus
    progress: float = Field(default=0, ge=0, le=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
