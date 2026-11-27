"""Validated application configuration with a paper-only startup invariant."""
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./portfolio.db"
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_allowed_chat_ids: str = ""
    kite_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    kite_access_token: SecretStr = Field(default=SecretStr(""), repr=False)
    kite_monitor_poll_interval_seconds: int = Field(30, ge=10, le=300)
    kite_monitor_max_stale_seconds: int = Field(120, ge=30, le=900)
    kite_monitor_retry_max_seconds: int = Field(30, ge=5, le=120)

    # Environment configuration can never silently boot the process into live mode.
    trading_mode: Literal["paper", "live"] = "paper"

    risk_per_trade_pct: float = Field(0.0075, gt=0, le=0.01)
    max_strategy_risk_pct: float = Field(0.01, gt=0, le=0.02)
    max_aggregate_open_risk_pct: float = Field(0.02, gt=0, le=0.05)
    daily_loss_stop_pct: float = Field(0.02, gt=0, le=0.10)
    weekly_drawdown_stop_pct: float = Field(0.04, gt=0, le=0.20)
    monthly_drawdown_stop_pct: float = Field(0.07, gt=0, le=0.30)
    max_concurrent_positions: int = Field(2, ge=1, le=10)
    max_new_entries_per_day: int = Field(3, ge=1, le=20)

    @field_validator("trading_mode", mode="before")
    @classmethod
    def force_paper_on_boot(cls, value: object) -> str:
        # Live mode is never restored from .env after a restart.
        return "paper"

    @field_validator("kite_monitor_max_stale_seconds")
    @classmethod
    def monitor_stale_window_must_cover_two_polls(cls, value: int, info: Any) -> int:
        interval = info.data.get("kite_monitor_poll_interval_seconds", 30)
        if value < interval * 2:
            raise ValueError("kite_monitor_max_stale_seconds must be at least twice the poll interval")
        return value

    def startup_mode(self) -> Literal["paper"]:
        return "paper"


settings = Settings()
