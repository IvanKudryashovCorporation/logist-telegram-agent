"""Подключение к БД и фабрика сессий.

URL берётся из настроек: SQLite (``sqlite+aiosqlite:///./logist.db``) для
разработки и тестов, PostgreSQL (``postgresql+asyncpg://user:pass@host/db``)
на проде — переключение это замена DATABASE_URL в .env.

Время в БД везде «наивное UTC» (см. app/timeutil.py), и на PostgreSQL это
обеспечивается явно: колонки ``timestamp without time zone`` и сессия с
``timezone=UTC`` — иначе ``now()`` отдавал бы время в поясе сервера.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.config import settings


def _engine_options(url: str) -> dict:
    if not url.startswith("postgresql"):
        return {}
    if settings.db_null_pool:
        return {
            "poolclass": NullPool,
            "connect_args": {"server_settings": {"timezone": "UTC"}},
        }
    return {
        # Соединения живут дольше, чем перезапуск PostgreSQL/сетевой сбой, —
        # без проверки первый запрос после этого падал бы.
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 10,
        "connect_args": {"server_settings": {"timezone": "UTC"}},
    }


engine = create_async_engine(
    settings.database_url, echo=False, future=True, **_engine_options(settings.database_url)
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Базовый класс моделей."""


class TimestampMixin:
    """Отметки времени создания и изменения записи."""

    # Наивное UTC, как и остальное время в проекте. timezone=True на PostgreSQL
    # дал бы timestamptz и aware-значения, которые код нигде не ждёт.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
