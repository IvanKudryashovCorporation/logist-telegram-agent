"""Поток оплаты подписки: сверка платежа с провайдером, напоминания, автопродление.

* :func:`apply_payment` — единственное место, где платёж превращается в дни подписки. Его зовут и вебхук,
  и страница возврата, и автосписание: все пути идемпотентны по ``provider_payment_id``.
* :func:`remind_expiring` и :func:`autorenew_due` крутятся в фоне агента (``run_subscription_worker``).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver, Payment
from app.models.payment import KIND_ADMIN, KIND_AUTORENEW, PAYMENT_PENDING
from app.services import billing, notifications
from app.services import billing_events as events
from app.services.payments import PaymentProvider, ProviderPayment
from app.services.payments.base import STATUS_CANCELED, STATUS_SUCCEEDED, ProviderRefund
from app.timeutil import now_utc_naive

log = logging.getLogger("billing")

#: За сколько дней до конца напоминаем.
REMIND_DAYS = 3
#: Автосписание делаем, когда до конца осталось не больше этого.
RENEW_AHEAD = timedelta(days=1)
#: Сверка зависших платежей: не раньше чем через RECONCILE_AFTER, не дольше RECONCILE_WINDOW после создания.
RECONCILE_AFTER = timedelta(minutes=2)
RECONCILE_WINDOW = timedelta(days=3)
#: После стольких неудач подряд автопродление выключаем.
MAX_RENEW_FAILURES = 3
#: Не чаще одной попытки автосписания за это время.
RENEW_RETRY_EVERY = timedelta(hours=20)


@dataclass(frozen=True)
class Settlement:
    """Итог обработки платежа: начислены ли дни и до какого момента теперь оплачено."""

    credited: bool
    telegram_id: Optional[int] = None
    until: Optional[datetime] = None


def _plan_days(info: ProviderPayment) -> int:
    days = info.days or settings.subscription_period_days
    return max(1, min(days, 366))


async def apply_payment(session: AsyncSession, provider_name: str, info: ProviderPayment) -> Settlement:
    """Применяет состояние платежа от провайдера: оплачен — продлевает подписку, отменён — закрывает запись.

    Все исходы пишутся в журнал, а оплата и любые подозрительные случаи уходят владельцу в Telegram."""
    if info.status == STATUS_CANCELED:
        await billing.mark_canceled(session, info.id)
        await events.record(
            events.PAYMENT_CANCELED, telegram_id=info.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
            details=f"способ: {info.kind or '—'}",
        )
        return Settlement(False, info.telegram_id)
    if info.status != STATUS_SUCCEEDED:
        return Settlement(False, info.telegram_id)
    if info.telegram_id is None:
        log.error("Платёж %s оплачен, но без telegram_id в metadata — не начисляем", info.id)
        await events.alert(
            events.PAYMENT_ANOMALY,
            f"🚨 Платёж {info.id} на {info.amount_kop / 100:.0f} ₽ оплачен, но в нём нет пользователя: "
            "дни не начислены, разберите вручную.",
            provider_id=info.id, amount_kop=info.amount_kop,
        )
        return Settlement(False)
    if info.amount_kop < settings.subscription_price_rub * 100:
        # Мы создаём платежи только на полную цену; меньшая сумма — чужой или подменённый платёж.
        log.error("Платёж %s на %s коп. меньше цены подписки — не начисляем", info.id, info.amount_kop)
        await events.alert(
            events.PAYMENT_ANOMALY,
            f"🚨 Платёж {info.id} на {info.amount_kop / 100:.0f} ₽ меньше цены подписки "
            f"({settings.subscription_price_rub} ₽): дни не начислены.",
            telegram_id=info.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
        )
        return Settlement(False, info.telegram_id)

    days = _plan_days(info)
    payment, credited = await billing.record_success(
        session,
        telegram_id=info.telegram_id,
        provider=provider_name,
        provider_payment_id=info.id,
        kind=info.kind or "sbp",
        amount_kop=info.amount_kop,
        days=days,
        payment_method_id=info.payment_method_id,
        card_last4=info.card_last4,
        card_brand=info.card_brand,
    )
    if payment is None:
        await events.alert(
            events.PAYMENT_ANOMALY,
            f"🚨 Платёж {info.id} на {info.amount_kop / 100:.0f} ₽ оплачен, но пользователя id {info.telegram_id} "
            "нет в базе: дни не начислены.",
            telegram_id=info.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
        )
        return Settlement(False, info.telegram_id)

    driver = await billing.get_driver(session, info.telegram_id)
    until = driver.paid_until if driver else None
    await session.commit()  # закрываем чтение: журнал пишет в своей сессии
    if credited:
        kind = info.kind or "sbp"
        await events.alert(
            events.AUTORENEW_CHARGED if kind == "autorenew" else events.PAYMENT_SUCCEEDED,
            await events.payment_message(
                telegram_id=info.telegram_id, kind=kind, amount_kop=info.amount_kop, days=days, until=until,
                payment_id=info.id,
            ),
            telegram_id=info.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
            details=f"+{days} дн., оплачено до {until:%d.%m.%Y}" if until else f"+{days} дн.",
        )
        await notifications.add(
            info.telegram_id, "Оплата получена",
            f"Подписка продлена до {until:%d.%m.%Y}." if until else "Подписка продлена.",
            kind="payment", url="/subscription",
        )
    else:
        await events.record(
            events.PAYMENT_DUPLICATE, telegram_id=info.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
            details="платёж уже был засчитан, дни не добавлены",
        )
    return Settlement(credited, info.telegram_id, until)


@dataclass(frozen=True)
class RefundSettlement:
    """Итог обработки возврата: сколько дней снято и до какого момента теперь оплачено."""

    applied: bool
    telegram_id: Optional[int] = None
    days: int = 0
    until: Optional[datetime] = None


async def apply_refund(session: AsyncSession, info: ProviderRefund) -> RefundSettlement:
    """Применяет состояние возврата от провайдера: успешный возврат сокращает подписку на соответствующие дни."""
    if info.status != STATUS_SUCCEEDED or not info.payment_id:
        return RefundSettlement(False)
    refund, days = await billing.record_refund(
        session, provider_refund_id=info.id, provider_payment_id=info.payment_id, amount_kop=info.amount_kop,
    )
    if refund is None:
        await events.alert(
            events.REFUND_IGNORED,
            f"⚠️ Возврат {info.id} на {info.amount_kop / 100:.0f} ₽ по платежу {info.payment_id}, которого нет среди "
            "засчитанных у нас: дни не списаны, проверьте вручную.",
            provider_id=info.id, amount_kop=info.amount_kop,
        )
        return RefundSettlement(False)
    driver = await billing.get_driver(session, refund.telegram_id)
    until = driver.paid_until if driver else None
    await session.commit()  # закрываем чтение: журнал пишет в своей сессии
    if not days:
        await events.record(
            events.REFUND_DUPLICATE, telegram_id=refund.telegram_id, provider_id=info.id, amount_kop=info.amount_kop,
            details="возврат уже учтён или снимать нечего",
        )
        return RefundSettlement(False, refund.telegram_id)
    await events.alert(
        events.REFUND_SUCCEEDED,
        await events.refund_message(
            telegram_id=refund.telegram_id, amount_kop=info.amount_kop, days=days, until=until, refund_id=info.id,
        ),
        telegram_id=refund.telegram_id, provider_id=info.id, amount_kop=info.amount_kop, details=f"-{days} дн.",
    )
    await notifications.add(
        refund.telegram_id, "Платёж возвращён",
        f"Срок подписки сокращён на {days} дн." + (f" Теперь она действует до {until:%d.%m.%Y}." if until else ""),
        kind="payment", url="/subscription",
    )
    return RefundSettlement(True, refund.telegram_id, days, until)


async def settle_refund(provider: PaymentProvider, refund_id: str) -> RefundSettlement:
    """Сверяет возврат с провайдером и применяет. Уведомлению не верим: возврат берём у ЮKassa."""
    info = await provider.fetch_refund(refund_id)
    async with SessionLocal() as session:
        return await apply_refund(session, info)


def refund_message(days: int, until: Optional[datetime]) -> str:
    return f"↩️ Платёж возвращён: срок подписки сокращён на {days} дн. Теперь она действует до {_date(until)}."


async def settle(provider: PaymentProvider, payment_id: str) -> Settlement:
    """Сверяет платёж с провайдером и применяет. Вебхуку не верим: статус берём у ЮKassa."""
    info = await provider.fetch(payment_id)
    async with SessionLocal() as session:
        return await apply_payment(session, provider.name, info)


# --- Сообщения водителю ---------------------------------------------------------


def _date(moment: Optional[datetime]) -> str:
    return moment.strftime("%d.%m.%Y") if moment else "—"


def paid_message(until: Optional[datetime]) -> str:
    return f"✅ Оплата получена. Подписка продлена до {_date(until)}. Спасибо!"


def _pay_markup() -> dict:
    return {"inline_keyboard": [[{"text": "Оплатить подписку", "url": f"{settings.public_base_url}/subscription"}]]}


async def notify_paid(telegram_id: int, until: Optional[datetime], *, send=None) -> None:
    from app.services.subscriptions import send_message

    await (send or send_message)(telegram_id, paid_message(until))


# --- Напоминания ----------------------------------------------------------------


async def remind_expiring(*, send=None, now: Optional[datetime] = None) -> int:
    """Напоминает водителям, что подписка заканчивается (за 3 дня) и что она закончилась.

    Дедуп — по ``reminded_for``: у напоминания «скоро конец» там дата конца, у «закончилась» — дата конца + 1 день.
    Оплата сдвигает конец, и новое напоминание придёт уже для нового срока."""
    from app.services.subscriptions import BLOCKED, send_message

    send = send or send_message
    now = now or now_utc_naive()
    if not billing.enabled():
        return 0

    sent = 0
    async with SessionLocal() as session:
        drivers = (await session.execute(select(Driver))).scalars().all()
        for driver in drivers:
            sub = billing.describe(driver, driver.telegram_id, now)
            until = sub.until
            if until is None or sub.state in (billing.STATE_OFF, billing.STATE_ADMIN):
                continue
            end_day: date = until.date()
            if sub.state == billing.STATE_EXPIRED:
                if now - until > timedelta(days=2):
                    continue  # давно закончилась: напоминать об этом уже поздно
                marker, text = end_day + timedelta(days=1), (
                    "⏳ Подписка закончилась. Чтобы снова писать диспетчерам и получать заказы в Telegram, продлите её."
                )
            elif sub.days <= REMIND_DAYS and not driver.auto_renew:
                marker, text = end_day, (
                    f"⏳ Подписка заканчивается {_date(until)} (осталось дней: {sub.days}). "
                    "Продлите её, чтобы не терять заказы."
                )
            else:
                continue
            if driver.reminded_for == marker:
                continue
            outcome = await send(driver.telegram_id, text, _pay_markup())
            driver.reminded_for = marker  # BLOCKED тоже запоминаем: повторять бессмысленно
            await events.record(
                events.REMINDER_SENT, telegram_id=driver.telegram_id, details=text[:80], session=session,
            )
            await notifications.add(
                driver.telegram_id,
                "Подписка закончилась" if sub.state == billing.STATE_EXPIRED else "Подписка скоро закончится",
                text.lstrip("⏳ "), kind="subscription", url="/subscription", session=session,
            )
            if outcome != BLOCKED:
                sent += 1
        await session.commit()
    return sent


# --- Автопродление --------------------------------------------------------------


async def autorenew_due(
    *, provider: Optional[PaymentProvider] = None, send=None, now: Optional[datetime] = None
) -> int:
    """Списывает оплату с сохранённой карты у тех, у кого подписка кончается в ближайшие сутки."""
    from app.services.payments import get_provider
    from app.services.subscriptions import send_message

    send = send or send_message
    provider = provider or get_provider()
    now = now or now_utc_naive()
    if provider is None or not billing.enabled():
        return 0

    charged = 0
    problems: list[tuple] = []
    notes: list[int] = []
    async with SessionLocal() as session:
        drivers = (
            await session.execute(
                select(Driver).where(Driver.auto_renew.is_(True), Driver.payment_method_id.is_not(None))
            )
        ).scalars().all()
        for driver in drivers:
            sub = billing.describe(driver, driver.telegram_id, now)
            if sub.state in (billing.STATE_OFF, billing.STATE_ADMIN) or sub.until is None:
                continue
            if sub.until - now > RENEW_AHEAD:
                continue
            recent = (
                await session.execute(
                    select(Payment.id).where(
                        Payment.telegram_id == driver.telegram_id,
                        Payment.kind == KIND_AUTORENEW,
                        Payment.created_at > now - RENEW_RETRY_EVERY,
                    )
                )
            ).first()
            if recent is not None:
                continue  # сегодня уже пробовали

            amount = settings.subscription_price_rub * 100
            days = settings.subscription_period_days
            try:
                info = await provider.charge_saved(
                    telegram_id=driver.telegram_id,
                    payment_method_id=driver.payment_method_id,
                    amount_kop=amount,
                    days=days,
                    description=f"Подписка на {days} дн.",
                    idempotence_key=f"renew-{driver.telegram_id}-{sub.until:%Y%m%d%H}-{driver.renew_failures}",
                )
            except Exception as exc:  # noqa: BLE001 — сеть/провайдер: попробуем в следующий раз
                log.warning("Автосписание tg:%s не вышло: %s", driver.telegram_id, exc)
                problems.append((events.AUTORENEW_ERROR, f"🚨 Автосписание не удалось из-за сбоя ЮKassa: {exc}",
                                 driver.telegram_id, None, timedelta(minutes=30)))
                continue

            await billing.start_payment(
                session, telegram_id=driver.telegram_id, provider=provider.name, provider_payment_id=info.id,
                kind=KIND_AUTORENEW, amount_kop=amount, days=days,
            )
            if info.status == STATUS_SUCCEEDED:
                settlement = await apply_payment(session, provider.name, info)
                if settlement.credited:
                    charged += 1
                    await send(driver.telegram_id, paid_message(settlement.until))
            elif info.status == STATUS_CANCELED:
                await billing.mark_canceled(session, info.id)
                driver.renew_failures += 1
                if driver.renew_failures >= MAX_RENEW_FAILURES:
                    driver.auto_renew = False
                await session.commit()
                notes.append(driver.telegram_id)
                problems.append((
                    events.AUTORENEW_FAILED,
                    f"⚠️ Автосписание не прошло: {await events.who(driver.telegram_id)}, неудач подряд: "
                    f"{driver.renew_failures}" + (" (автопродление выключено)" if not driver.auto_renew else ""),
                    driver.telegram_id, info.id, None,
                ))
                await send(
                    driver.telegram_id,
                    "⚠️ Не удалось списать оплату подписки с сохранённой карты. "
                    "Оплатите вручную, чтобы не потерять доступ.",
                    _pay_markup(),
                )
            # pending: итог придёт вебхуком или сверкой
    for telegram_id in notes:
        await notifications.add(
            telegram_id, "Не удалось списать оплату",
            "Автопродление не сработало. Оплатите подписку вручную, чтобы не потерять доступ.",
            kind="subscription", url="/subscription",
        )
    for kind, text, telegram_id, provider_id, throttle in problems:
        await events.alert(kind, text, telegram_id=telegram_id, provider_id=provider_id, throttle=throttle)
    return charged


async def reconcile_pending(*, provider: Optional[PaymentProvider] = None, now: Optional[datetime] = None) -> int:
    """Сверяет с ЮKassa платежи, по которым ждём оплаты: вебхук мог потеряться, а водитель на сайт не вернуться.

    Оплаченные засчитываются (владельцу и водителю уходят сообщения), а платёж, который висит «в ожидании» дольше
    ``PENDING_TTL`` (10 минут), перестаёт «ждать оплаты» и помечается неоплаченным."""
    from app.services.payments import PaymentError, get_provider

    provider = provider or get_provider()
    now = now or now_utc_naive()
    if provider is None or not billing.enabled():
        return 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Payment.provider_payment_id, Payment.telegram_id, Payment.created_at).where(
                    Payment.status == PAYMENT_PENDING,
                    Payment.kind != KIND_ADMIN,
                    Payment.created_at > now - RECONCILE_WINDOW,
                    Payment.created_at < now - RECONCILE_AFTER,
                )
            )
        ).all()

    credited = 0
    for payment_id, _telegram_id, _created_at in rows:
        try:
            result = await settle(provider, payment_id)
        except PaymentError as exc:
            if exc.permanent:
                # ЮKassa не знает такого платежа (например, он создан в другом магазине): тревоги нет, платёж
                # просто перестанет ждать оплаты по истечении срока.
                log.info("Сверка: платёж %s ЮKassa не нашла (%s)", payment_id, exc)
                continue
            await events.alert(
                events.PROVIDER_ERROR, f"🚨 Сверка платежей: ЮKassa не отвечает или отказала ({exc}).",
                throttle=timedelta(minutes=30),
            )
            continue
        if result.credited:
            credited += 1
            if result.telegram_id is not None:
                await notify_paid(result.telegram_id, result.until)
            continue
    async with SessionLocal() as session:
        expired = await billing.expire_stale_pending(session, now=now)
    for payment in expired:
        await events.record(
            events.PAYMENT_CANCELED, telegram_id=payment.telegram_id, provider_id=payment.provider_payment_id,
            amount_kop=payment.amount_kop,
            details=f"не оплачен за {int(billing.PENDING_TTL.total_seconds() // 60)} минут",
        )
    return credited
