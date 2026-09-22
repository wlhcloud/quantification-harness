import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ROOT = Path(__file__).resolve().parents[4]

# 管理 key 的唯一权威变量名（与 quant-sync 共用同一个值，见 services/quant-sync/settings.py）。
# QUANT_ENGINE_API_KEY 仍作为兼容回退保留。
SHARED_API_KEY_ENV = "QUANT_API_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANT_ENGINE_", extra="ignore")

    project_root: Path = DEFAULT_ROOT
    host: str = "127.0.0.1"
    port: int = 9102
    # 量化训练是重 CPU/内存任务；全局串行避免 ETF 与股票训练互相争抢资源。
    max_workers: int = 1
    api_key: str = ""
    # 诊断用：key 实际来自哪个环境变量（health 会返回）
    api_key_source: str = ""
    # 短线（T+1 隔夜）回测依赖股票 5 分钟线；股票分钟数据已按使用方要求清零，
    # 置 False 后 kind='short' 的回测准入直接判不通过（不会产生无效 job）。
    short_backtest_enabled: bool = False

    @model_validator(mode="after")
    def _resolve_shared_api_key(self) -> "Settings":
        """key 收敛：QUANT_API_KEY **权威**，服务专属变量只在它为空时回退。

        注意顺序不能反：先看服务专属字段会让一个陈旧的 QUANT_ENGINE_API_KEY 悄悄覆盖
        新写入的 QUANT_API_KEY（轮换时会出现"改了没生效"）。
        """
        shared = os.environ.get(SHARED_API_KEY_ENV, "").strip()
        if shared:
            self.api_key = shared
            self.api_key_source = SHARED_API_KEY_ENV
        elif self.api_key:
            self.api_key_source = "QUANT_ENGINE_API_KEY"
        return self

    @property
    def raw_data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def feature_dir(self) -> Path:
        return self.project_root / "data" / "features"

    @property
    def artifact_dir(self) -> Path:
        return self.project_root / "artifacts"

    @property
    def metadata_path(self) -> Path:
        return self.project_root / "data" / "quant-engine.db"

    @property
    def market_path(self) -> Path:
        return self.project_root / "data" / "market.db"

    @property
    def finance_path(self) -> Path:
        return self.project_root / "data" / "finance.db"

    @property
    def minute_path(self) -> Path:
        return self.project_root / "data" / "minute.db"

    @property
    def factors_path(self) -> Path:
        return self.project_root / "data" / "factors.db"

    @property
    def backtest_path(self) -> Path:
        return self.project_root / "data" / "backtest.db"

    @property
    def model_config_path(self) -> Path:
        return self.project_root / "config" / "factor-models.yaml"

    @property
    def backtest_config_path(self) -> Path:
        return self.project_root / "config" / "backtest" / "t1-default.yaml"

    @property
    def etf_quant_db_path(self) -> Path:
        return self.project_root / "data" / "etf-quant.db"

    @property
    def etf_config_path(self) -> Path:
        return self.project_root / "config" / "etf-quant.yaml"

    @property
    def etf_artifact_dir(self) -> Path:
        return self.project_root / "artifacts" / "etf"

    @property
    def stock_ml_config_path(self) -> Path:
        return self.project_root / "config" / "stock-ml.yaml"

    @property
    def stock_ml_artifact_dir(self) -> Path:
        return self.project_root / "artifacts" / "stock-ml"


settings = Settings()
