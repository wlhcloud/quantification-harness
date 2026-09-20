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


# 显示名与枚举放在一起：新增/退役 job 类型时不会出现"代码改了、标签忘了"。
# 退役类型的标签保留在 UNKNOWN_JOB_TYPE_LABEL 兜底，见 job_type_label()。
JOB_TYPE_LABELS: dict[str, str] = {
    "features": "特征计算",
    "training": "模型训练",
    "backtest": "回测",
    "inference": "推理",
    "etf_factors": "ETF 因子",
    "etf_research": "ETF 因子研究",
    "etf_train": "ETF 训练",
    "etf_backtest": "ETF 回测",
    "etf_walkforward": "ETF Walkforward",
    "stock_walkforward": "股票 Walkforward",
    "stock_ml_factors": "股票 ML 因子",
    "stock_industry_factors": "行业因子",
    "stock_money_flow_factors": "资金流因子",
    "stock_ml_backtest": "股票 ML 回测",
    "stock_ml_predict": "股票 ML 选股预测",
}

# 历史记录里的类型可能已随功能退役而删除（例如 factor_mining 随 legacy 因子选股退役）。
# 这类记录**必须仍可读出**：一条历史记录不该让整个任务列表接口 500。
UNKNOWN_JOB_TYPE_LABEL = "已退役任务"


def job_type_label(value: object) -> str:
    """任务类型的显示名；未知/已退役类型返回兜底标签而不是抛错。"""
    text = value.value if isinstance(value, JobType) else str(value or "")
    return JOB_TYPE_LABELS.get(text, UNKNOWN_JOB_TYPE_LABEL)


class JobCreate(BaseModel):
    type: JobType
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobRecord(BaseModel):
    id: str
    # 放宽为 str：历史记录里可能有已退役的类型（如 factor_mining）。
    # 提交侧 JobCreate.type 仍是严格的 JobType，所以这不放松任何写契约。
    type: str
    status: str
    progress: float = Field(default=0, ge=0, le=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
