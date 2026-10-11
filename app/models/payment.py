"""Платежи за подписку.

Одна строка — один платёж у провайдера (ЮKassa). ``provider_payment_id`` уникален: вебхук и
перепроверка могут прийти несколько раз, а дни подписки нельзя начислить дважды.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

#: Статусы платежа (строкой, не Enum: см. README про переносимость на PostgreSQL).
PAYMENT_PENDING = "pending"
PAYMENT_SUCCEEDED = "succeeded"
PAYMENT_CANCELED = "canceled"

#: Виды платежа.
KIND_SBP = "sbp"
KIND_CARD = "card"
KIND_ANY = "any"
KIND_AUTORENEW = "autorenew"
KIND_ADMIN = "admin"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Кто платит. Без внешнего ключа: как у ``OrderSubscription``, запись должна пережить правку водителя.
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Идентификатор платежа у провайдера (для ручных начислений админа — ``admin-<uuid>``).
    provider_payment_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_kop: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: На сколько дней продлевает подписку.
    days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PAYMENT_PENDING)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime())

    def __repr__(self) -> str:
        return f"<Payment {self.provider_payment_id} tg:{self.telegram_id} {self.status}>"
