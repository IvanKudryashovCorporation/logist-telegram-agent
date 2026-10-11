"""Интерфейс платёжного провайдера: сайт и воркеры знают только его, а не ЮKassa.

Так провайдера можно сменить (банк напрямую, другой агрегатор), а в тестах подставить фейк.
"""

from dataclasses import dataclass
from typing import Optional, Protocol

STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_CANCELED = "canceled"


@dataclass(frozen=True)
class ProviderPayment:
    """Платёж глазами провайдера."""

    id: str
    #: ``pending`` | ``succeeded`` | ``canceled``
    status: str
    amount_kop: int
    telegram_id: Optional[int] = None
    kind: str = ""
    days: int = 0
    #: Куда отправить водителя платить (для автосписаний пусто).
    confirmation_url: str = ""
    #: Способ оплаты, сохранённый у провайдера для автосписаний.
    payment_method_id: Optional[str] = None


class PaymentError(RuntimeError):
    """Провайдер недоступен или отверг запрос."""


class PaymentProvider(Protocol):
    name: str

    async def create_payment(
        self, *, telegram_id: int, kind: str, amount_kop: int, days: int, return_url: str, description: str
    ) -> ProviderPayment:
        """Создаёт платёж; ``kind`` — ``sbp`` (разовая оплата по СБП) или ``card`` (карта с сохранением)."""

    async def charge_saved(
        self, *, telegram_id: int, payment_method_id: str, amount_kop: int, days: int, description: str,
        idempotence_key: str,
    ) -> ProviderPayment:
        """Автосписание с сохранённого способа оплаты."""

    async def fetch(self, payment_id: str) -> ProviderPayment:
        """Актуальное состояние платежа. Вебхуку не верим: статус всегда берём отсюда."""
