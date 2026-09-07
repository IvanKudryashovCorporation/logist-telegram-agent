"""История действий — обязательна по опросу (вопрос 8)."""

from typing import Optional

from sqlalchemy import BigInteger, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import ActorType


class ActionLog(Base, TimestampMixin):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    actor: Mapped[ActorType] = mapped_column(
        Enum(ActorType, native_enum=False, length=32), nullable=False
    )
    actor_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Короткий код действия: order_created, status_changed, published, deleted, ...
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ActionLog {self.action} order={self.order_id}>"
