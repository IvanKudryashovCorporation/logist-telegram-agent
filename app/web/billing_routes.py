"""Подписка: страница оплаты, переход к оплате, возврат с оплаты, вебхук ЮKassa, оферта.

Деньги принимает провайдер (:mod:`app.services.payments`). Здесь только веб-обвязка; начисление дней делает
:func:`app.services.billing_flow.apply_payment` — единственное место, общее для вебхука, страницы возврата и
автосписания, и оно идемпотентно.
"""

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Payment
from app.models.payment import KIND_CARD, KIND_SBP, PAYMENT_PENDING
from app.services import billing, billing_flow
from app.services.payments import PaymentError, get_provider
from app.timeutil import now_utc_naive
from app.web import queries
from app.web.deps import (
    attach_driver_cookie,
    clear_flash,
    logged_in_telegram_id,
    pop_flash,
    resolve_driver,
)
from app.web.routes import _base_context, _login_redirect, _redirect
from app.web.templates_env import templates

log = logging.getLogger("web.billing")

router = APIRouter()

#: Сколько последних платежей показываем и сверяем с провайдером при открытии страницы.
_HISTORY_LIMIT = 10
_PAYMENT_KINDS = {KIND_SBP: "СБП", KIND_CARD: "Картой", "autorenew": "Автопродление", "admin": "Начислено вручную"}


def _banner(notice: Optional[str]) -> Optional[dict]:
    """Всплывающее сообщение на странице подписки: по тексту flash определяем вид баннера."""
    if not notice:
        return None
    if notice.startswith("ok:"):
        return {"kind": "ok", "title": "Оплата получена", "text": f"Подписка продлена до {notice[3:]}.", "retry": ""}
    if notice == "wait":
        return {
            "kind": "wait", "title": "Платёж ещё обрабатывается",
            "text": "Обычно это занимает до минуты. Обновите страницу.", "retry": "",
        }
    if notice.startswith("err:"):
        return {
            "kind": "err", "title": "Не удалось создать платёж",
            "text": "Деньги не списаны. Попробуйте ещё раз или выберите другой способ оплаты.",
            "retry": notice[4:] if notice[4:] in (KIND_SBP, KIND_CARD) else KIND_SBP,
        }
    return {"kind": "wait", "title": notice, "text": "", "retry": ""}


async def _reconcile_pending(telegram_id: int) -> Optional[billing_flow.Settlement]:
    """Сверяет с провайдером платежи, которые водитель начал, но вебхук о них не пришёл.

    Страница сама себя лечит: если вебхук потерялся, оплата всё равно засчитается при открытии страницы."""
    provider = get_provider()
    if provider is None:
        return None
    async with SessionLocal() as session:
        pending = (
            await session.execute(
                select(Payment.provider_payment_id)
                .where(Payment.telegram_id == telegram_id, Payment.status == PAYMENT_PENDING)
                .order_by(Payment.id.desc())
                .limit(3)
            )
        ).scalars().all()
    last: Optional[billing_flow.Settlement] = None
    for payment_id in pending:
        try:
            result = await billing_flow.settle(provider, payment_id)
        except PaymentError as exc:
            log.warning("Сверка платежа %s не удалась: %s", payment_id, exc)
            continue
        if result.credited:
            last = result
    return last


@router.get("/subscription", response_class=HTMLResponse)
async def subscription_page(request: Request):
    """Состояние подписки, цена, оплата и история платежей."""
    if not settings.telegram_login_enabled:
        return HTMLResponse("Страница не найдена", status_code=404)
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request)
    telegram_id = logged_in_telegram_id(request)

    settled = await _reconcile_pending(telegram_id)
    if settled is not None:
        await billing_flow.notify_paid(telegram_id, settled.until)

    async with SessionLocal() as session:
        driver = await billing.get_driver(session, telegram_id)
        if driver is None:
            return _login_redirect(request)
        counts = await queries.header_counts(session, token)
        payments = (
            await session.execute(
                select(Payment)
                .where(Payment.telegram_id == telegram_id)
                .order_by(Payment.id.desc())
                .limit(_HISTORY_LIMIT)
            )
        ).scalars().all()

    sub = billing.describe(driver, telegram_id)
    html = templates.TemplateResponse(
        "subscription.html",
        _base_context(request, counts, None)
        | {
            "banner": _banner(pop_flash(request)),
            "next_charge": (sub.until - timedelta(days=1)) if sub.until else None,
            "driver": driver,
            "sub": sub,
            "payments": payments,
            "kind_labels": _PAYMENT_KINDS,
            "price_rub": settings.subscription_price_rub,
            "period_days": settings.subscription_period_days,
            "can_pay": get_provider() is not None,
            "trial_days": settings.trial_days,
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.post("/subscription/pay")
async def subscription_pay(request: Request):
    """Создаёт платёж у провайдера и отправляет водителя на оплату (СБП или карта с автопродлением)."""
    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/subscription")
    telegram_id = logged_in_telegram_id(request)

    form = await request.form()
    kind = KIND_CARD if str(form.get("kind") or "") == KIND_CARD else KIND_SBP
    provider = get_provider()
    if provider is None:
        return _redirect("/subscription", new_token, "Оплата пока недоступна. Попробуйте позже.")

    amount = settings.subscription_price_rub * 100
    days = settings.subscription_period_days
    try:
        info = await provider.create_payment(
            telegram_id=telegram_id, kind=kind, amount_kop=amount, days=days,
            return_url=f"{settings.public_base_url}/subscription/return",
            description=f"Подписка на {days} дн.",
        )
    except PaymentError as exc:
        log.error("Не удалось создать платёж для tg:%s: %s", telegram_id, exc)
        return _redirect("/subscription", new_token, f"err:{kind}")
    if not info.confirmation_url:
        return _redirect("/subscription", new_token, f"err:{kind}")

    async with SessionLocal() as session:
        await billing.start_payment(
            session, telegram_id=telegram_id, provider=provider.name, provider_payment_id=info.id,
            kind=kind, amount_kop=amount, days=days,
        )
    return _redirect(info.confirmation_url, new_token)


@router.get("/subscription/return")
async def subscription_return(request: Request):
    """Сюда провайдер возвращает водителя после оплаты: проверяем платёж и показываем итог."""
    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/subscription")
    telegram_id = logged_in_telegram_id(request)

    settled = await _reconcile_pending(telegram_id)
    if settled is not None:
        await billing_flow.notify_paid(telegram_id, settled.until)
        notice = f"ok:{settled.until:%d.%m.%Y}"
    else:
        notice = "wait"
    return _redirect("/subscription", new_token, notice)


@router.post("/subscription/autorenew/off")
async def subscription_autorenew_off(request: Request):
    """Выключает автопродление: сохранённая карта больше не используется."""
    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/subscription")
    telegram_id = logged_in_telegram_id(request)
    async with SessionLocal() as session:
        driver = await billing.get_driver(session, telegram_id)
        if driver is not None:
            driver.auto_renew = False
            driver.payment_method_id = None
            await session.commit()
    return _redirect("/subscription", new_token, "Автопродление выключено.")


@router.post("/payments/yookassa")
async def yookassa_webhook(request: Request):
    """Уведомление ЮKassa. Телу не верим (подписи нет): берём из него только id платежа и перепроверяем статус
    запросом к ЮKassa с нашими ключами. Всегда отвечаем 200, чтобы провайдер не слал повторы без нужды."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"ok": True})
    try:
        payload = await request.json()
        payment_id = str((payload.get("object") or {}).get("id") or "")
    except Exception:  # noqa: BLE001 — не JSON: просто игнорируем
        return JSONResponse({"ok": True})
    if not payment_id:
        return JSONResponse({"ok": True})
    try:
        result = await billing_flow.settle(provider, payment_id)
    except PaymentError as exc:
        log.warning("Вебхук: сверка платежа %s не удалась: %s", payment_id, exc)
        return JSONResponse({"ok": False}, status_code=502)  # пусть ЮKassa повторит позже
    if result.credited and result.telegram_id is not None:
        log.info("Оплата засчитана: tg:%s до %s (платёж %s)", result.telegram_id, result.until, payment_id)
        await billing_flow.notify_paid(result.telegram_id, result.until)
    return JSONResponse({"ok": True})


@router.get("/offer", response_class=HTMLResponse)
async def offer_page(request: Request):
    """Публичная оферта: условия платной подписки (нужна платёжному провайдеру и покупателю)."""
    async with SessionLocal() as session:
        counts = await queries.header_counts(session, "")
    return templates.TemplateResponse(
        "offer.html",
        _base_context(request, counts)
        | {
            "price_rub": settings.subscription_price_rub,
            "period_days": settings.subscription_period_days,
            "trial_days": settings.trial_days,
            "seller": settings.offer_seller_name.strip(),
            "seller_inn": settings.offer_seller_inn.strip(),
            "today": now_utc_naive(),
        },
    )
