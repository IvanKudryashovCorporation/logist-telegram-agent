"""Водительские группы и публикации в них (вопросы 41-55, 133-140)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class DriverGroup(Base, TimestampMixin):
    """Группа для публикации заказов. Правила вносятся вручную (вопрос 136)."""

    __tablename__ = "driver_groups"

    id: Mapped[int] = mapped_column(primary_key=True)

    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    # Пусто = общая группа, иначе список направлений через запятую (вопрос 42).
    directions: Mapped[Optional[str]] = mapped_column(String(512))
    # Правила публикации свободным текстом — их учитывает агент при отборе.
    rules: Mapped[Optional[str]] = mapped_column(Text)
    # В части групп есть таймер между сообщениями (вопрос 49).
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    publications = relationship("Publication", back_populates="group")

    def __repr__(self) -> str:
        return f"<DriverGroup {self.title}>"


class Publication(Base, TimestampMixin):
    """Опубликованное объявление. Нужно, чтобы удалить его после назначения (вопросы 50-51)."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("driver_groups.id"), index=True)

    tg_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    text: Mapped[Optional[str]] = mapped_column(Text)
    # Оплата водителю на момент публикации: при перепубликации она растёт (вопрос 55).
    driver_payment_at_post: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Если удалить не удалось — ставим проблему и уведомляем логиста (вопрос 100).
    delete_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    order = relationship("Order", back_populates="publications")
    group = relationship("DriverGroup", back_populates="publications")

    def __repr__(self) -> str:
        return f"<Publication order={self.order_id} group={self.group_id}>"
