"""Платная подписка: пробный период, оплаченные дни и доступ к функциям.

Правило одно: доступ есть, пока ``now < max(конец пробного периода, paid_until)``.

* Подписка выключена (``SUBSCRIPTION_LAUNCH_DATE`` пуст) — доступ у всех, плашки нет.
* Пробный период: новым водителям ``trial_ends_at`` ставится при регистрации (+``TRIAL_DAYS``); у тех,
  кто зарегистрирован до запуска (поле пусто), он считается от даты запуска.
* Оплата продлевает от ``max(now, текущий конец доступа)``: остаток пробного периода и уже оплаченные
  дни не сгорают.
* Админы (``ADMIN_TELEGRAM_IDS``) не ограничиваются никогда.

Закрытие функций делают вызывающие: сайт (``app.web.routes``), бот (``app.web.bot_orders``) и рассылка
уведомлений (``app.services.subscriptions``). Здесь только вопрос «есть ли доступ и сколько дней».
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Driver, Payment, Refund
from app.models.payment import (
    KIND_ADMIN,
    PAYMENT_CANCELED,
    PAYMENT_PENDING,
    PAYMENT_REFUNDED,
    PAYMENT_SUCCEEDED,
)
from app.timeutil import now_utc_naive

STATE_OFF = "off"
STATE_ADMIN = "admin"
STATE_TRIAL = "trial"
STATE_PAID = "paid"
STATE_EXPIRED = "expired"

#: Состояния, в которых функции открыты.
_OPEN_STATES = frozenset({STATE_OFF, STATE_ADMIN, STATE_TRIAL, STATE_PAID})


def launch_at() -> Optional[datetime]:
    """Момент запуска подписки (начало суток) или ``None``, если она выключена/дата некорректна."""
    raw = (settings.subscription_launch_date or "").strip()
    if not raw:
        return None
    try:
        return datetime.combine(date.fromisoformat(raw), time.min)
    except ValueError:
        return None


def enabled() -> bool:
    return launch_at() is not None


def new_driver_trial_end(now: Optional[datetime] = None) -> Optional[datetime]:
    """Конец пробного периода для только что зарегистрированного водителя.

    Пока подписка выключена, он пустой: тогда водителю достанется пробный период от даты запуска."""
    if not enabled():
        return None
    return (now or now_utc_naive()) + timedelta(days=max(0, settings.trial_days))


def trial_end(driver: Optional[Driver]) -> Optional[datetime]:
    if driver is not None and driver.trial_ends_at is not None:
        return driver.trial_ends_at
    launch = launch_at()
    if launch is None:
        return None
    return launch + timedelta(days=max(0, settings.trial_days))


def access_until(driver: Optional[Driver]) -> Optional[datetime]:
    """До какого момента открыт доступ (``None`` — подписка выключена)."""
    base = trial_end(driver)
    paid = driver.paid_until if driver is not None else None
    if base is None and paid is None:
        return None
    return max(value for value in (base, paid) if value is not None)


@dataclass(frozen=True)
class Subscription:
    state: str
    #: Сколько дней осталось (округление вверх: «осталось 3 часа» — это ещё 1 день).
    days: int
    until: Optional[datetime]

    @property
    def has_access(self) -> bool:
        return self.state in _OPEN_STATES

    @property
    def shows_badge(self) -> bool:
        """Плашка «Дней подписки: N» нужна всем вошедшим, кроме случая выключенной подписки (у админа — «∞»)."""
        return self.state in (STATE_ADMIN, STATE_TRIAL, STATE_PAID, STATE_EXPIRED)


def describe(
    driver: Optional[Driver], telegram_id: Optional[int] = None, now: Optional[datetime] = None
) -> Subscription:
    """Состояние подписки водителя на момент ``now``."""
    now = now or now_utc_naive()
    if not enabled():
        return Subscription(STATE_OFF, 0, None)
    tg = telegram_id if telegram_id is not None else (driver.telegram_id if driver is not None else None)
    if tg is not None and tg in settings.admin_ids:
        return Subscription(STATE_ADMIN, 0, None)

    until = access_until(driver)
    if until is None or until <= now:
        return Subscription(STATE_EXPIRED, 0, until)
    days = max(1, math.ceil((until - now).total_seconds() / 86400))
    base = trial_end(driver)
    # «Оплачено» — только срок за пределами пробного периода (после возврата остаток триала снова «пробный»).
    paid = (
        driver is not None and driver.paid_until is not None and driver.paid_until > now
        and (base is None or driver.paid_until > base)
    )
    return Subscription(STATE_PAID if paid else STATE_TRIAL, days, until)


async def get_driver(session: AsyncSession, telegram_id: int) -> Optional[Driver]:
    return (
        await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
    ).scalar_one_or_none()


async def subscription_for(
    session: AsyncSession, telegram_id: Optional[int], now: Optional[datetime] = None
) -> Subscription:
    """Подписка по id Telegram. Гостю (``None``) плашка не нужна: он ничего не закрывает."""
    if telegram_id is None:
        return Subscription(STATE_OFF, 0, None)
    return describe(await get_driver(session, telegram_id), telegram_id, now)


async def has_access_for(session: AsyncSession, telegram_id: int, now: Optional[datetime] = None) -> bool:
    return (await subscription_for(session, telegram_id, now)).has_access


async def access_map(
    session: AsyncSession, telegram_ids: Iterable[int], now: Optional[datetime] = None
) -> dict[int, bool]:
    """Есть ли доступ у каждого из водителей — одним запросом (для рассылки уведомлений)."""
    ids = set(telegram_ids)
    if not ids:
        return {}
    if not enabled():
        return {tg: True for tg in ids}
    drivers = {
        driver.telegram_id: driver
        for driver in (await session.execute(select(Driver).where(Driver.telegram_id.in_(ids)))).scalars().all()
    }
    return {tg: describe(drivers.get(tg), tg, now).has_access for tg in ids}


def extend(driver: Driver, days: int, now: Optional[datetime] = None) -> datetime:
    """Продлевает оплаченный доступ на ``days`` дней поверх остатка пробного периода и оплат."""
    now = now or now_utc_naive()
    current = access_until(driver)
    start = max(now, current) if current is not None else now
    driver.paid_until = start + timedelta(days=days)
    driver.renew_failures = 0
    return driver.paid_until


async def start_payment(
    session: AsyncSession, *, telegram_id: int, provider: str, provider_payment_id: str, kind: str,
    amount_kop: int, days: int,
) -> Payment:
    """Записывает созданный у провайдера платёж как «ожидает оплаты» (повторный вызов ничего не меняет)."""
    existing = (
        await session.execute(select(Payment).where(Payment.provider_payment_id == provider_payment_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    payment = Payment(
        telegram_id=telegram_id, provider=provider, provider_payment_id=provider_payment_id, kind=kind,
        amount_kop=amount_kop, days=days, status=PAYMENT_PENDING,
    )
    session.add(payment)
    await session.commit()
    return payment


async def record_success(
    session: AsyncSession, *, telegram_id: int, provider: str, provider_payment_id: str, kind: str,
    amount_kop: int, days: int, payment_method_id: Optional[str] = None, now: Optional[datetime] = None,
) -> tuple[Optional[Payment], bool]:
    """Засчитывает оплату и продлевает подписку. Идемпотентно по ``provider_payment_id``.

    Возвращает ``(платёж, начислено_ли_сейчас)``: вебхук и перепроверка могут прийти повторно, и второй раз
    дни не добавляются. Если водителя нет (удалён) — ``(None, False)``."""
    now = now or now_utc_naive()
    driver = await get_driver(session, telegram_id)
    if driver is None:
        return None, False

    payment = (
        await session.execute(select(Payment).where(Payment.provider_payment_id == provider_payment_id))
    ).scalar_one_or_none()
    if payment is not None and payment.status == PAYMENT_SUCCEEDED:
        return payment, False
    if payment is None:
        payment = Payment(
            telegram_id=telegram_id, provider=provider, provider_payment_id=provider_payment_id, kind=kind,
            amount_kop=amount_kop, days=days,
        )
        session.add(payment)
    payment.status = PAYMENT_SUCCEEDED
    payment.paid_at = now
    payment.days = days
    payment.amount_kop = amount_kop

    extend(driver, days, now)
    if payment_method_id and kind in ("autopay", "autorenew"):
        driver.payment_method_id = payment_method_id
        driver.auto_renew = True
    await session.commit()
    return payment, True


async def record_refund(
    session: AsyncSession, *, provider_refund_id: str, provider_payment_id: str, amount_kop: int,
) -> tuple[Optional[Refund], int]:
    """Засчитывает возврат: снимает с оплаченного срока дни, соответствующие возвращённой сумме.

    Возвращает ``(возврат, снято_дней)``. Идемпотентно по ``provider_refund_id``: повторное уведомление ничего
    не снимает. Возврат по неизвестному платежу (или платежу, которого мы не засчитывали) игнорируется.
    Полный возврат снимает все дни платежа, частичный — пропорционально сумме."""
    existing = (
        await session.execute(select(Refund).where(Refund.provider_refund_id == provider_refund_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, 0
    payment = (
        await session.execute(select(Payment).where(Payment.provider_payment_id == provider_payment_id))
    ).scalar_one_or_none()
    if payment is None or payment.status not in (PAYMENT_SUCCEEDED, PAYMENT_REFUNDED) or payment.amount_kop <= 0:
        return None, 0

    done_kop, done_days = (
        await session.execute(
            select(func.coalesce(func.sum(Refund.amount_kop), 0), func.coalesce(func.sum(Refund.days), 0)).where(
                Refund.provider_payment_id == provider_payment_id
            )
        )
    ).one()
    total_kop = int(done_kop) + amount_kop
    left = max(0, payment.days - int(done_days))
    # Полный возврат снимает всё, что ещё не снято; частичный — дни пропорционально сумме.
    days = left if total_kop >= payment.amount_kop else min(left, round(payment.days * amount_kop / payment.amount_kop))

    refund = Refund(
        provider_refund_id=provider_refund_id, provider_payment_id=provider_payment_id,
        telegram_id=payment.telegram_id, amount_kop=amount_kop, days=days,
    )
    session.add(refund)
    if total_kop >= payment.amount_kop:
        payment.status = PAYMENT_REFUNDED
    driver = await get_driver(session, payment.telegram_id)
    if driver is not None and driver.paid_until is not None and days:
        driver.paid_until = driver.paid_until - timedelta(days=days)
    await session.commit()
    return refund, days


#: Сколько платёж ждёт оплаты. Не оплатили за это время — в истории он перестаёт висеть «в ожидании»
#: (если водитель всё-таки оплатит позже, вебхук или сверка зачтёт платёж как обычно).
PENDING_TTL = timedelta(minutes=10)


async def expire_stale_pending(
    session: AsyncSession, *, telegram_id: Optional[int] = None, now: Optional[datetime] = None
) -> list[Payment]:
    """Помечает неоплаченными платежи, которые ждут оплаты дольше ``PENDING_TTL``. Возвращает их список."""
    cutoff = (now or now_utc_naive()) - PENDING_TTL
    query = select(Payment).where(
        Payment.status == PAYMENT_PENDING, Payment.created_at < cutoff, Payment.kind != KIND_ADMIN
    )
    if telegram_id is not None:
        query = query.where(Payment.telegram_id == telegram_id)
    stale = list((await session.execute(query)).scalars().all())
    for payment in stale:
        payment.status = PAYMENT_CANCELED
    if stale:
        await session.commit()
    return stale


async def mark_canceled(session: AsyncSession, provider_payment_id: str) -> None:
    payment = (
        await session.execute(select(Payment).where(Payment.provider_payment_id == provider_payment_id))
    ).scalar_one_or_none()
    if payment is not None and payment.status == PAYMENT_PENDING:
        payment.status = PAYMENT_CANCELED
        await session.commit()


async def grant_days(
    session: AsyncSession, telegram_id: int, days: int, *, now: Optional[datetime] = None, note: str = ""
) -> Optional[datetime]:
    """Ручное начисление дней админом (запись в ``payments`` с ``kind='admin'``)."""
    import uuid

    from app.models.payment import KIND_ADMIN

    _, credited = await record_success(
        session, telegram_id=telegram_id, provider="admin", provider_payment_id=f"admin-{uuid.uuid4().hex[:24]}",
        kind=KIND_ADMIN, amount_kop=0, days=days, now=now,
    )
    if not credited:
        return None
    return (await get_driver(session, telegram_id)).paid_until


async def reset_access(session: AsyncSession, telegram_id: int, *, now: Optional[datetime] = None) -> bool:
    """Обнуляет доступ (пробный период и оплату) — «подписка закончилась» прямо сейчас."""
    driver = await get_driver(session, telegram_id)
    if driver is None:
        return False
    driver.trial_ends_at = now or now_utc_naive()
    driver.paid_until = None
    driver.auto_renew = False
    await session.commit()
    return True
