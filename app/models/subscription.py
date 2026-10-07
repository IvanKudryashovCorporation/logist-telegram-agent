"""Сохранённые фильтры водителя («Мои фильтры») и уведомления по ним в Telegram.

Когда водитель применяет фильтр с галочкой «присылать в Telegram», в ``params`` сохраняются
нормализованные параметры ленты (те же, что в адресе страницы), а фоновый воркер присылает ему
через бота каждый новый подходящий заказ ссылкой. Фильтров у водителя несколько, у каждого своё
включение уведомлений.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class OrderSubscription(Base, TimestampMixin):
    __tablename__ = "order_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Telegram id водителя — он же chat_id для бота. Фильтров у одного человека несколько.
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    #: Параметры фильтра строками: ``Filters.as_dict()`` (from_city, to_city, ...).
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Заказы, созданные раньше этого момента (наивное UTC), не присылаем:
    #: подписка — про новые заказы, а не про те, что водитель уже видит в ленте.
    since: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    #: Почему подписка выключилась сама (например, бот не может писать водителю).
    error: Mapped[Optional[str]] = mapped_column(String(255))

    def __repr__(self) -> str:
        return f"<OrderSubscription tg:{self.telegram_id} active={self.is_active}>"


class SubscriptionNotification(Base):
    """Что уже отправлено: по паре (подписка, заказ) повторно не пишем."""

    __tablename__ = "subscription_notifications"
    __table_args__ = (UniqueConstraint("subscription_id", "order_id", name="uq_sub_notification"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("order_subscriptions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Без внешнего ключа: заказ могут удалить очисткой, а запись о рассылке — нет смысла держать вечно.
    order_id: Mapped[int] = mapped_column(index=True, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    #: Номер сообщения бота у водителя (одно сообщение может нести несколько заказов).
    #: Пусто у уведомлений, отправленных до появления этого поля.
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    #: Когда заказ убран из сообщения (диспетчер удалил заявку): повторно не трогаем.
    retracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
