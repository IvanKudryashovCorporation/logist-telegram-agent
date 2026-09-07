"""Карточка водителя и его отклики (вопросы 68-90)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import ResponseKind, ResponseStatus


class Driver(Base, TimestampMixin):
    """Карточка водителя. Обязательный набор данных — вопрос 81."""

    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)

    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    tg_username: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    name: Mapped[Optional[str]] = mapped_column(String(128))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    car_model: Mapped[Optional[str]] = mapped_column(String(128))
    car_plate: Mapped[Optional[str]] = mapped_column(String(32))

    # Фото храним ссылками на Telegram, файлы никуда не выгружаем (вопрос 86).
    photo_exterior_file_id: Mapped[Optional[str]] = mapped_column(String(256))
    photo_interior_file_id: Mapped[Optional[str]] = mapped_column(String(256))
    # Актуальность фото переспрашиваем всегда, но дату держим для подсказки (вопросы 87-89).
    photos_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Чёрный список ведётся в отдельной Telegram-группе по никнеймам (вопрос 79).
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    responses = relationship("DriverResponse", back_populates="driver")

    @property
    def has_required_data(self) -> bool:
        """Полон ли набор данных, требуемый перед назначением (вопрос 81).

        При неполном наборе назначение не блокируется, но логисту
        показывается предупреждение (вопросы 97-98).
        """
        return all(
            [
                self.name,
                self.phone,
                self.car_model,
                self.car_plate,
                self.photo_exterior_file_id,
                self.photo_interior_file_id,
            ]
        )

    def __repr__(self) -> str:
        return f"<Driver #{self.id} {self.name or self.tg_username}>"


class DriverResponse(Base, TimestampMixin):
    """Отклик водителя на заказ."""

    __tablename__ = "driver_responses"

    id: Mapped[int] = mapped_column(primary_key=True)

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"), index=True)

    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[ResponseKind] = mapped_column(
        Enum(ResponseKind, native_enum=False, length=32),
        default=ResponseKind.UNCLEAR,
        nullable=False,
    )
    status: Mapped[ResponseStatus] = mapped_column(
        Enum(ResponseStatus, native_enum=False, length=32),
        default=ResponseStatus.NEW,
        nullable=False,
    )
    # Сколько водитель просит при торге; агент вправе дать не более +15% (вопрос 75).
    requested_payment: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    agreed_payment: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    order = relationship("Order", back_populates="responses")
    driver = relationship("Driver", back_populates="responses")

    def __repr__(self) -> str:
        return f"<DriverResponse order={self.order_id} driver={self.driver_id} {self.kind.value}>"
