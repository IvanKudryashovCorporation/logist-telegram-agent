"""Уведомления водителям о новых заказах по их фильтру.

Фоновый воркер раз в ``SUBSCRIPTION_POLL_SECONDS`` секунд берёт свежие свободные
заказы, проверяет каждый по фильтру каждой активной подписки (тем же
``Filters.matches``, что и лента сайта, включая радиус) и пишет водителю через
бота. Каждый заказ — отдельное сообщение с кнопками «Написать диспетчеру» (сразу чат с ним) и
«Взять заказ» (обработка нажатия — :mod:`app.web.bot_orders`). Повторно по одному заказу не пишем
(таблица ``subscription_notifications``). Если за проход подходит очень много заказов, первые
``SUBSCRIPTION_MAX_LINES`` приходят отдельными сообщениями, а про остальные — одно «и ещё N».

Номер отправленного сообщения запоминается: если диспетчер потом удалил заявку в
Telegram, сообщение у водителя удаляется (а в списке из нескольких заказов — переписывается
без неё), см. :func:`retract_withdrawn`.

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
from sqlalchemy import exists, select

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    ActionLog,
    Order,
    OrderStatus,
    OrderSubscription,
    SubscriptionNotification,
    WorkGroup,
)
from app.timeutil import now_msk_naive, now_utc_naive
from app.web import queries
from app.web.filters import VEHICLE_CHOICES, Filters
from app.web.presenters import dispatcher_link, dispatcher_message, pickup_label

log = logging.getLogger("app.subscriptions")
logging.getLogger("httpx").setLevel(logging.WARNING)  # в адресе Bot API лежит токен

#: Не старше скольки минут заказ ещё может быть разослан (хватает на повтор при сбое).
WINDOW = timedelta(minutes=30)
#: Сколько ждём координаты, прежде чем проверять заказ без них.
GEO_WAIT = timedelta(seconds=90)

OK, BLOCKED, RETRY = "ok", "blocked", "retry"
#: Сообщение уже нечем убирать: его нет, прошло слишком много времени или бот заблокирован.
GONE = "gone"
Sender = Callable[..., Awaitable[str]]
#: Bot API разрешает удалять свои сообщения не позже чем через 48 часов; берём с запасом.
RETRACT_WINDOW = timedelta(hours=47)
#: Действия журнала, после которых заявка считается снятой самим диспетчером.
WITHDRAWN_ACTIONS = ("cancelled_message_deleted",)


class SendOutcome(str):
    """Итог отправки: ведёт себя как обычная строка ``ok``/``blocked``/``retry``, но у успешной
    отправки несёт ещё и номер сообщения бота."""

    message_id: Optional[int] = None

    def __new__(cls, value: str, message_id: Optional[int] = None):
        obj = super().__new__(cls, value)
        obj.message_id = message_id
        return obj


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


#: Параметры, по которым заказ отсеивается. Радиус сам по себе ничего не фильтрует (он расширяет город).
CRITERIA = (
    "from_city", "to_city", "vehicle", "date_from", "date_to", "time_from", "time_to", "price_min", "price_max",
)


def has_criteria(params: dict) -> bool:
    """Фильтр хоть что-то отсеивает: город, тип авто, дата, время или цена.

    Полностью пустой фильтр прислал бы весь поток заявок, такой не сохраняем. Фильтр без города
    (только цена или тип авто) разрешён: водитель сам выбирает, чем он ловит заказы."""
    return any(params.get(key) for key in CRITERIA)


def feed_path(params: dict) -> str:
    """Относительная ссылка на ленту с этим фильтром (для сайта)."""
    query = {key: value for key, value in params.items() if value}
    return f"/?{urlencode(query)}" if query else "/"


def feed_link(params: dict) -> str:
    """Абсолютная ссылка на ленту с этим фильтром (для сообщений бота)."""
    return f"{settings.public_base_url.rstrip('/')}{feed_path(params)}"


ADDED, EXISTS, LIMIT = "added", "exists", "limit"


async def list_subscriptions(session, telegram_id: int) -> list[OrderSubscription]:
    """Все сохранённые фильтры водителя, старые первыми."""
    return list(
        (
            await session.execute(
                select(OrderSubscription)
                .where(OrderSubscription.telegram_id == telegram_id)
                .order_by(OrderSubscription.id)
            )
        ).scalars().all()
    )


async def get_subscription(session, telegram_id: int, sub_id: int) -> Optional[OrderSubscription]:
    """Фильтр по номеру — только если он принадлежит этому водителю."""
    return (
        await session.execute(
            select(OrderSubscription).where(
                OrderSubscription.id == sub_id, OrderSubscription.telegram_id == telegram_id
            )
        )
    ).scalar_one_or_none()


async def add_subscription(
    session, telegram_id: int, params: dict
) -> tuple[Optional[OrderSubscription], str]:
    """Сохраняет фильтр водителя и включает по нему уведомления.

    Такой же фильтр уже есть — не плодим копию, а включаем существующий (``exists``). Больше
    :data:`app.config.Settings.max_filters_per_driver` фильтров не бывает (``limit``).
    ``since`` ставится на «сейчас»: присылаем только то, что появится дальше, а не то, что
    водитель уже видит в ленте.
    """
    now = now_utc_naive()
    existing = await list_subscriptions(session, telegram_id)
    for sub in existing:
        if sub.params == params:
            sub.is_active = True
            sub.since = now
            sub.error = None
            await session.commit()
            return sub, EXISTS
    if len(existing) >= max(1, settings.max_filters_per_driver):
        return None, LIMIT
    sub = OrderSubscription(telegram_id=telegram_id, params=params, since=now)
    session.add(sub)
    await session.commit()
    return sub, ADDED


async def set_subscription_active(
    session, telegram_id: int, sub_id: int, active: bool
) -> Optional[OrderSubscription]:
    sub = await get_subscription(session, telegram_id, sub_id)
    if sub is None:
        return None
    if active and not sub.is_active:
        sub.since = now_utc_naive()  # включили заново — старое не досылаем
        sub.error = None
    sub.is_active = active
    await session.commit()
    return sub


async def delete_subscription(session, telegram_id: int, sub_id: int) -> bool:
    sub = await get_subscription(session, telegram_id, sub_id)
    if sub is None:
        return False
    await session.delete(sub)
    await session.commit()
    return True


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
    """Текст уведомления. Ссылок на заказы в нём нет: действовать водителю помогают кнопки."""
    if len(orders) == 1:
        return f"🔔 Новый заказ по вашему фильтру\n\n{order_line(orders[0])}"

    limit = settings.subscription_max_lines
    lines = [f"🔔 Новых заказов по вашему фильтру: {len(orders)}", ""]
    for number, order in enumerate(orders[:limit], 1):
        lines.append(f"{number}. {order_line(order)}")
    if len(orders) > limit:
        lines.append(f"\n…и ещё {len(orders) - limit}. Все в ленте: {feed_link(params)}")
    return "\n".join(lines)


def build_overflow_message(count: int, params: dict) -> str:
    """Один заказ за другим — слишком много: хвост одним сообщением со ссылкой на ленту."""
    return f"🔔 И ещё подходящих заказов по вашему фильтру: {count}. Все в ленте: {feed_link(params)}"


def chat_url(order: Order, group_username: Optional[str]) -> Optional[str]:
    """Ссылка «сразу в чат к диспетчеру» для кнопки уведомления, или ``None``.

    Только обычные ``https``-ссылки: ``tg://user?id=`` Telegram в кнопке не примет, если у
    диспетчера закрыта приватность (отправка сообщения упадёт целиком), а ссылка на сообщение
    закрытой группы пускает лишь участников — такая кнопка водителю только мешала бы.
    """
    url = dispatcher_link(order, text=dispatcher_message(order), group_username=group_username)
    if not url or url.startswith("tg://") or url.startswith("https://t.me/c/"):
        return None
    return url


def build_keyboard(orders: list[Order], group_names: dict[int, Optional[str]]) -> dict:
    """Кнопки под уведомлением: у одного заказа — «Написать диспетчеру» и «Взять заказ»;
    у списка — по паре кнопок на строку с номером заказа."""
    shown = orders[: settings.subscription_max_lines]
    rows: list[list[dict]] = []
    for number, order in enumerate(shown, 1):
        single = len(shown) == 1
        row: list[dict] = []
        url = chat_url(order, group_names.get(order.source_chat_id))
        if url:
            row.append({"text": "✉️ Написать диспетчеру" if single else f"✉️ {number}", "url": url})
        row.append({"text": "✅ Взять заказ" if single else f"✅ Взять {number}", "callback_data": f"take:{order.id}"})
        rows.append(row)
        if single:
            rows.append([{"text": "Подробнее на сайте", "url": order_url(order)}])
    return {"inline_keyboard": rows}


async def group_usernames(session, orders: list[Order]) -> dict[int, Optional[str]]:
    """Публичные имена групп заказов — по ним строится ссылка на сообщение в группе."""
    chat_ids = {order.source_chat_id for order in orders}
    if not chat_ids:
        return {}
    rows = (
        await session.execute(
            select(WorkGroup.tg_chat_id, WorkGroup.username).where(WorkGroup.tg_chat_id.in_(chat_ids))
        )
    ).all()
    return {chat_id: username or None for chat_id, username in rows}


# --- Отправка ---------------------------------------------------------------------


def bot_token() -> str:
    return settings.alert_bot_token.strip() or settings.telegram_login_bot_token.strip()


def _without_url_buttons(markup: Optional[dict]) -> Optional[dict]:
    """Клавиатура только с кнопками-действиями (без ссылок): запасной вариант, если Telegram
    отверг ссылку в кнопке."""
    if not markup:
        return None
    rows = [[b for b in row if "callback_data" in b] for row in markup.get("inline_keyboard", [])]
    rows = [row for row in rows if row]
    return {"inline_keyboard": rows} if rows else None


async def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> str:
    """Отправляет сообщение через Bot API. Возвращает ``ok`` / ``blocked`` / ``retry``.

    ``blocked`` — бот не может писать этому человеку (не нажал «Старт», заблокировал
    бота): подписку выключаем. ``retry`` — временный сбой, попробуем в следующий проход.
    Если Telegram отверг кнопку (``BUTTON_…``), сообщение уходит ещё раз без кнопок-ссылок:
    иначе из-за одной плохой ссылки водитель остался бы без уведомления, а подписка
    отключилась бы как «бот заблокирован».
    """
    token = bot_token()
    if not token:
        log.error("Нет токена бота для уведомлений")
        return RETRY
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            if response.status_code == 400 and "BUTTON" in response.text.upper() and reply_markup:
                log.warning("Telegram отверг кнопку для tg:%s: %s", chat_id, response.text[:120])
                fallback = _without_url_buttons(reply_markup)
                payload.pop("reply_markup", None)
                if fallback:
                    payload["reply_markup"] = fallback
                response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
    except httpx.HTTPError as exc:
        log.warning("Telegram недоступен: %s", type(exc).__name__)
        return RETRY
    if response.status_code == 200:
        try:
            message_id = int(response.json()["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            message_id = None
        return SendOutcome(OK, message_id)
    if response.status_code in (400, 403):
        log.info("Бот не может писать tg:%s: %s", chat_id, response.text[:120])
        return BLOCKED
    log.warning("Telegram ответил %s: %s", response.status_code, response.text[:120])
    return RETRY


async def _bot_call(method: str, payload: dict) -> str:
    """Вызов Bot API без результата. ``ok`` / ``gone`` (повторять нет смысла) / ``retry``."""
    token = bot_token()
    if not token:
        return RETRY
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        log.warning("Telegram недоступен (%s): %s", method, type(exc).__name__)
        return RETRY
    if response.status_code == 200:
        return OK
    if response.status_code in (400, 403):
        # 400: сообщения уже нет или оно слишком старое; текст не изменился — тоже хорошо.
        return OK if "not modified" in response.text else GONE
    log.warning("Telegram ответил %s на %s: %s", response.status_code, method, response.text[:120])
    return RETRY


async def delete_message(chat_id: int, message_id: int) -> str:
    return await _bot_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


async def edit_message(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> str:
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _bot_call("editMessageText", payload)


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

        # Уже отправленное — по ЧЕЛОВЕКУ, а не по фильтру: заказ, подходящий двум фильтрам
        # водителя, присылается один раз.
        already = {
            (telegram_id, order_id)
            for order_id, telegram_id in (
                await session.execute(
                    select(SubscriptionNotification.order_id, OrderSubscription.telegram_id)
                    .join(
                        OrderSubscription,
                        OrderSubscription.id == SubscriptionNotification.subscription_id,
                    )
                    .where(SubscriptionNotification.order_id.in_([o.id for o in orders]))
                )
            ).all()
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
                and (sub.telegram_id, order.id) not in already
                and _ready(order, needs_geo, now)
                and filters.matches(order)
            ]
            if not matched:
                continue

            names = await group_usernames(session, matched)
            limit = max(1, settings.subscription_max_lines)
            stopped = False
            for position, order in enumerate(matched[:limit]):
                if position:
                    await asyncio.sleep(settings.subscription_send_gap_seconds)
                outcome = await send(
                    sub.telegram_id,
                    build_message([order], sub.params or {}),
                    reply_markup=build_keyboard([order], names),
                )
                if outcome == OK:
                    already.add((sub.telegram_id, order.id))
                    session.add(
                        SubscriptionNotification(
                            subscription_id=sub.id, order_id=order.id, sent_at=now,
                            message_id=getattr(outcome, "message_id", None),
                        )
                    )
                    sub.last_notified_at = now
                    sent_messages += 1
                else:
                    if outcome == BLOCKED:
                        sub.is_active = False
                        sub.error = "Бот не может написать вам: откройте бота и нажмите «Старт»"
                    stopped = True  # сбой или блок: остальное — на следующем проходе (или никогда)
                    break

            rest = matched[limit:]
            if rest and not stopped:
                # Много заказов сразу: про остальные одно сообщение. Они считаются отправленными —
                # иначе при каждом проходе водитель получал бы новый кусок того же хвоста.
                await asyncio.sleep(settings.subscription_send_gap_seconds)
                summary = await send(sub.telegram_id, build_overflow_message(len(rest), sub.params or {}))
                if summary == OK:
                    for order in rest:
                        already.add((sub.telegram_id, order.id))
                        session.add(SubscriptionNotification(subscription_id=sub.id, order_id=order.id, sent_at=now))
                    sent_messages += 1
            await session.commit()

    return sent_messages


Deleter = Callable[[int, int], Awaitable[str]]
Editor = Callable[..., Awaitable[str]]


async def retract_withdrawn(
    *,
    delete: Deleter = delete_message,
    edit: Editor = edit_message,
    now: Optional[datetime] = None,
) -> int:
    """Убирает у водителей уведомления о заявках, которые диспетчер удалил в Telegram.

    Сообщение целиком из таких заявок удаляется; если в нём были и другие (список из нескольких
    заказов) — переписывается без снятых. Сетевой сбой оставляет всё как есть до следующего
    прохода. Возвращает число обработанных сообщений.
    """
    now = now or now_utc_naive()
    done = 0
    async with SessionLocal() as session:
        notes = list(
            (
                await session.execute(
                    select(SubscriptionNotification).where(
                        SubscriptionNotification.message_id.is_not(None),
                        SubscriptionNotification.retracted_at.is_(None),
                        SubscriptionNotification.sent_at > now - RETRACT_WINDOW,
                    )
                )
            ).scalars().all()
        )
        if not notes:
            return 0

        order_ids = {note.order_id for note in notes}
        withdrawn = set(
            (
                await session.execute(
                    select(Order.id).where(
                        Order.id.in_(order_ids),
                        Order.status == OrderStatus.CANCELLED,
                        exists().where(ActionLog.order_id == Order.id, ActionLog.action.in_(WITHDRAWN_ACTIONS)),
                    )
                )
            ).scalars().all()
        )
        if not withdrawn:
            return 0

        orders = {
            order.id: order
            for order in (await session.execute(select(Order).where(Order.id.in_(order_ids)))).scalars().all()
        }
        subs = {
            sub.id: sub
            for sub in (
                await session.execute(
                    select(OrderSubscription).where(
                        OrderSubscription.id.in_({note.subscription_id for note in notes})
                    )
                )
            ).scalars().all()
        }

        messages: dict[tuple[int, int], list[SubscriptionNotification]] = {}
        for note in notes:
            messages.setdefault((note.subscription_id, note.message_id), []).append(note)

        for (sub_id, message_id), group in messages.items():
            gone = [note for note in group if note.order_id in withdrawn]
            if not gone:
                continue
            sub = subs.get(sub_id)
            if sub is None:
                continue
            remaining = [
                orders[note.order_id]
                for note in group
                if note.order_id not in withdrawn and note.order_id in orders
            ]
            if remaining:
                outcome = await edit(
                    sub.telegram_id,
                    message_id,
                    build_message(remaining, sub.params or {}),
                    reply_markup=build_keyboard(remaining, await group_usernames(session, remaining)),
                )
            else:
                outcome = await delete(sub.telegram_id, message_id)
            if outcome == RETRY:
                continue
            for note in gone:
                note.retracted_at = now
            done += 1
            log.info(
                "Уведомление tg:%s msg=%s: заявка снята диспетчером — %s",
                sub.telegram_id, message_id, "переписано" if remaining else "удалено",
            )
        await session.commit()
    return done


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
            await retract_withdrawn()
        except Exception:
            log.exception("Ошибка уборки снятых уведомлений")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
