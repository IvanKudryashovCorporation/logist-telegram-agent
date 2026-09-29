"""SQL-запросы веб-слоя: лента, счётчики, справочник городов, действия водителя.

Здесь всё, что ходит в БД из сайта. Отдельный модуль, потому что роуты должны
оставаться тонкими, а запросы — тестируемыми без HTTP.

Главное отличие от прежней версии: фильтрация, сортировка и пагинация
выполняются В SQL. Раньше из БД доставались вообще все живые заказы, а дальше
Python их фильтровал и сортировал — при росте ленты это и тормоза, и
невозможность сделать страницы.

Исключение — сортировка «по близости»: расстояние считается от геопозиции
водителя до города подачи через гаверсинус, portable SQL-функции для этого нет,
поэтому для этого одного режима выборка сортируется в Python.
"""

import logging
import time
from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.city_aliases import KNOWN_CITIES, city_coords, expand_city_term
from app.config import settings
from app.models import HIDDEN_STATUSES, ActionLog, ActorType, Driver, Order, OrderStatus
from app.timeutil import now_msk_naive, now_utc_naive
from app.web.filters import Filters

log = logging.getLogger("web.queries")

SORT_LABELS = {
    "recent": "Недавности",
    "price": "Цене",
    "date": "Дате подачи",
    "distance": "Близости",
}
DEFAULT_SORT = "recent"

#: Жёсткий потолок на размер страницы — защита от ?page_size=999999.
MAX_PAGE_SIZE = 200


@dataclass
class FeedPage:
    """Страница ленты + всё, что нужно шаблону для навигации."""

    items: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 60

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def window(self) -> list[int]:
        """Номера страниц вокруг текущей — чтобы не рисовать 500 кнопок."""
        span = 2
        start = max(1, self.page - span)
        end = min(self.pages, self.page + span)
        return list(range(start, end + 1))


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние между двумя точками (lat, lon) в километрах."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    h = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def feed_conditions(*, now=None) -> list:
    """Что попадает в общую ленту.

    * скрытые статусы (CANCELLED / AGREED / EXPIRED) — нет;
    * уже взятые другим водителем — нет;
    * заявки с прошедшим временем подачи — нет (они же уходят в EXPIRED фоном).
    """
    reference = now or now_msk_naive()
    return [
        Order.status.notin_(HIDDEN_STATUSES),
        Order.taken_by_token.is_(None),
        or_(Order.pickup_at.is_(None), Order.pickup_at >= reference),
    ]


def sort_clause(sort: str):
    """SQL-порядок сортировки для режимов, которые считаются в БД."""
    if sort == "price":
        # Сначала «цена неизвестна», дальше по убыванию цены.
        return (Order.client_price.is_(None), Order.client_price.desc())
    if sort == "date":
        return (Order.pickup_at.is_(None), Order.pickup_at.asc())
    # recent — самые свежие сверху.
    return (Order.created_at.desc(), Order.id.desc())


def distance_sort(orders: list, coords: tuple[float, float]) -> list:
    """Сортировка по удалённости от водителя; города без координат — в конец."""

    def _distance(order: Order) -> float:
        city = city_coords(order.from_city)
        return haversine_km(coords, city) if city else float("inf")

    return sorted(orders, key=_distance)


# --- Справочник городов для автодополнения ----------------------------------


class _CityCache:
    """TTL-кэш списка городов.

    Список меняется только когда появляются заявки из нового города, а
    вычислялся он на КАЖДЫЙ запрос ленты двумя DISTINCT-сканами таблицы
    orders. Кэш на пару минут убирает эту работу почти полностью.
    """

    def __init__(self) -> None:
        self._value: Optional[list[str]] = None
        self._expires_at: float = 0.0

    def get(self) -> Optional[list[str]]:
        if self._value is not None and time.monotonic() < self._expires_at:
            return list(self._value)
        return None

    def set(self, value: list[str]) -> None:
        self._value = list(value)
        self._expires_at = time.monotonic() + max(0, settings.city_cache_ttl_seconds)

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0


_city_cache = _CityCache()


def invalidate_city_cache() -> None:
    """Сброс кэша городов (тесты, принудительное обновление)."""
    _city_cache.invalidate()


async def known_cities(session: AsyncSession) -> list[str]:
    """Справочник городов + реально встречающиеся в заявках."""
    cached = _city_cache.get()
    if cached is not None:
        return cached

    from_cities = (
        await session.execute(
            select(Order.from_city).where(Order.from_city.is_not(None)).distinct()
        )
    ).scalars().all()
    to_cities = (
        await session.execute(
            select(Order.to_city).where(Order.to_city.is_not(None)).distinct()
        )
    ).scalars().all()
    # На всякий случай раскрываем сокращения и здесь — часть заказов в базе
    # ещё может хранить город как есть, «Симф» и «Симферополь» не должны
    # попадать в подсказки как два разных города.
    real_cities = {expand_city_term(c) for c in [*from_cities, *to_cities]}
    value = sorted(set(KNOWN_CITIES) | real_cities)
    _city_cache.set(value)
    return list(value)


# --- Лента ------------------------------------------------------------------


async def fetch_feed(
    session: AsyncSession,
    *,
    filters: Optional[Filters] = None,
    q: str = "",
    sort: str = DEFAULT_SORT,
    page: int = 1,
    page_size: Optional[int] = None,
    coords: Optional[tuple[float, float]] = None,
    conditions: Optional[list] = None,
) -> FeedPage:
    """Страница ленты: фильтрация, сортировка и пагинация — в SQL.

    ``conditions`` позволяет переиспользовать запрос для других списков
    (например, «мои заказы»), подставив свой базовый WHERE.
    """
    filters = filters or Filters()
    size = min(max(1, page_size or settings.web_page_size), MAX_PAGE_SIZE)
    current_page = max(1, page)

    where = list(conditions if conditions is not None else feed_conditions())
    where += filters.sql()

    q = (q or "").strip().lower()
    if q:
        # Ищем по подготовленной search_text (всё уже в нижнем регистре):
        # LOWER() в SQLite не понимает кириллицу, поэтому приводить регистр
        # нужно при записи, а не в запросе. autoescape — чтобы «%» в запросе
        # не превращался в wildcard.
        where.append(Order.search_text.contains(q, autoescape=True))

    base = select(Order).where(*where)

    if sort == "distance" and coords is not None:
        # Расстояние считается в Python — пагинируем уже отсортированный список.
        rows = list((await session.execute(base)).scalars().all())
        rows = distance_sort(rows, coords)
        total = len(rows)
        start = (current_page - 1) * size
        return FeedPage(items=rows[start:start + size], total=total,
                        page=current_page, page_size=size)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    stmt = base.order_by(*sort_clause(sort)).limit(size).offset((current_page - 1) * size)
    items = list((await session.execute(stmt)).scalars().all())
    return FeedPage(items=items, total=total, page=current_page, page_size=size)


async def header_counts(session: AsyncSession, token: str) -> dict:
    """Счётчики в шапке сайта. Все три — по индексированным полям."""
    lenta_count = (
        await session.execute(
            select(func.count()).select_from(Order).where(*feed_conditions())
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


# --- Действия водителя ------------------------------------------------------


@dataclass
class ActionResult:
    """Итог действия водителя.

    ``reason`` нужен роутам, чтобы показать внятное сообщение, а не просто
    «не получилось»: заказ уже мог уйти другому водителю за те секунды,
    пока открывалась карточка.
    """

    ok: bool
    reason: str = ""
    order_id: Optional[int] = None

    @property
    def message(self) -> str:
        return {
            "": "",
            # Заказ уже у этого водителя — ругаться не на что.
            "already_mine": "",
            "not_found": "Заказ не найден.",
            "taken_by_other": "Заказ уже взял другой водитель — он пропал из общей ленты.",
            "not_owner": "Этот заказ взяли не вы.",
            "closed": "Заказ уже закрыт (скрыт или просрочен).",
        }.get(self.reason, "")


#: Взять нельзя то, что уже отработано.
_NOT_TAKABLE = frozenset({OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.AGREED})


async def take_order(session: AsyncSession, order_id: int, token: str) -> ActionResult:
    """Атомарно берёт заказ, если он ещё свободен.

    Ключевое отличие от прежней версии: там было «прочитали строку → посмотрели,
    что taken_by_token is None → присвоили → commit». Между проверкой и записью
    второй водитель успевал сделать то же самое, и заказ доставался ОБОИМ
    (последний commit побеждал). Теперь присвоение — один
    ``UPDATE ... WHERE taken_by_token IS NULL``: если строку уже заняли,
    условие не совпадёт и ``rowcount`` будет 0. Одинаково работает на SQLite
    и PostgreSQL.
    """
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.taken_by_token.is_(None),
            Order.status.notin_(_NOT_TAKABLE),
        )
        .values(taken_by_token=token, taken_at=now_utc_naive())
    )
    if result.rowcount == 1:
        session.add(
            ActionLog(
                order_id=order_id, actor=ActorType.DRIVER, action="order_taken",
                details=f"token={token[:8]}",
            )
        )
        await session.commit()
        return ActionResult(True, "", order_id)

    await session.rollback()
    return ActionResult(False, await _failure_reason(session, order_id, token), order_id)


async def release_order(session: AsyncSession, order_id: int, token: str) -> ActionResult:
    """Отменяет взятие — только если заказ взял именно этот водитель.

    «Договорились» откатывается в «новую»: водитель передумал, и заказ снова
    доступен остальным.

    Два отдельных UPDATE вместо одного с ``case()`` — намеренно. SQLAlchemy
    хранит enum по ИМЕНИ (в БД лежит ``'NEW'``), а внутри ``case()`` тип
    выражения выводится как String, и значение enum уходит в БД как ``'new'``.
    Такую строку потом невозможно прочитать (``LookupError``) — заказ ломался
    навсегда. Обычный ``values(status=...)`` проходит через bind-процессор
    колонки и пишет корректно.
    """
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.taken_by_token == token,
            Order.status == OrderStatus.AGREED,
        )
        .values(taken_by_token=None, taken_at=None, status=OrderStatus.NEW)
    )
    if result.rowcount != 1:
        # Заказ был просто взят (не «договорились») — статус не трогаем.
        result = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.taken_by_token == token)
            .values(taken_by_token=None, taken_at=None)
        )
    if result.rowcount == 1:
        session.add(
            ActionLog(
                order_id=order_id, actor=ActorType.DRIVER, action="order_released",
                details=f"token={token[:8]}",
            )
        )
        await session.commit()
        return ActionResult(True, "", order_id)
    await session.rollback()
    return ActionResult(False, await _failure_reason(session, order_id, token), order_id)


async def agree_order(session: AsyncSession, order_id: int, token: str) -> ActionResult:
    """Водитель договорился с диспетчером — заказ закрыт."""
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.taken_by_token == token,
            Order.status != OrderStatus.CANCELLED,
        )
        .values(status=OrderStatus.AGREED)
    )
    if result.rowcount == 1:
        session.add(
            ActionLog(order_id=order_id, actor=ActorType.DRIVER, action="order_agreed")
        )
        await session.commit()
        return ActionResult(True, "", order_id)
    await session.rollback()
    return ActionResult(False, await _failure_reason(session, order_id, token), order_id)


async def report_problem(
    session: AsyncSession, order_id: int, token: str, note: str
) -> ActionResult:
    """Обратная связь от водителя: цена неактуальна, диспетчер не отвечает и т.п.

    Это единственный способ для владельца узнать, что заявка в ленте врёт.
    Поля ``has_problem``/``problem_note`` в модели существовали и раньше, но
    не заполнялись ничем — то есть были мёртвым грузом.
    """
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.taken_by_token == token)
        .values(has_problem=True, problem_note=(note or "")[:500])
    )
    if result.rowcount == 1:
        session.add(
            ActionLog(
                order_id=order_id, actor=ActorType.DRIVER, action="problem_reported",
                details=(note or "")[:500],
            )
        )
        await session.commit()
        return ActionResult(True, "", order_id)
    await session.rollback()
    return ActionResult(False, await _failure_reason(session, order_id, token), order_id)


async def _failure_reason(session: AsyncSession, order_id: int, token: str) -> str:
    """Почему действие не прошло — для внятного сообщения водителю."""
    order = await session.get(Order, order_id)
    if order is None:
        return "not_found"
    if order.taken_by_token == token:
        # Заказ уже у этого водителя. Повторный клик — не ошибка, а
        # идемпотентность: без этой ветки показывалось «заказ закрыт».
        return "already_mine"
    if order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        return "closed"
    if order.taken_by_token:
        return "taken_by_other"
    return "closed"


async def my_orders(session: AsyncSession, token: str) -> list:
    """Заказы, взятые этим водителем (включая закрытые — это его история)."""
    rows = await session.execute(
        select(Order)
        .where(Order.taken_by_token == token)
        .order_by(Order.pickup_at.is_(None), Order.pickup_at.asc())
    )
    return list(rows.scalars().all())


async def upsert_driver(session: AsyncSession, payload: dict) -> Driver:
    """Создаёт или обновляет водителя по данным Telegram Login Widget.

    ``payload`` — уже ПРОВЕРЕННЫЕ (verify_telegram_login) данные виджета,
    ключи id/username/first_name/last_name/photo_url как есть из Telegram.
    Имя/фото могут смениться — обновляем их при каждом входе, а не только
    при первой регистрации.
    """
    telegram_id = int(payload["id"])
    driver = (
        await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
    ).scalar_one_or_none()

    if driver is None:
        driver = Driver(telegram_id=telegram_id)
        session.add(driver)

    driver.username = payload.get("username") or None
    driver.first_name = payload.get("first_name") or None
    driver.last_name = payload.get("last_name") or None
    driver.photo_url = payload.get("photo_url") or None
    driver.last_login_at = now_utc_naive()

    await session.commit()
    await session.refresh(driver)
    return driver


@dataclass
class DriverStats:
    taken_total: int
    agreed_total: int
    earned_total: float
    member_since: object  # datetime — object, чтобы не тащить сюда лишний импорт типов


async def driver_stats(session: AsyncSession, token: str, member_since) -> DriverStats:
    """Статистика для профиля: сколько взял, сколько закрыл, сколько заработал.

    "Заработал" — сумма client_price по заказам, которые водитель довёл до
    "Договорились". Агрегатор передаёт заявки 1 в 1 без наценки и не участвует
    в оплате, поэтому это не факт поступления денег, а честная оценка по тем
    ценам, что были в заявках, — другой цифры у нас просто нет.
    """
    taken_total = (
        await session.execute(
            select(func.count()).select_from(Order).where(Order.taken_by_token == token)
        )
    ).scalar_one()

    agreed_total, earned_total = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Order.client_price), 0))
            .select_from(Order)
            .where(Order.taken_by_token == token, Order.status == OrderStatus.AGREED)
        )
    ).one()

    return DriverStats(
        taken_total=taken_total,
        agreed_total=agreed_total,
        earned_total=float(earned_total or 0),
        member_since=member_since,
    )

