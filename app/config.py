"""Конфигурация приложения: читается из .env."""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _empty_to_none(value: object) -> object:
    """Незаполненная строка в .env означает «не задано», а не ошибку."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalInt = Annotated[Optional[int], BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    tg_api_id: OptionalInt = None
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_session_name: str = "logist"

    # Чаты
    work_group_chat_id: OptionalInt = None
    logist_user_id: OptionalInt = None

    # LLM
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # БД
    database_url: str = "sqlite+aiosqlite:///./logist.db"

    # Бизнес-правила (см. опрос: вопросы 25-26, 30, 52, 75, 120, 124)
    default_driver_share: Decimal = Decimal("0.80")
    waiting_rate_per_hour: Decimal = Decimal("500")
    max_negotiation_uplift_pct: int = 15
    repost_uplift_pct: int = 10
    repost_interval_minutes: int = 10
    commission_reminder_hours: int = 24
    payment_details: str = ""

    @property
    def session_path(self) -> Path:
        return PROJECT_ROOT / f"{self.tg_session_name}.session"


settings = Settings()
