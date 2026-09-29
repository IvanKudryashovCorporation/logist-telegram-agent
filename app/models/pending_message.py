"""Очередь сообщений, которые не удалось разобрать с первого раза.

Зачем нужна: Telethon НЕ переигрывает уже доставленное событие. Если LLM
ответила 429/таймаутом/5xx, сообщение терялось навсегда — заявка не попадала
в БД, и никто об этом не узнавал. Теперь такое сообщение складывается сюда,
а фоновый воркер (``app/telegram/queue_worker.py``) повторяет разбор с
экспоненциальной задержкой.

Храним только ``(chat_id, message_id)`` и текст: воркер перечитывает сообщение
из Telegram заново, поэтому правки диспетчера тоже учитываются.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import PendingStatus


class PendingMessage(Base, TimestampMixin):
    __tablename__ = "pending_messages"
    __table_args__ = (
        # Повторная постановка того же сообщения в очередь не создаёт дубль.
        UniqueConstraint("chat_id", "message_id", name="uq_pending_messages_source"),
        Index("ix_pending_messages_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_edit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Текст на момент постановки в очередь — для логов и админки.
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[PendingStatus] = mapped_column(
        Enum(PendingStatus, native_enum=False, length=16),
        default=PendingStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Когда пробовать в следующий раз (наивный UTC, см. app/timeutil.py).
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"<PendingMessage chat={self.chat_id} msg={self.message_id} "
            f"{self.status.value} tries={self.attempts}>"
        )
