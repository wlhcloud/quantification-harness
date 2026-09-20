from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROOT = Path(__file__).resolve().parents[4]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATA_QUERY_", extra="ignore")
    project_root: Path = DEFAULT_ROOT
    host: str = "127.0.0.1"
    port: int = 9103
    max_rows: int = 2000
    busy_timeout_ms: int = 1500

    def database(self, name: str) -> Path:
        return self.project_root / "data" / f"{name}.db"

    @property
    def model_config_path(self) -> Path:
        return self.project_root / "config" / "factor-models.yaml"

settings = Settings()
