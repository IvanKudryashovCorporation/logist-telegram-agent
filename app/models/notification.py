"""Уведомления внутри сайта (колокольчик в шапке).

Отдельно от сообщений в Telegram: это то, что водитель увидит на сайте — оплата получена, подписка скоро
закончится, объявление владельца. ``telegram_id`` пуст — уведомление для всех (рассылка владельца).
"""

from typing import Optional

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

#: Виды уведомлений (влияют только на подпись, не на логику).
KIND_INFO = "info"
KIND_PAYMENT = "payment"
KIND_SUBSCRIPTION = "subscription"


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Кому: id Telegram водителя; ``None`` — всем (рассылка).
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=KIND_INFO)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Куда ведёт уведомление (путь на сайте или внешняя ссылка); необязательно.
    url: Mapped[Optional[str]] = mapped_column(String(300))

    def __repr__(self) -> str:
        return f"<Notification #{self.id} tg:{self.telegram_id} {self.title!r}>"
