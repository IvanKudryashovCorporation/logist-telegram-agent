"""Приём платежей за подписку."""

from typing import Optional

from app.config import settings
from app.services.payments.base import PaymentError, PaymentProvider, ProviderPayment


def get_provider() -> Optional[PaymentProvider]:
    """Настроенный провайдер или ``None`` (оплата недоступна, подписка живёт на пробном периоде).

    Функция — единственная точка подмены провайдера в тестах."""
    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        from app.services.payments.yookassa import YooKassa

        return YooKassa()
    return None


__all__ = ["PaymentError", "PaymentProvider", "ProviderPayment", "get_provider"]
