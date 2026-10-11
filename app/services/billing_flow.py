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
from app.models.payment import KIND_AUTORENEW
from app.services import billing
from app.services.payments import PaymentProvider, ProviderPayment
from app.services.payments.base import STATUS_CANCELED, STATUS_SUCCEEDED
from app.timeutil import now_utc_naive

log = logging.getLogger("billing")

#: За сколько дней до конца напоминаем.
REMIND_DAYS = 3
#: Автосписание делаем, когда до конца осталось не больше этого.
RENEW_AHEAD = timedelta(days=1)
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
    """Применяет состояние платежа от провайдера: оплачен — продлевает подписку, отменён — закрывает запись."""
    if info.status == STATUS_CANCELED:
        await billing.mark_canceled(session, info.id)
        return Settlement(False, info.telegram_id)
    if info.status != STATUS_SUCCEEDED:
        return Settlement(False, info.telegram_id)
    if info.telegram_id is None:
        log.error("Платёж %s оплачен, но без telegram_id в metadata — не начисляем", info.id)
        return Settlement(False)
    if info.amount_kop < settings.subscription_price_rub * 100:
        # Мы создаём платежи только на полную цену; меньшая сумма — чужой или подменённый платёж.
        log.error("Платёж %s на %s коп. меньше цены подписки — не начисляем", info.id, info.amount_kop)
        return Settlement(False, info.telegram_id)

    _payment, credited = await billing.record_success(
        session,
        telegram_id=info.telegram_id,
        provider=provider_name,
        provider_payment_id=info.id,
        kind=info.kind or "sbp",
        amount_kop=info.amount_kop,
        days=_plan_days(info),
        payment_method_id=info.payment_method_id,
    )
    driver = await billing.get_driver(session, info.telegram_id)
    return Settlement(credited, info.telegram_id, driver.paid_until if driver else None)


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
                await send(
                    driver.telegram_id,
                    "⚠️ Не удалось списать оплату подписки с сохранённой карты. "
                    "Оплатите вручную, чтобы не потерять доступ.",
                    _pay_markup(),
                )
            # pending: итог придёт вебхуком или сверкой
    return charged
