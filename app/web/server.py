"""Публичная витрина заказов для водителей: общий список, "Мои заказы" и
карточка с кнопками "Написать диспетчеру" / "Взять заказ". Без авторизации.

Водителя различаем анонимной cookie в браузере (никаких паролей/логинов) —
кто первым нажал "Взять заказ", тот и забрал его себе в "Мои заказы", и
заказ пропадает из общего списка.

Цены показываются 1 в 1 из заявки, без наценки.
"""

import json
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from app.db.base import SessionLocal
from app.models import ORDER_STATUS_LABELS, Order, OrderStatus
from app.city_aliases import KNOWN_CITIES, city_coords, city_matches, expand_city_term, split_city_terms

# Диспетчеры пишут время подачи как есть, по местным часам (Крым/Кубань/
# Кавминводы — везде МСК, UTC+3, без перевода). pickup_at хранится в БД
# как это "гражданское" московское время, БЕЗ конвертации в UTC при разборе
# (см. combine_pickup_at в app/parsing/order_builder.py) — поэтому сравнивать
# его нужно с "сейчас по МСК", а не с datetime.utcnow() напрямую, иначе
# обратный отсчёт до подачи систематически врёт на 3 часа.
MSK_OFFSET = timedelta(hours=3)


def _now_msk() -> datetime:
    return datetime.utcnow() + MSK_OFFSET


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Заказы для водителей")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["status_labels"] = ORDER_STATUS_LABELS
templates.env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)

DRIVER_COOKIE = "driver_id"
DRIVER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 года


def _driver_token(request: Request) -> tuple[str, str | None]:
    """Возвращает (текущий токен, новый_токен_если_надо_поставить_cookie)."""
    existing = request.cookies.get(DRIVER_COOKIE)
    if existing:
        return existing, None
    new_token = uuid.uuid4().hex
    return new_token, new_token


def _set_driver_cookie(response: Response, new_token: str | None) -> None:
    if new_token:
        response.set_cookie(
            DRIVER_COOKIE, new_token, max_age=DRIVER_COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )


def _order_bucket(order: Order, today: date) -> str:
    if order.pickup_at is None:
        return "no_date"
    pickup_date = order.pickup_at.date()
    # Просроченные (дата в прошлом) группируем вместе с сегодняшними —
    # отдельной секции для них больше нет, а карточка красится через
    # is_overdue() независимо от бакета.
    if pickup_date <= today:
        return "today"
    if pickup_date == today + timedelta(days=1):
        return "tomorrow"
    return "later"


def _matches_query(order: Order, q: str) -> bool:
    haystack = " ".join(
        str(v)
        for v in [
            order.id,
            order.client_phone,
            order.client_name,
            order.from_city,
            order.to_city,
            order.from_address,
            order.to_address,
            order.dispatcher_username,
            order.contact_username,
        ]
        if v
    ).lower()
    return q.lower() in haystack


class Filters:
    """Структурные фильтры ленты: откуда/куда, дата и время подачи, цена,
    мин. число пассажиров. Все поля опциональны и комбинируются по И."""

    def __init__(
        self,
        from_city: str = "",
        to_city: str = "",
        passengers: str = "",
        date_from: str = "",
        date_to: str = "",
        time_from: str = "",
        time_to: str = "",
        price_min: str = "",
        price_max: str = "",
    ) -> None:
        self.from_city = from_city.strip()
        self.to_city = to_city.strip()
        self.passengers = self._parse_int(passengers)
        self.date_from = self._parse_date(date_from)
        self.date_to = self._parse_date(date_to)
        self.time_from = self._parse_time(time_from)
        self.time_to = self._parse_time(time_to)
        self.price_min = self._parse_decimal(price_min)
        self.price_max = self._parse_decimal(price_max)

    @staticmethod
    def _parse_int(v: str) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_date(v: str) -> date | None:
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_time(v: str) -> time | None:
        try:
            return datetime.strptime(v, "%H:%M").time()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_decimal(v: str) -> Decimal | None:
        try:
            return Decimal(v)
        except (TypeError, ValueError, InvalidOperation):
            return None

    @property
    def active_count(self) -> int:
        return sum(
            1
            for v in [
                self.from_city,
                self.to_city,
                self.passengers,
                self.date_from,
                self.date_to,
                self.time_from,
                self.time_to,
                self.price_min,
                self.price_max,
            ]
            if v is not None and v != ""
        )

    def matches(self, order: Order) -> bool:
        if self.from_city:
            terms = split_city_terms(self.from_city)
            if terms and not any(city_matches(t, order.from_city) for t in terms):
                return False
        if self.to_city:
            terms = split_city_terms(self.to_city)
            if terms and not any(city_matches(t, order.to_city) for t in terms):
                return False
        if self.passengers is not None and (order.passengers or 0) < self.passengers:
            return False
        if self.date_from is not None and (order.pickup_at is None or order.pickup_at.date() < self.date_from):
            return False
        if self.date_to is not None and (order.pickup_at is None or order.pickup_at.date() > self.date_to):
            return False
        if self.time_from is not None and (order.pickup_at is None or order.pickup_at.time() < self.time_from):
            return False
        if self.time_to is not None and (order.pickup_at is None or order.pickup_at.time() > self.time_to):
            return False
        if self.price_min is not None and (order.client_price is None or order.client_price < self.price_min):
            return False
        if self.price_max is not None and (order.client_price is None or order.client_price > self.price_max):
            return False
        return True


def dispatcher_link(order: Order) -> str | None:
    """Ссылка на диалог с диспетчером в Telegram, если известен его аккаунт.

    contact_username — явное "писать @..." из текста заявки — приоритетнее
    отправителя сообщения: часто заявку публикует не тот, кому по ней
    фактически нужно писать (пересылка, бот группы и т.п.).

    https://t.me/<username> — предпочтительно: работает в любом браузере,
    как в приложении, так и без него (откроет web.telegram.org). tg://user?id=
    оставлен запасным для диспетчеров без публичного username — многие
    мобильные браузеры блокируют этот "сырой" протокол при переходе с сайта,
    поэтому им пользуемся только когда другого варианта нет.
    """
    if order.contact_username:
        return f"https://t.me/{order.contact_username}"
    if order.dispatcher_username:
        return f"https://t.me/{order.dispatcher_username}"
    if order.dispatcher_tg_id:
        return f"tg://user?id={order.dispatcher_tg_id}"
    return None


templates.env.globals["dispatcher_link"] = dispatcher_link


def _duration_str(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч" + (f" {rem} мин" if rem else "")
    days = hours // 24
    return f"{days} дн"


def is_overdue(order: Order) -> bool:
    """Подача уже прошла по времени — красим карточку независимо от бакета
    (бакет группирует по дате, а не по факту "уже прошло")."""
    return bool(order.pickup_at and order.pickup_at < _now_msk())


def pickup_subtext(order: Order) -> str:
    """Короткая подпись под временем подачи — как давно/скоро подача.

    Список больше не группируется по дню (Сегодня/Завтра/...), поэтому
    бакет определяется прямо здесь, по факту даты подачи.
    """
    if order.pickup_at is None:
        return "время не указано"
    bucket = _order_bucket(order, _now_msk().date())
    if bucket == "tomorrow":
        return "завтра"
    if bucket == "later":
        return order.pickup_at.strftime("%d.%m")
    delta_min = int((order.pickup_at - _now_msk()).total_seconds() // 60)
    if delta_min >= 0:
        return f"через {_duration_str(delta_min)}"
    return f"просрочено на {_duration_str(-delta_min)}"


def relative_ago(dt: datetime | None) -> str:
    """«N мин назад» и т.п. — для времени поступления заявки."""
    if dt is None:
        return ""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
    minutes = max(0, int((now - dt).total_seconds() // 60))
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def order_tags(order: Order) -> list[tuple[str, bool]]:
    """Короткие теги для карточки: (текст, основной_ли/тёмный)."""
    tags: list[tuple[str, bool]] = []
    if order.passengers:
        tags.append((f"{order.passengers} пасс.", True))
    if order.car_class:
        tags.append((order.car_class, False))
    if order.flight_or_train:
        tags.append(("Рейс", False))
    if order.needs_child_seat:
        tags.append(("Кресло", False))
    if order.has_pets:
        tags.append(("Животное", False))
    return tags


templates.env.globals["pickup_subtext"] = pickup_subtext
templates.env.globals["is_overdue"] = is_overdue
templates.env.globals["relative_ago"] = relative_ago
templates.env.globals["order_tags"] = order_tags


async def _header_counts(session, token: str) -> dict:
    lenta_count = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status != OrderStatus.CANCELLED,
                Order.taken_by_token.is_(None),
                # То же условие, что и в самой ленте — иначе счётчик в шапке
                # включает и заказы, чья подача уже прошла и которые в
                # списке не показываются.
                or_(Order.pickup_at.is_(None), Order.pickup_at >= _now_msk()),
            )
        )
    ).scalar_one()
    my_count = (
        await session.execute(
            select(func.count()).select_from(Order).where(Order.taken_by_token == token)
        )
    ).scalar_one()
    groups_count = (
        await session.execute(select(func.count(func.distinct(Order.source_chat_id))))
    ).scalar_one()
    return {"lenta_count": lenta_count, "my_count": my_count, "groups_count": groups_count}


async def _known_cities(session) -> list[str]:
    """Справочник городов + реально встречающиеся в заявках — для
    автодополнения в полях "Откуда"/"Куда" на сайте."""
    from_cities = (
        await session.execute(select(Order.from_city).where(Order.from_city.is_not(None)).distinct())
    ).scalars().all()
    to_cities = (
        await session.execute(select(Order.to_city).where(Order.to_city.is_not(None)).distinct())
    ).scalars().all()
    # На всякий случай раскрываем сокращения и здесь — часть заказов в базе
    # ещё может хранить город как есть (до этого фикса), "Симф" и
    # "Симферополь" не должны попадать в подсказки как два разных города.
    real_cities = {expand_city_term(c) for c in [*from_cities, *to_cities]}
    return sorted(set(KNOWN_CITIES) | real_cities)


SORT_LABELS = {
    "recent": "Недавности",
    "price": "Цене",
    "date": "Дате подачи",
    "distance": "Близости",
}
DEFAULT_SORT = "recent"


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    h = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def _sort_orders(orders: list[Order], sort: str, driver_coords: tuple[float, float] | None) -> list[Order]:
    """Меняет порядок заказов ВНУТРИ каждой секции (Сегодня/Завтра/...) —
    сама разбивка по дню подачи сохраняется, сортировка только на неё
    накладывается (_bucketize просто раскладывает уже упорядоченный список)."""
    if sort == "price":
        return sorted(orders, key=lambda o: (o.client_price is None, -(o.client_price or 0)))
    if sort == "date":
        return sorted(orders, key=lambda o: (o.pickup_at is None, o.pickup_at))
    if sort == "distance" and driver_coords is not None:
        def _distance(order: Order) -> float:
            coords = city_coords(order.from_city)
            return _haversine_km(driver_coords, coords) if coords else float("inf")

        return sorted(orders, key=_distance)
    # "recent" (по умолчанию) — самые свежие заявки сверху.
    return sorted(orders, key=lambda o: o.created_at, reverse=True)


def _bucketize(orders: list[Order]) -> dict[str, list[Order]]:
    today = _now_msk().date()
    buckets: dict[str, list[Order]] = {
        "today": [],
        "tomorrow": [],
        "later": [],
        "no_date": [],
    }
    for order in orders:
        buckets[_order_bucket(order, today)].append(order)
    return buckets


BUCKET_TITLES = {
    "today": "Сегодня",
    "tomorrow": "Завтра",
    "later": "Позже",
    "no_date": "Без даты",
}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    q: str = "",
    from_city: str = "",
    to_city: str = "",
    passengers: str = "",
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    price_min: str = "",
    price_max: str = "",
    sort: str = DEFAULT_SORT,
    lat: str = "",
    lon: str = "",
):
    token, new_token = _driver_token(request)
    filters = Filters(
        from_city, to_city, passengers, date_from, date_to, time_from, time_to, price_min, price_max
    )
    if sort not in SORT_LABELS:
        sort = DEFAULT_SORT
    driver_coords: tuple[float, float] | None = None
    if sort == "distance":
        try:
            driver_coords = (float(lat), float(lon))
        except (TypeError, ValueError):
            driver_coords = None

    async with SessionLocal() as session:
        stmt = (
            select(Order)
            .where(
                Order.status != OrderStatus.CANCELLED,
                Order.taken_by_token.is_(None),
                # Подача уже прошла — заказ больше не актуален для ленты.
                or_(Order.pickup_at.is_(None), Order.pickup_at >= _now_msk()),
            )
            .order_by(Order.pickup_at.is_(None), Order.pickup_at)
        )
        orders = (await session.execute(stmt)).scalars().all()
        counts = await _header_counts(session, token)
        known_cities = await _known_cities(session)

    q = q.strip()
    if q:
        orders = [o for o in orders if _matches_query(o, q)]
    orders = [o for o in orders if filters.matches(o)]
    orders = _sort_orders(orders, sort, driver_coords)

    html = templates.TemplateResponse(
        "orders_list.html",
        {
            "request": request,
            "orders": orders,
            "q": q,
            "filters": filters,
            "known_cities": known_cities,
            "sort": sort,
            "sort_labels": SORT_LABELS,
            "lat": lat,
            "lon": lon,
            **counts,
        },
    )
    _set_driver_cookie(html, new_token)
    return html


@app.get("/my", response_class=HTMLResponse)
async def my_orders(request: Request):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        stmt = select(Order).where(Order.taken_by_token == token).order_by(Order.pickup_at.is_(None), Order.pickup_at)
        orders = (await session.execute(stmt)).scalars().all()
        counts = await _header_counts(session, token)

    html = templates.TemplateResponse(
        "my_orders.html",
        {
            "request": request,
            "buckets": _bucketize(orders),
            "bucket_titles": BUCKET_TITLES,
            **counts,
        },
    )
    _set_driver_cookie(html, new_token)
    return html


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)
        counts = await _header_counts(session, token)

    html = templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "order": order,
            "is_mine": order.taken_by_token == token,
            "is_taken_by_someone_else": bool(order.taken_by_token) and order.taken_by_token != token,
            **counts,
        },
    )
    _set_driver_cookie(html, new_token)
    return html


@app.post("/orders/{order_id}/take")
async def take_order(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is not None and order.taken_by_token is None:
            # Кто первый нажал — тот и взял; SQLite обрабатывает запросы
            # последовательно, гонки между двумя водителями тут не будет.
            order.taken_by_token = token
            order.taken_at = datetime.utcnow()
            await session.commit()

    redirect = RedirectResponse(url=f"/orders/{order_id}", status_code=303)
    _set_driver_cookie(redirect, new_token)
    return redirect


@app.post("/orders/{order_id}/take-and-contact")
async def take_and_contact(request: Request, order_id: int):
    """Единая кнопка "Написать диспетчеру": берёт заказ себе (если ещё
    свободен) и сразу открывает диалог с диспетчером в Telegram."""
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

        if order.taken_by_token is not None and order.taken_by_token != token:
            # Уже взят другим водителем — просто открываем карточку заказа,
            # без перехода к диспетчеру.
            redirect = RedirectResponse(url=f"/orders/{order_id}", status_code=303)
            _set_driver_cookie(redirect, new_token)
            return redirect

        if order.taken_by_token is None:
            order.taken_by_token = token
            order.taken_at = datetime.utcnow()
            await session.commit()

        link = dispatcher_link(order)

    redirect = RedirectResponse(url=link or f"/orders/{order_id}", status_code=303)
    _set_driver_cookie(redirect, new_token)
    return redirect


@app.post("/orders/{order_id}/release")
async def release_order(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is not None and order.taken_by_token == token:
            order.taken_by_token = None
            order.taken_at = None
            await session.commit()

    redirect = RedirectResponse(url="/my", status_code=303)
    _set_driver_cookie(redirect, new_token)
    return redirect
