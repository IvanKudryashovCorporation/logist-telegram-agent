"""Уведомления водителям о новых заказах по их фильтру.

Фоновый воркер раз в ``SUBSCRIPTION_POLL_SECONDS`` секунд берёт свежие свободные
заказы, проверяет каждый по фильтру каждой активной подписки (тем же
``Filters.matches``, что и лента сайта, включая радиус) и пишет водителю через
бота со ссылкой на заказ. Повторно по одному заказу не пишем (таблица
``subscription_notifications``). Если за проход подходит много заказов — одно
сообщение со списком, а не поток.

Подписка с радиусом ждёт координат заказа (геокодер ставит их сам), но не
дольше ``GEO_WAIT``: иначе заказ без координат для неё невидим. Обычная подписка
(по названию города, цене и т.п.) ничего не ждёт.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import Order, OrderSubscription, SubscriptionNotification
from app.timeutil import now_msk_naive, now_utc_naive
from app.web import queries
from app.web.filters import VEHICLE_CHOICES, Filters
from app.web.presenters import pickup_label

log = logging.getLogger("app.subscriptions")
logging.getLogger("httpx").setLevel(logging.WARNING)  # в адресе Bot API лежит токен

#: Не старше скольки минут заказ ещё может быть разослан (хватает на повтор при сбое).
WINDOW = timedelta(minutes=30)
#: Сколько ждём координаты, прежде чем проверять заказ без них.
GEO_WAIT = timedelta(seconds=90)

OK, BLOCKED, RETRY = "ok", "blocked", "retry"
Sender = Callable[[int, str], Awaitable[str]]


# --- Фильтр -> текст и ссылка -------------------------------------------------


def describe_filters(params: dict) -> list[str]:
    """Человекочитаемое описание сохранённого фильтра для профиля и сообщений."""
    lines: list[str] = []

    def place(label: str, city: str, radius: str) -> None:
        if city:
            lines.append(f"{label}: {city}" + (f" (+{radius} км)" if radius else ""))

    place("Откуда", params.get("from_city", ""), params.get("from_radius", ""))
    place("Куда", params.get("to_city", ""), params.get("to_radius", ""))
    if params.get("vehicle") in VEHICLE_CHOICES:
        lines.append(f"Тип авто: {VEHICLE_CHOICES[params['vehicle']].lower()}")
    if params.get("date_from") or params.get("date_to"):
        lines.append(f"Дата: {params.get('date_from') or '…'} — {params.get('date_to') or '…'}")
    if params.get("time_from") or params.get("time_to"):
        lines.append(f"Время: {params.get('time_from') or '…'} — {params.get('time_to') or '…'}")
    if params.get("price_min") or params.get("price_max"):
        lines.append(f"Цена: от {params.get('price_min') or '0'} до {params.get('price_max') or '∞'} ₽")
    return lines


def has_route(params: dict) -> bool:
    """Подписка без города прислала бы весь поток группы — такую не заводим."""
    return bool(params.get("from_city") or params.get("to_city"))


def feed_path(params: dict) -> str:
    """Относительная ссылка на ленту с этим фильтром (для сайта)."""
    query = {key: value for key, value in params.items() if value}
    return f"/?{urlencode(query)}" if query else "/"


def feed_link(params: dict) -> str:
    """Абсолютная ссылка на ленту с этим фильтром (для сообщений бота)."""
    return f"{settings.public_base_url.rstrip('/')}{feed_path(params)}"


async def get_subscription(session, telegram_id: int) -> Optional[OrderSubscription]:
    return (
        await session.execute(
            select(OrderSubscription).where(OrderSubscription.telegram_id == telegram_id)
        )
    ).scalar_one_or_none()


async def save_subscription(session, telegram_id: int, params: dict) -> OrderSubscription:
    """Создаёт или обновляет подписку водителя и включает её.

    ``since`` сдвигается на «сейчас»: после смены фильтра присылаем только то, что
    появится дальше, а не то, что водитель уже видит в ленте.
    """
    now = now_utc_naive()
    sub = await get_subscription(session, telegram_id)
    if sub is None:
        sub = OrderSubscription(telegram_id=telegram_id, params=params, since=now)
        session.add(sub)
    sub.params = params
    sub.is_active = True
    sub.since = now
    sub.error = None
    await session.commit()
    return sub


async def set_subscription_active(session, telegram_id: int, active: bool) -> Optional[OrderSubscription]:
    sub = await get_subscription(session, telegram_id)
    if sub is None:
        return None
    if active and not sub.is_active:
        sub.since = now_utc_naive()  # включили заново — старое не досылаем
        sub.error = None
    sub.is_active = active
    await session.commit()
    return sub


# --- Сообщения -------------------------------------------------------------------


def order_line(order: Order) -> str:
    route = f"{order.from_city or '?'} → {order.to_city or '?'}"
    extras = [pickup_label(order)]
    if order.client_price:
        extras.append(f"{order.client_price:.0f} ₽")
    if order.passengers:
        extras.append(f"{order.passengers} пасс.")
    return f"{route}\n   {' · '.join(extras)}"


def order_url(order: Order) -> str:
    return f"{settings.public_base_url.rstrip('/')}/orders/{order.id}"


def build_message(orders: list[Order], params: dict) -> str:
    if len(orders) == 1:
        order = orders[0]
        return f"🔔 Новый заказ по вашему фильтру\n\n{order_line(order)}\n\n{order_url(order)}"

    limit = settings.subscription_max_lines
    lines = [f"🔔 Новых заказов по вашему фильтру: {len(orders)}", ""]
    for order in orders[:limit]:
        lines.append(f"• {order_line(order)}\n  {order_url(order)}")
    if len(orders) > limit:
        lines.append(f"\n…и ещё {len(orders) - limit}. Все в ленте: {feed_link(params)}")
    return "\n".join(lines)


# --- Отправка ---------------------------------------------------------------------


def bot_token() -> str:
    return settings.alert_bot_token.strip() or settings.telegram_login_bot_token.strip()


async def send_message(chat_id: int, text: str) -> str:
    """Отправляет сообщение через Bot API. Возвращает ``ok`` / ``blocked`` / ``retry``.

    ``blocked`` — бот не может писать этому человеку (не нажал «Старт», заблокировал
    бота): подписку выключаем. ``retry`` — временный сбой, попробуем в следующий проход.
    """
    token = bot_token()
    if not token:
        log.error("Нет токена бота для уведомлений")
        return RETRY
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
    except httpx.HTTPError as exc:
        log.warning("Telegram недоступен: %s", type(exc).__name__)
        return RETRY
    if response.status_code == 200:
        return OK
    if response.status_code in (400, 403):
        log.info("Бот не может писать tg:%s: %s", chat_id, response.text[:120])
        return BLOCKED
    log.warning("Telegram ответил %s: %s", response.status_code, response.text[:120])
    return RETRY


# --- Подбор заказов ------------------------------------------------------------------


async def _prepare_filters(session, sub: OrderSubscription, cache: dict) -> Filters:
    filters = Filters(**{key: str(value) for key, value in (sub.params or {}).items()})
    for side, city, radius in (
        ("from", filters.from_city, filters.from_radius),
        ("to", filters.to_city, filters.to_radius),
    ):
        if not (radius and city):
            continue
        if city not in cache:
            cache[city] = (await geo.centers_for(session, city))[0]
        setattr(filters, f"{side}_centers", cache[city])
    return filters


def _ready(order: Order, needs_geo: bool, now: datetime) -> bool:
    """Радиус считается по координатам, которые геокодер ставит с задержкой:
    подождём их, но не дольше GEO_WAIT. Обычным подпискам ждать нечего."""
    return not needs_geo or order.geo_checked_at is not None or order.created_at <= now - GEO_WAIT


async def notify_once(*, send: Sender = send_message, now: Optional[datetime] = None) -> int:
    """Один проход: подбирает заказы под подписки и отправляет. Возвращает число сообщений."""
    now = now or now_utc_naive()
    sent_messages = 0

    async with SessionLocal() as session:
        subs = list(
            (
                await session.execute(
                    select(OrderSubscription).where(OrderSubscription.is_active.is_(True))
                )
            ).scalars().all()
        )
        if not subs:
            return 0

        candidates = list(
            (
                await session.execute(
                    select(Order)
                    .where(*queries.feed_conditions(now=now_msk_naive()), Order.created_at > now - WINDOW)
                    .order_by(Order.id.asc())
                )
            ).scalars().all()
        )
        orders = candidates
        if not orders:
            return 0

        already = {
            (row.subscription_id, row.order_id)
            for row in (
                await session.execute(
                    select(SubscriptionNotification).where(
                        SubscriptionNotification.order_id.in_([o.id for o in orders])
                    )
                )
            ).scalars().all()
        }

        centers_cache: dict = {}
        for sub in subs:
            filters = await _prepare_filters(session, sub, centers_cache)
            needs_geo = bool(
                (filters.from_radius and filters.from_city) or (filters.to_radius and filters.to_city)
            )

            matched = [
                order for order in orders
                if order.created_at > sub.since
                and (sub.id, order.id) not in already
                and _ready(order, needs_geo, now)
                and filters.matches(order)
            ]
            if not matched:
                continue

            outcome = await send(sub.telegram_id, build_message(matched, sub.params or {}))
            if outcome == OK:
                for order in matched:
                    session.add(SubscriptionNotification(subscription_id=sub.id, order_id=order.id, sent_at=now))
                sub.last_notified_at = now
                sent_messages += 1
            elif outcome == BLOCKED:
                sub.is_active = False
                sub.error = "Бот не может написать вам: откройте бота и нажмите «Старт»"
            await session.commit()

    return sent_messages


async def run_subscription_worker(stop_event: asyncio.Event) -> None:
    """Фоновый цикл воркера уведомлений."""
    if not bot_token():
        log.info("Уведомления о заказах выключены: нет токена бота")
        return
    interval = max(10, settings.subscription_poll_seconds)
    log.info("Уведомления по фильтрам запущены: проход раз в %s с", interval)
    while not stop_event.is_set():
        try:
            sent = await notify_once()
            if sent:
                log.info("Отправлено уведомлений о заказах: %s", sent)
        except Exception:
            log.exception("Ошибка воркера уведомлений")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
