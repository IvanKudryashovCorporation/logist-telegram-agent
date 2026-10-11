"""Провайдер ЮKassa (https://yookassa.ru/developers/api).

* СБП: ``payment_method_data.type = sbp`` — разовая оплата, комиссия минимальная.
* Карта: ``save_payment_method = true`` — способ оплаты сохраняется для автопродления.
* Автосписание: платёж с ``payment_method_id`` без подтверждения от водителя.
* Статус платежа берём запросом ``GET /payments/{id}``, а не из вебхука: у уведомлений нет подписи.

Чек самозанятого ЮKassa формирует отдельной услугой (включается в личном кабинете), в запрос он не входит.
"""

import logging
import uuid
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.payments.base import (
    STATUS_CANCELED,
    STATUS_PENDING,
    STATUS_SUCCEEDED,
    PaymentError,
    ProviderPayment,
    ProviderRefund,
)

log = logging.getLogger("payments.yookassa")

API_URL = "https://api.yookassa.ru/v3/payments"
REFUNDS_URL = "https://api.yookassa.ru/v3/refunds"


def _amount(amount_kop: int) -> dict[str, str]:
    return {"value": f"{amount_kop / 100:.2f}", "currency": "RUB"}


def _status(raw: str) -> str:
    # waiting_for_capture при capture=true не встречается, но на всякий случай считаем «ещё не оплачено».
    if raw == "succeeded":
        return STATUS_SUCCEEDED
    if raw == "canceled":
        return STATUS_CANCELED
    return STATUS_PENDING


def parse_payment(data: dict[str, Any]) -> ProviderPayment:
    """Ответ ЮKassa -> :class:`ProviderPayment`."""
    meta = data.get("metadata") or {}
    method = data.get("payment_method") or {}
    amount = data.get("amount") or {}
    card = method.get("card") or {}
    last4 = str(card.get("last4") or "")
    try:
        amount_kop = round(float(amount.get("value", "0")) * 100)
    except (TypeError, ValueError):
        amount_kop = 0
    try:
        telegram_id = int(meta["telegram_id"]) if meta.get("telegram_id") else None
    except (TypeError, ValueError):
        telegram_id = None
    try:
        days = int(meta.get("days", 0))
    except (TypeError, ValueError):
        days = 0
    return ProviderPayment(
        id=str(data.get("id", "")),
        status=_status(str(data.get("status", ""))),
        amount_kop=amount_kop,
        telegram_id=telegram_id,
        kind=str(meta.get("kind", "")),
        days=days,
        confirmation_url=str((data.get("confirmation") or {}).get("confirmation_url", "")),
        payment_method_id=str(method["id"]) if method.get("saved") and method.get("id") else None,
        card_last4=last4 if last4.isdigit() and len(last4) == 4 else None,
        card_brand=str(card.get("card_type") or "")[:24] or None,
    )


def parse_refund(data: dict[str, Any]) -> ProviderRefund:
    """Ответ ЮKassa о возврате -> :class:`ProviderRefund`."""
    amount = data.get("amount") or {}
    try:
        amount_kop = round(float(amount.get("value", "0")) * 100)
    except (TypeError, ValueError):
        amount_kop = 0
    return ProviderRefund(
        id=str(data.get("id", "")),
        status=_status(str(data.get("status", ""))),
        payment_id=str(data.get("payment_id", "")),
        amount_kop=amount_kop,
    )


class YooKassa:
    name = "yookassa"

    def __init__(self, shop_id: Optional[str] = None, secret_key: Optional[str] = None) -> None:
        self._auth = (shop_id or settings.yookassa_shop_id, secret_key or settings.yookassa_secret_key)

    async def _request(
        self, method: str, url: str, *, json: Optional[dict] = None, idempotence_key: Optional[str] = None
    ) -> dict[str, Any]:
        headers = {"Idempotence-Key": idempotence_key or uuid.uuid4().hex} if method == "POST" else {}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=20.0, auth=self._auth) as client:
                response = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise PaymentError(f"ЮKassa недоступна: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            log.error("ЮKassa %s %s -> %s %s", method, url, response.status_code, response.text[:300])
            raise PaymentError(f"ЮKassa отклонила запрос ({response.status_code})", status=response.status_code)
        return response.json()

    async def create_payment(
        self, *, telegram_id: int, kind: str, amount_kop: int, days: int, return_url: str, description: str
    ) -> ProviderPayment:
        body: dict[str, Any] = {
            "amount": _amount(amount_kop),
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": {"telegram_id": str(telegram_id), "kind": kind, "days": str(days)},
        }
        if kind == "sbp":
            body["payment_method_data"] = {"type": "sbp"}
        elif kind == "autopay":
            # Автоплатёж: карта сохраняется у ЮKassa, дальше подписка продлевается списаниями без участия водителя.
            body["payment_method_data"] = {"type": "bank_card"}
            body["save_payment_method"] = True
        elif kind == "card":
            body["payment_method_data"] = {"type": "bank_card"}  # обычная разовая оплата картой, карта не сохраняется
        # иначе (kind == "any") способ не задаём: водитель выберет из подключённых в магазине.
        return parse_payment(await self._request("POST", API_URL, json=body))

    async def charge_saved(
        self, *, telegram_id: int, payment_method_id: str, amount_kop: int, days: int, description: str,
        idempotence_key: str,
    ) -> ProviderPayment:
        body = {
            "amount": _amount(amount_kop),
            "capture": True,
            "payment_method_id": payment_method_id,
            "description": description[:128],
            "metadata": {"telegram_id": str(telegram_id), "kind": "autorenew", "days": str(days)},
        }
        return parse_payment(await self._request("POST", API_URL, json=body, idempotence_key=idempotence_key))

    async def fetch(self, payment_id: str) -> ProviderPayment:
        return parse_payment(await self._request("GET", f"{API_URL}/{payment_id}"))

    async def fetch_refund(self, refund_id: str) -> ProviderRefund:
        return parse_refund(await self._request("GET", f"{REFUNDS_URL}/{refund_id}"))
