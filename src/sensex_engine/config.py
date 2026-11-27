"""Validated application configuration with a paper-only startup invariant."""
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./portfolio.db"
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_allowed_chat_ids: str = ""

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

    def startup_mode(self) -> Literal["paper"]:
        return "paper"


settings = Settings()
