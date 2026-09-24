"""Заказ — заявка на перевозку, разобранная из сообщения в рабочей группе.

Агрегатор для водителей: показываем заявку как есть, без наценки и без
собственного флоу назначения — водитель сам пишет диспетчеру.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import OrderStatus


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Источник: сообщение диспетчера в рабочей группе ---
    # Храним id сообщения, чтобы ловить его редактирование.
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Диспетчер определяется по аккаунту отправителя — на него ведёт кнопка
    # "Написать диспетчеру" на сайте.
    dispatcher_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    dispatcher_username: Mapped[Optional[str]] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Маршрут и время ---
    pickup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    from_address: Mapped[Optional[str]] = mapped_column(String(512))
    to_address: Mapped[Optional[str]] = mapped_column(String(512))
    from_city: Mapped[Optional[str]] = mapped_column(String(128))
    to_city: Mapped[Optional[str]] = mapped_column(String(128))
    flight_or_train: Mapped[Optional[str]] = mapped_column(String(128))

    # --- Параметры поездки ---
    car_class: Mapped[Optional[str]] = mapped_column(String(64))
    passengers: Mapped[Optional[int]] = mapped_column(Integer)
    luggage: Mapped[Optional[str]] = mapped_column(String(128))
    has_pets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_child_seat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Клиент ---
    client_name: Mapped[Optional[str]] = mapped_column(String(128))
    client_phone: Mapped[Optional[str]] = mapped_column(String(64))

    # --- Деньги: 1 в 1 из заявки, без наценки ---
    client_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Оставлено как синоним client_price (совпадает с ним) — на случай, если
    # в заявке отдельно указана сумма именно для водителя.
    driver_payment: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    # --- Состояние ---
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32),
        default=OrderStatus.NEW,
        nullable=False,
    )
    has_problem: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    problem_note: Mapped[Optional[str]] = mapped_column(Text)
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<Order #{self.id} {self.from_city}->{self.to_city} {self.status.value}>"
