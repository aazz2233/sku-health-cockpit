"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """All runtime knobs. Env vars win over .env, .env wins over defaults."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # storage
    mysql_dsn: str | None = Field(default=None, description="mysql+pymysql://... DSN")
    duckdb_path: Path = ROOT / "data" / "warehouse" / "cockpit.duckdb"

    # generator
    data_seed: int = 20241119
    data_shops: int = 3
    data_skus: int = 200
    data_days: int = 90

    # llm
    llm_provider: str = "mock"
    llm_model: str | None = None
    llm_api_base: str | None = None
    llm_max_retries: int = 2
    llm_timeout_seconds: int = 60
    llm_max_tokens: int = 1200
    # reasoning models burn the whole budget on thinking and return no content;
    # this task needs no long chain-of-thought (all numbers are pre-computed)
    llm_disable_thinking: bool = True
    deepseek_api_key: str | None = None

    # paths
    data_dir: Path = ROOT / "data"
    generated_dir: Path = ROOT / "data" / "generated"
    reports_dir: Path = ROOT / "reports"
    config_dir: Path = ROOT / "config"

    def require_dsn(self) -> str:
        if not self.mysql_dsn:
            raise RuntimeError("MYSQL_DSN is not set. Copy .env.example to .env and fill it in.")
        return self.mysql_dsn


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
