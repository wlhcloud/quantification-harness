import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROOT = Path(__file__).resolve().parents[4]

# 管理 key 的唯一权威变量名。9101/9102/每日链/看门狗都读它。
# 历史上有 QUANT_SYNC_API_KEY / QUANT_ENGINE_API_KEY 两个同名同值的变量（key 收敛前），
# 仍作为**兼容回退**保留，避免旧部署/旧脚本（scripts/run_quant_sync.ps1 等）失效。
SHARED_API_KEY_ENV = "QUANT_API_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANT_SYNC_", extra="ignore")

    project_root: Path = DEFAULT_ROOT
    host: str = "127.0.0.1"
    port: int = 9101
    request_timeout_ms: int = 30000
    concurrency: int = 10
    one_minute_segment_days: int = 30
    five_minute_segment_days: int = 120
    api_key: str = ""
    # 诊断用：key 实际来自哪个环境变量（health 会返回，便于确认"有没有真的加载到"）
    api_key_source: str = ""
    credential_key: str = ""
    # 行情调度线程：交易时段会自动发起真实上游同步。
    # 测试/CI 里用 QUANT_SYNC_SCHEDULER_ENABLED=0 关闭，避免测试进程联网。
    scheduler_enabled: bool = True
    # 股票分钟线（minute_bars）：默认关闭。
    # 2026-09 起不再保留股票分钟数据（原 21GB / 1.29 亿行已清零），
    # 置 False 后 /sync/minute* 一律 410，避免误触发把 21GB 重新下载回来。
    minute_sync_enabled: bool = False

    @model_validator(mode="after")
    def _resolve_shared_api_key(self) -> "Settings":
        """key 收敛：QUANT_API_KEY **权威**，服务专属变量只在它为空时回退。

        注意顺序不能反：先看服务专属字段会让一个陈旧的 QUANT_SYNC_API_KEY 悄悄覆盖
        新写入的 QUANT_API_KEY（轮换时会出现"改了没生效"）。
        """
        shared = os.environ.get(SHARED_API_KEY_ENV, "").strip()
        if shared:
            self.api_key = shared
            self.api_key_source = SHARED_API_KEY_ENV
        elif self.api_key:
            self.api_key_source = "QUANT_SYNC_API_KEY"
        return self

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def meta_path(self) -> Path:
        return self.data_dir / "meta.db"

    @property
    def market_path(self) -> Path:
        return self.data_dir / "market.db"

    @property
    def finance_path(self) -> Path:
        return self.data_dir / "finance.db"

    @property
    def minute_path(self) -> Path:
        return self.data_dir / "minute.db"

    @property
    def sync_path(self) -> Path:
        return self.data_dir / "sync.db"

    @property
    def meta_contract_path(self) -> Path:
        """meta.db 契约 DDL（用于启动时自举，见 MetaStore.ensure_meta_schema）。"""
        return self.project_root / "contracts" / "schemas" / "meta.sql"


settings = Settings()
