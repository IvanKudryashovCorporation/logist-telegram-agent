"""Журнал событий подписки и уведомления владельцу в Telegram.

Зачем: деньги — то место, где ошибку нельзя узнать «когда-нибудь потом». Каждое значимое событие (создан платёж,
пришло уведомление, дни начислены, возврат, автосписание, сбой провайдера, подозрительный платёж) попадает:

* в лог сервиса (``journalctl``) строкой ``billing[вид] …``;
* в таблицу ``billing_events`` (её читает страница ``/admin/billing``);
* владельцу в Telegram — для важного: оплата, возврат, автосписание, а также всё, что похоже на ошибку.

Всё здесь безопасно для вызывающего кода: исключения не пробрасываются, сбой журнала или Telegram не должен
ронять обработку платежа.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import SessionLocal
from app.models import BillingEvent, Driver
from app.timeutil import now_utc_naive

log = logging.getLogger("billing.events")

# Виды событий.
PAYMENT_CREATED = "payment_created"
PAYMENT_CREATE_FAILED = "payment_create_failed"
PAYMENT_SUCCEEDED = "payment_succeeded"
PAYMENT_DUPLICATE = "payment_duplicate"
PAYMENT_CANCELED = "payment_canceled"
PAYMENT_ANOMALY = "payment_anomaly"
REFUND_SUCCEEDED = "refund_succeeded"
REFUND_IGNORED = "refund_ignored"
REFUND_DUPLICATE = "refund_duplicate"
WEBHOOK_RECEIVED = "webhook_received"
WEBHOOK_ERROR = "webhook_error"
PROVIDER_ERROR = "provider_error"
AUTORENEW_CHARGED = "autorenew_charged"
AUTORENEW_FAILED = "autorenew_failed"
AUTORENEW_ERROR = "autorenew_error"
AUTORENEW_OFF = "autorenew_off"
REMINDER_SENT = "reminder_sent"
ADMIN_GRANT = "admin_grant"
ADMIN_RESET = "admin_reset"

#: Подписи для админки.
KIND_LABELS = {
    PAYMENT_CREATED: "платёж создан",
    PAYMENT_CREATE_FAILED: "не удалось создать платёж",
    PAYMENT_SUCCEEDED: "оплата получена",
    PAYMENT_DUPLICATE: "повтор уведомления об оплате",
    PAYMENT_CANCELED: "платёж отменён",
    PAYMENT_ANOMALY: "подозрительный платёж",
    REFUND_SUCCEEDED: "возврат",
    REFUND_IGNORED: "возврат не засчитан",
    REFUND_DUPLICATE: "повтор уведомления о возврате",
    WEBHOOK_RECEIVED: "уведомление ЮKassa",
    WEBHOOK_ERROR: "ошибка обработки уведомления",
    PROVIDER_ERROR: "ЮKassa недоступна",
    AUTORENEW_CHARGED: "автосписание",
    AUTORENEW_FAILED: "автосписание не прошло",
    AUTORENEW_ERROR: "ошибка автосписания",
    AUTORENEW_OFF: "автопродление выключено",
    REMINDER_SENT: "напоминание водителю",
    ADMIN_GRANT: "дни добавлены админом",
    ADMIN_RESET: "доступ обнулён админом",
}

#: События-проблемы: в админке подсвечиваются.
PROBLEM_KINDS = frozenset({
    PAYMENT_CREATE_FAILED, PAYMENT_ANOMALY, REFUND_IGNORED, WEBHOOK_ERROR, PROVIDER_ERROR,
    AUTORENEW_FAILED, AUTORENEW_ERROR,
})


def _rub(amount_kop: Optional[int]) -> str:
    return f"{(amount_kop or 0) / 100:.0f} ₽"


async def record(
    kind: str, *, telegram_id: Optional[int] = None, provider_id: Optional[str] = None,
    amount_kop: Optional[int] = None, details: str = "", session: Optional[AsyncSession] = None,
) -> None:
    """Пишет событие в лог и в журнал. Никогда не бросает исключение.

    ``session`` — если передана, строка добавляется в неё (коммитит вызывающий), иначе открывается своя."""
    log.info("billing[%s] tg=%s id=%s amount=%s %s", kind, telegram_id, provider_id, amount_kop, details)
    event = BillingEvent(
        kind=kind, telegram_id=telegram_id, provider_id=provider_id, amount_kop=amount_kop, details=details[:1000],
    )
    try:
        if session is not None:
            session.add(event)
            return
        async with SessionLocal() as own:
            own.add(event)
            await own.commit()
    except Exception:
        log.exception("Не удалось записать событие подписки %s", kind)


async def seen(kind: str, provider_id: str) -> bool:
    """Было ли уже такое событие по этому платежу (для «один раз сообщить»)."""
    try:
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(BillingEvent.id).where(BillingEvent.kind == kind, BillingEvent.provider_id == provider_id)
                )
            ).first() is not None
    except Exception:
        log.exception("Не удалось прочитать журнал подписки")
        return False


async def _recently(kind: str, within: timedelta) -> bool:
    try:
        async with SessionLocal() as session:
            return (
                await session.execute(
                    select(BillingEvent.id).where(
                        BillingEvent.kind == kind, BillingEvent.created_at > now_utc_naive() - within
                    )
                )
            ).first() is not None
    except Exception:  # noqa: BLE001
        return False


async def who(telegram_id: Optional[int]) -> str:
    """Человекочитаемо: «Иван @ivan (id 123)»."""
    if telegram_id is None:
        return "неизвестный пользователь"
    try:
        async with SessionLocal() as session:
            driver = (
                await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        driver = None
    if driver is None:
        return f"id {telegram_id}"
    name = " ".join(part for part in (driver.first_name, driver.last_name) if part)
    handle = f"@{driver.username}" if driver.username else ""
    return f"{' '.join(part for part in (name, handle) if part) or 'без имени'} (id {telegram_id})"


async def notify_owner(text: str) -> bool:
    """Сообщение владельцу (``ALERT_CHAT_ID``) от бота-сторожа. ``False`` — не отправлено; не бросает."""
    chat = settings.alert_chat_id.strip()
    if not chat.lstrip("-").isdigit():
        return False  # владелец не настроен: всё остаётся в логе и журнале
    try:
        from app.services.subscriptions import OK, send_message

        outcome = await send_message(int(chat), text)
        return outcome == OK
    except Exception:
        log.exception("Не удалось отправить уведомление владельцу")
        return False


async def alert(
    kind: str, text: str, *, telegram_id: Optional[int] = None, provider_id: Optional[str] = None,
    amount_kop: Optional[int] = None, details: str = "", throttle: Optional[timedelta] = None,
) -> None:
    """Событие в журнал + сообщение владельцу. ``throttle`` — не слать повторно такой же вид раньше срока
    (чтобы лежащая ЮKassa не завалила сообщениями); в журнал событие пишется всегда."""
    quiet = throttle is not None and await _recently(kind, throttle)
    await record(kind, telegram_id=telegram_id, provider_id=provider_id, amount_kop=amount_kop, details=details or text)
    if not quiet:
        await notify_owner(text)


# --- Готовые сообщения -----------------------------------------------------------------------------------------


def _date(moment: Optional[datetime]) -> str:
    return moment.strftime("%d.%m.%Y") if moment else "—"


async def payment_message(*, telegram_id: int, kind: str, amount_kop: int, days: int, until: Optional[datetime],
                          payment_id: str) -> str:
    title = "🔁 Автопродление" if kind == "autorenew" else "💰 Оплата подписки"
    method = {"sbp": "СБП", "card": "банковская карта", "autopay": "карта (автоплатёж)", "any": "другой способ",
              "autorenew": "списание с сохранённой карты"}.get(kind, kind)
    return (
        f"{title}\n{await who(telegram_id)}\n{_rub(amount_kop)} · {method} · {days} дн.\n"
        f"Оплачено до {_date(until)}\nПлатёж {payment_id}"
    )


async def refund_message(*, telegram_id: int, amount_kop: int, days: int, until: Optional[datetime],
                         refund_id: str) -> str:
    return (
        f"↩️ Возврат платежа\n{await who(telegram_id)}\n{_rub(amount_kop)} · снято {days} дн.\n"
        f"Теперь оплачено до {_date(until)}\nВозврат {refund_id}"
    )
