"""Подписка: страница оплаты, переход к оплате, возврат с оплаты, вебхук ЮKassa, оферта.

Деньги принимает провайдер (:mod:`app.services.payments`). Здесь только веб-обвязка; начисление дней делает
:func:`app.services.billing_flow.apply_payment` — единственное место, общее для вебхука, страницы возврата и
автосписания, и оно идемпотентно.
"""

import json
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Payment
from app.models.payment import KIND_ANY, KIND_AUTOPAY, KIND_CARD, KIND_SBP, PAYMENT_PENDING
from app.services import billing, billing_flow
from app.services import billing_events as events
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
_PAYMENT_KINDS = {
    KIND_SBP: "СБП", KIND_CARD: "Банковской картой", KIND_AUTOPAY: "Автоплатёж (карта)", KIND_ANY: "Другой способ",
    "autorenew": "Автопродление",
    "admin": "Начислено вручную",
}


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
            "retry": notice[4:] if notice[4:] in (KIND_SBP, KIND_CARD, KIND_AUTOPAY, KIND_ANY) else KIND_SBP,
        }
    return {"kind": "wait", "title": notice, "text": "", "retry": ""}


async def _reconcile_pending(telegram_id: int) -> Optional[billing_flow.Settlement]:
    """Сверяет с провайдером платежи, которые водитель начал, но вебхук о них не пришёл.

    Страница сама себя лечит: если вебхук потерялся, оплата всё равно засчитается при открытии страницы."""
    provider = get_provider()
    if provider is None:
        async with SessionLocal() as session:
            await billing.expire_stale_pending(session, telegram_id=telegram_id)
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
    # Платежи, которые не оплатили за 10 минут, перестают висеть «в ожидании».
    async with SessionLocal() as session:
        expired = await billing.expire_stale_pending(session, telegram_id=telegram_id)
    for payment in expired:
        await events.record(
            events.PAYMENT_CANCELED, telegram_id=telegram_id, provider_id=payment.provider_payment_id,
            amount_kop=payment.amount_kop, details="не оплачен за 10 минут",
        )
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
    requested = str(form.get("kind") or "")
    kind = requested if requested in (KIND_CARD, KIND_AUTOPAY, KIND_ANY) else KIND_SBP
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
        await events.alert(
            events.PAYMENT_CREATE_FAILED,
            f"🚨 Не удалось создать платёж ({kind}) для {await events.who(telegram_id)}: {exc}",
            telegram_id=telegram_id, throttle=timedelta(minutes=10),
        )
        return _redirect("/subscription", new_token, f"err:{kind}")
    if not info.confirmation_url:
        await events.alert(
            events.PAYMENT_CREATE_FAILED, f"🚨 ЮKassa создала платёж {info.id}, но не вернула ссылку на оплату.",
            telegram_id=telegram_id, provider_id=info.id, throttle=timedelta(minutes=10),
        )
        return _redirect("/subscription", new_token, f"err:{kind}")

    async with SessionLocal() as session:
        await billing.start_payment(
            session, telegram_id=telegram_id, provider=provider.name, provider_payment_id=info.id,
            kind=kind, amount_kop=amount, days=days,
        )
    await events.record(
        events.PAYMENT_CREATED, telegram_id=telegram_id, provider_id=info.id, amount_kop=amount,
        details=f"способ: {kind}",
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
            driver.card_last4 = driver.card_brand = None
            await session.commit()
    await events.record(events.AUTORENEW_OFF, telegram_id=telegram_id, details="водитель отключил автопродление")
    return _redirect("/subscription", new_token, "Автопродление выключено.")


async def _handle_refund(provider, refund_id: str) -> JSONResponse:
    """Возврат: сверяем с ЮKassa, снимаем соответствующие дни и сообщаем водителю."""
    try:
        result = await billing_flow.settle_refund(provider, refund_id)
    except PaymentError as exc:
        log.warning("Вебхук: сверка возврата %s не удалась: %s", refund_id, exc)
        await events.alert(
            events.WEBHOOK_ERROR if exc.permanent else events.PROVIDER_ERROR,
            f"🚨 Уведомление ЮKassa о возврате {refund_id}: сверка не удалась ({exc}).",
            provider_id=refund_id, throttle=timedelta(minutes=10),
        )
        # Нет такого возврата у магазина (4xx): повторять нечего. Сеть и 5xx — пусть ЮKassa повторит позже.
        return JSONResponse({"ok": exc.permanent}, status_code=200 if exc.permanent else 502)
    if result.applied and result.telegram_id is not None:
        log.info(
            "Возврат %s: у tg:%s снято %s дн., оплачено до %s", refund_id, result.telegram_id, result.days, result.until
        )
        from app.services.subscriptions import send_message

        await send_message(result.telegram_id, billing_flow.refund_message(result.days, result.until))
    return JSONResponse({"ok": True})


@router.post("/payments/yookassa")
async def yookassa_webhook(request: Request):
    """Уведомление ЮKassa. Телу не верим (подписи нет): берём из него только id платежа и перепроверяем статус
    запросом к ЮKassa с нашими ключами. Всегда отвечаем 200, чтобы провайдер не слал повторы без нужды."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"ok": True})
    try:
        payload = await request.json()
        event = str(payload.get("event") or "")
        payment_id = str((payload.get("object") or {}).get("id") or "")
    except Exception:  # noqa: BLE001 — не JSON: просто игнорируем
        return JSONResponse({"ok": True})
    await events.record(events.WEBHOOK_RECEIVED, provider_id=payment_id or None, details=f"событие: {event or '—'}")
    if payment_id and event == "refund.succeeded":
        return await _handle_refund(provider, payment_id)
    # Дальше только события платежа (payment.succeeded / canceled / waiting_for_capture). Привязка способа оплаты
    # (payment_method.active) приходит с чужим id: запрашивать её как платёж нельзя.
    if not payment_id or not event.startswith("payment."):
        return JSONResponse({"ok": True})
    try:
        result = await billing_flow.settle(provider, payment_id)
    except PaymentError as exc:
        log.warning("Вебхук: сверка платежа %s не удалась: %s", payment_id, exc)
        await events.alert(
            events.WEBHOOK_ERROR if exc.permanent else events.PROVIDER_ERROR,
            f"🚨 Уведомление ЮKassa по платежу {payment_id}: сверка не удалась ({exc}). "
            + ("Такого платежа у магазина нет." if exc.permanent else "ЮKassa повторит запрос позже."),
            provider_id=payment_id, throttle=timedelta(minutes=10),
        )
        # Нет такого платежа у магазина (4xx): повторять нечего. Сеть и 5xx — пусть ЮKassa повторит позже.
        return JSONResponse({"ok": exc.permanent}, status_code=200 if exc.permanent else 502)
    if result.credited and result.telegram_id is not None:
        log.info("Оплата засчитана: tg:%s до %s (платёж %s)", result.telegram_id, result.until, payment_id)
        await billing_flow.notify_paid(result.telegram_id, result.until)
    return JSONResponse({"ok": True})


_FAQ = (
    ("Откуда берутся заказы?", "Из Telegram-групп диспетчеров такси и межгорода. Сервис читает свежие сообщения, "
     "отбирает настоящие заявки и показывает их в одной ленте."),
    ("Сколько это стоит?", "Первые {trial} дней бесплатно, потом {price} ₽ за {period} дней. Оплата по СБП или картой, "
     "автоплатёж включается по желанию и отключается в один клик."),
    ("Можно ли писать диспетчеру прямо с сайта?",
     "Да: у каждого заказа есть кнопка «Написать диспетчеру», она открывает чат в Telegram с готовым текстом."),
    ("Как приходят уведомления?", "Сохраните фильтр (города, радиус, тип авто, время, цена), и новые подходящие "
     "заказы будут приходить вам в Telegram."),
    ("Нужна ли регистрация?", "Нет, достаточно войти через Telegram."),
)


def _about_context() -> dict:
    base = settings.public_base_url.rstrip("/")
    price, period, trial = settings.subscription_price_rub, settings.subscription_period_days, settings.trial_days
    faq = [(q, a.format(trial=trial, price=price, period=period)) for q, a in _FAQ]
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebApplication", "name": "Лента заказов", "url": f"{base}/about", "inLanguage": "ru",
                "applicationCategory": "BusinessApplication",
                "description": "Заявки такси и межгорода из Telegram-групп диспетчеров в одной ленте для водителей.",
                "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "RUB"},
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq
                ],
            },
        ],
    }
    return {
        "base_url": base, "faq": faq, "price_rub": price, "period_days": period, "trial_days": trial,
        "ld_json": json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"),
    }


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    """Публичная страница о сервисе: единственная (кроме оферты), которую можно индексировать поисковикам."""
    async with SessionLocal() as session:
        counts = await queries.header_counts(session, "")
    return templates.TemplateResponse("about.html", _base_context(request, counts) | _about_context())


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
            "base_url": settings.public_base_url.rstrip("/"),
        },
    )
