"""Конфигурация приложения: читается из .env."""

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

    # Чаты. Список рабочих групп живёт в БД (scripts.manage_work_groups);
    # это поле — только для разового переноса старой настройки из .env.
    work_group_chat_id: OptionalInt = None

    # LLM
    # Внимание: НЕ называть ANTHROPIC_*  — эти имена зарезервированы окружением
    # песочницы разработки и имеют приоритет над .env, значения будут подменены.
    llm_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
    # Задайте, если ключ выдан прокси-сервисом, а не напрямую console.anthropic.com
    llm_base_url: str = ""

    # БД
    database_url: str = "sqlite+aiosqlite:///./logist.db"

    # Веб-панель. Локально — 127.0.0.1, чтобы не торчать наружу.
    # На VPS для внешнего доступа задайте WEB_HOST=0.0.0.0 в .env.
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @property
    def session_path(self) -> Path:
        return PROJECT_ROOT / f"{self.tg_session_name}.session"


settings = Settings()
