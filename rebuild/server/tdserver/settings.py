"""Server settings, read from environment variables prefixed TD2_ (or a .env file next to the process)."""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).parent / "reference"  # not "data/": the repo ignores data folders


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TD2_", env_file=".env", extra="ignore")

    # prod unless told otherwise: a missing or misspelt TD2_ENV must never fall back to the insecure dev key
    env: str = Field("prod", description="prod | dev | test; only dev/test allow the built-in zero key")
    database_url: str = "sqlite:///./td2.db"
    # 32 random bytes, base64. Encrypts every broker secret and signs acknowledge links.
    # In production it comes from the secrets store (KMS later); never from the repository.
    master_key: str = ""
    public_base_url: str = "http://localhost:8000"
    app_scheme: str = "tradedesk"
    market_file: Path = DATA_DIR / "market.yaml"
    telegram_bot_token: str = ""
    ntfy_server: str = "https://ntfy.sh"
    session_days: int = 30
    console_session_hours: int = 12
    kite_login_base: str = "https://kite.zerodha.com/connect/login"
    kite_api_base: str = "https://api.kite.trade"
    # the Parquet market-data store (py/marketdata layout); empty = no data on this server yet
    store_root: str = ""
    duckdb_memory: str = "1GB"
    # where backtest results are kept (default: <store>/runs)
    runs_dir: str = ""
    # backtests wait for the close on trading days (09:00-15:40 IST) unless the owner turns this on
    research_in_market_hours: bool = False
    backtest_timeout_s: int = 1800
    backtest_memory_mb: int = 900
    # files shared with the update scripts (update-request, update-status.json, update-available.json)
    data_dir: str = "/opt/td2/data"
    live_enabled_file: str = ""   # root-owned file outside the services' write paths; present = live allowed
    # annual risk-free rate for option Greeks and implied volatility (decimal; roughly the 91-day T-bill yield)
    risk_free_rate: float = 0.065
    # US stocks (IBKR): the folder ibkr-sync writes to, and the files that start/stop IB Gateway (empty = not set up)
    ibkr_data_dir: str = ""
    ibkr_request_file: str = ""
    ibkr_control_file: str = ""
    # red health lights raise alerts (tests turn this off unless they test it)
    health_alerts: bool = True

    @field_validator("public_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def master_key_bytes(self) -> bytes:
        if not self.master_key:
            if self.env not in ("dev", "test") or not self.database_url.startswith("sqlite"):
                raise RuntimeError("TD2_MASTER_KEY is required (only TD2_ENV=dev with SQLite may run without it)")
            return b"\x00" * 32  # dev and tests only
        raw = base64.b64decode(self.master_key)
        if len(raw) != 32:
            raise RuntimeError("TD2_MASTER_KEY must be 32 bytes, base64-encoded")
        return raw

    @property
    def is_dev(self) -> bool:
        return self.env in ("dev", "test")

    def check_production(self) -> None:
        if self.is_dev:
            return
        self.master_key_bytes()
        if not self.public_base_url.startswith("https://"):
            raise RuntimeError("TD2_PUBLIC_BASE_URL must be https in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
