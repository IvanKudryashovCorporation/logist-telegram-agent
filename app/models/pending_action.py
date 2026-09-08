"""Очередь действий с side-эффектами в Telegram, поставленных из веб-панели.

Веб-панель (отдельный процесс, FastAPI) не может сама писать в Telegram —
одновременное подключение второго Telethon-клиента к той же сессии ломает
файл сессии. Поэтому она только кладёт задачу сюда, а исполняет её уже
работающий агент (см. app/workflow/queue.py), у которого есть живой клиент.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import PendingActionStatus, PendingActionType


class PendingAction(Base, TimestampMixin):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    action: Mapped[PendingActionType] = mapped_column(
        Enum(PendingActionType, native_enum=False, length=32), nullable=False
    )
    # Для ASSIGN — id отклика-кандидата; для остальных действий не нужен.
    response_id: Mapped[Optional[int]] = mapped_column(Integer)

    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus, native_enum=False, length=16),
        default=PendingActionStatus.PENDING,
        nullable=False,
    )
    result_message: Mapped[Optional[str]] = mapped_column(Text)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PendingAction {self.action.value} order={self.order_id} {self.status.value}>"
