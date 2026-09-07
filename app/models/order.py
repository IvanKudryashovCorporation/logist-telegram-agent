"""Заказ — центральная сущность. Поля соответствуют шаблону заявки (вопрос 12)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import OrderStatus


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Источник: сообщение диспетчера в рабочей группе ---
    # Храним id сообщения, чтобы ловить его редактирование (вопросы 19-20).
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Диспетчер определяется по аккаунту отправителя (вопрос 103).
    dispatcher_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    dispatcher_username: Mapped[Optional[str]] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Маршрут и время ---
    pickup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    # Полный адрес — только назначенному водителю (вопросы 61-62).
    from_address: Mapped[Optional[str]] = mapped_column(String(512))
    to_address: Mapped[Optional[str]] = mapped_column(String(512))
    # Город/направление — то, что можно писать в водительские группы.
    from_city: Mapped[Optional[str]] = mapped_column(String(128))
    to_city: Mapped[Optional[str]] = mapped_column(String(128))
    # Рейс/поезд диспетчер указывает, но водительским группам он не нужен (вопрос 60).
    flight_or_train: Mapped[Optional[str]] = mapped_column(String(128))

    # --- Параметры поездки ---
    # Класс авто печатаем в объявлении, только если он не стандартный (вопрос 58).
    car_class: Mapped[Optional[str]] = mapped_column(String(64))
    passengers: Mapped[Optional[int]] = mapped_column(Integer)
    # Багаж указываем в объявлении, только если объёмный (вопрос 59).
    luggage: Mapped[Optional[str]] = mapped_column(String(128))
    has_pets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_child_seat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Клиент (скрыт до назначения водителя, вопросы 61, 93-95) ---
    client_name: Mapped[Optional[str]] = mapped_column(String(128))
    client_phone: Mapped[Optional[str]] = mapped_column(String(64))

    # --- Деньги ---
    # Цена клиента: логист её видит и считает по ней маржу (вопросы 22-23).
    client_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Оплата водителю: по умолчанию 80% от стоимости (вопросы 25-26).
    driver_payment: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    # Комиссию назначает логист вручную, фиксированной суммой (вопросы 110-111).
    commission: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    commission_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    commission_screenshot_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # --- Состояние ---
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32),
        default=OrderStatus.NEW,
        nullable=False,
    )
    # «Проблема» — флажок поверх любого статуса, а не отдельный статус (вопрос 34).
    has_problem: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    problem_note: Mapped[Optional[str]] = mapped_column(Text)
    # Срочные заказы перепубликуются с повышением оплаты (вопросы 52, 123).
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Назначенный водитель ---
    assigned_driver_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Момент, когда водитель подтвердил созвон с клиентом (вопросы 36, 104, 113).
    driver_confirmed_call_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Факт передачи данных водителя диспетчеру (вопрос 108).
    handed_to_dispatcher_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    publications = relationship(
        "Publication", back_populates="order", cascade="all, delete-orphan"
    )
    responses = relationship(
        "DriverResponse", back_populates="order", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Order #{self.id} {self.from_city}->{self.to_city} {self.status.value}>"
