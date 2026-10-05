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
from datetime import timedelta
from typing import Optional

from sqlalchemy import Float, case, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.city_aliases import KNOWN_CITIES, _compact, city_coords, city_key, expand_city_term
from app.config import settings
from app.geo import haversine_km
from app.models import (
    HIDDEN_STATUSES,
    ActionLog,
    ActorType,
    Driver,
    Order,
    OrderStatus,
    WorkGroup,
)
from app.timeutil import now_msk_naive, now_utc_naive
from app.web.filters import Filters

log = logging.getLogger("web.queries")

SORT_LABELS = {
    "recent": "Недавности",
    "price": "Цене",
    "per_km": "Цене за км",
    "trip_km": "Расстоянию",
    "date": "Дате подачи",
    "distance": "Близости ко мне",
}
DEFAULT_SORT = "recent"

#: Направление каждой сортировки, когда водитель его не выбирал. Совпадает с тем,
#: как сайт сортировал раньше: свежие и дорогие сверху, ближайшая подача и
#: ближайший к водителю — первыми.
DEFAULT_DIRECTION = {
    "recent": "desc",
    "price": "desc",
    "per_km": "desc",
    "trip_km": "desc",
    "date": "asc",
    "distance": "asc",
}
DIRECTION_LABELS = {"desc": "По убыванию", "asc": "По возрастанию"}


def effective_direction(sort: str, direction: str = "") -> str:
    """``asc`` / ``desc``: выбранное водителем или обычное для этой сортировки."""
    direction = (direction or "").strip().lower()
    return direction if direction in DIRECTION_LABELS else DEFAULT_DIRECTION.get(sort, "desc")

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
        # «В ближайшее время» живёт ASAP_EXPIRE_HOURS: из ленты такая заявка пропадает сразу,
        # не дожидаясь фоновой очистки (она раз в 15 минут переводит её в EXPIRED).
        or_(
            Order.pickup_asap.is_(False),
            Order.created_at >= now_utc_naive() - timedelta(hours=max(0, settings.asap_expire_hours)),
        ),
    ]


def price_per_km_expr():
    """Цена заказа на километр пути; NULL, если нет цены или расстояния (< 1 км)."""
    return case(
        (
            Order.distance_km >= 1,
            cast(Order.client_price, Float) / Order.distance_km,
        ),
        else_=None,
    )


def sort_clause(sort: str, direction: str = ""):
    """SQL-порядок сортировки для режимов, которые считаются в БД.

    Заказы, у которых нет значения (цена, расстояние), всегда в конце списка —
    в любом направлении, иначе «по возрастанию» начиналось бы с пустых.
    """
    desc = effective_direction(sort, direction) == "desc"

    def ordered(expr):
        return (expr.is_(None), expr.desc() if desc else expr.asc(), Order.id.desc())

    if sort == "price":
        return ordered(Order.client_price)
    if sort == "trip_km":
        return ordered(Order.distance_km)
    if sort == "per_km":
        return ordered(price_per_km_expr())
    if sort == "date":
        # «В ближайшее время» (без времени подачи) — раньше всех при сортировке
        # по возрастанию, позже всех при сортировке по убыванию.
        if desc:
            return (Order.pickup_at.is_(None), Order.pickup_at.desc(), Order.id.desc())
        return (Order.pickup_asap.desc(), Order.pickup_at.is_(None), Order.pickup_at.asc(), Order.id.asc())
    # recent — по времени появления.
    if desc:
        return (Order.created_at.desc(), Order.id.desc())
    return (Order.created_at.asc(), Order.id.asc())


def distance_sort(orders: list, coords: tuple[float, float], *, descending: bool = False) -> list:
    """Сортировка по удалённости от водителя; города без координат — в конец."""

    def _distance(order: Order) -> float:
        return haversine_km(coords, city_coords(order.from_city))

    known = [o for o in orders if city_coords(o.from_city)]
    unknown = [o for o in orders if not city_coords(o.from_city)]
    return sorted(known, key=_distance, reverse=descending) + unknown


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
    # Один город — одна запись: названия сводятся к каноническому и дедуплицируются по
    # «сжатой» форме («Мелитополь» и «мелитополь» — не два города).
    chosen: dict[str, str] = {}
    for name in [*KNOWN_CITIES, *(expand_city_term(c) for c in [*from_cities, *to_cities])]:
        key = _compact(city_key(name))
        if not key:
            continue
        current = chosen.get(key)
        # Предпочитаем написание с заглавной буквы; из равных — по алфавиту.
        if current is None or (name[:1].isupper(), current) > (current[:1].isupper(), name):
            chosen[key] = name
    value = sorted(chosen.values())
    _city_cache.set(value)
    return list(value)


# --- Лента ------------------------------------------------------------------


def feed_where(filters: Optional[Filters] = None, q: str = "", conditions: Optional[list] = None) -> list:
    """WHERE ленты: базовые условия + структурные фильтры + поиск по тексту.

    Одно место и для самой ленты, и для счётчика на кнопке «Применить (N)» —
    иначе число на кнопке разошлось бы с тем, что покажет лента.
    """
    filters = filters or Filters()
    where = list(conditions if conditions is not None else feed_conditions())
    where += filters.sql()

    q = (q or "").strip().lower()
    if q:
        # Ищем по подготовленной search_text (всё уже в нижнем регистре):
        # LOWER() в SQLite не понимает кириллицу, поэтому приводить регистр
        # нужно при записи, а не в запросе. autoescape — чтобы «%» в запросе
        # не превращался в wildcard.
        where.append(Order.search_text.contains(q, autoescape=True))
    return where


async def count_feed(session: AsyncSession, *, filters: Optional[Filters] = None, q: str = "") -> int:
    """Сколько заказов покажет лента с этим фильтром (для «Применить (N)»)."""
    return (
        await session.execute(
            select(func.count()).select_from(Order).where(*feed_where(filters, q))
        )
    ).scalar_one()


async def fetch_feed(
    session: AsyncSession,
    *,
    filters: Optional[Filters] = None,
    q: str = "",
    sort: str = DEFAULT_SORT,
    direction: str = "",
    page: int = 1,
    page_size: Optional[int] = None,
    coords: Optional[tuple[float, float]] = None,
    conditions: Optional[list] = None,
) -> FeedPage:
    """Страница ленты: фильтрация, сортировка и пагинация — в SQL.

    ``conditions`` позволяет переиспользовать запрос для других списков
    (например, «мои заказы»), подставив свой базовый WHERE.
    """
    size = min(max(1, page_size or settings.web_page_size), MAX_PAGE_SIZE)
    current_page = max(1, page)

    base = select(Order).where(*feed_where(filters, q, conditions))

    if sort == "distance" and coords is not None:
        # Расстояние считается в Python — пагинируем уже отсортированный список.
        rows = list((await session.execute(base)).scalars().all())
        descending = effective_direction(sort, direction) == "desc"
        rows = distance_sort(rows, coords, descending=descending)
        total = len(rows)
        start = (current_page - 1) * size
        return FeedPage(items=rows[start:start + size], total=total,
                        page=current_page, page_size=size)

    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    stmt = base.order_by(*sort_clause(sort, direction)).limit(size).offset((current_page - 1) * size)
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
    # Сколько групп сейчас читает агент — а не из скольких уже пришли заказы:
    # новые группы молчат, пока в них не появится заявка, и счётчик «застревал».
    groups_count = (
        await session.execute(
            select(func.count()).select_from(WorkGroup).where(WorkGroup.is_active.is_(True))
        )
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
            "completed": "Заказ уже выполнен.",
            "not_active": "Выполнить можно только заказ, по которому вы договорились.",
        }.get(self.reason, "")


#: Взять нельзя то, что уже отработано.
_NOT_TAKABLE = frozenset(
    {
        OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.AGREED,
        OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED,
    }
)


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

    «Договорились» и «В работе» откатываются в «новую»: водитель передумал, и
    заказ снова доступен остальным. «Выполнен» отменить нельзя.

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
            Order.status.in_((OrderStatus.AGREED, OrderStatus.IN_PROGRESS)),
        )
        .values(taken_by_token=None, taken_at=None, status=OrderStatus.NEW)
    )
    if result.rowcount != 1:
        # Заказ был просто взят (не «договорились») — статус не трогаем.
        result = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.taken_by_token == token,
                Order.status != OrderStatus.COMPLETED,  # выполненный не отменить
            )
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
    """«Договорился с диспетчером»: заказ достаётся водителю и закрывается.

    Только это действие делает заказ «моим» — простой переход в чат к
    диспетчеру («Написать диспетчеру») ничего не берёт. Присвоение и закрытие —
    один ``UPDATE`` с условием «свободен или уже мой», поэтому два водителя,
    одновременно нажавшие кнопку, не получат заказ оба (см. ``take_order``).
    Повторное нажатие тем же водителем ничего не меняет. ``taken_at`` — момент
    «Договорился»: от него считается «в работе» у заявки без точного времени.
    """
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            or_(Order.taken_by_token.is_(None), Order.taken_by_token == token),
            Order.status.notin_(_NOT_TAKABLE),
        )
        .values(taken_by_token=token, taken_at=now_utc_naive(), status=OrderStatus.AGREED)
    )
    if result.rowcount == 1:
        session.add(
            ActionLog(
                order_id=order_id, actor=ActorType.DRIVER, action="order_agreed",
                details=f"token={token[:8]}",
            )
        )
        await session.commit()
        return ActionResult(True, "", order_id)
    await session.rollback()
    return ActionResult(False, await _failure_reason(session, order_id, token), order_id)


async def complete_order(session: AsyncSession, order_id: int, token: str) -> ActionResult:
    """«Выполнил заказ»: доступно, когда заказ «Договорились» или уже «В работе»."""
    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.taken_by_token == token,
            Order.status.in_((OrderStatus.AGREED, OrderStatus.IN_PROGRESS)),
        )
        .values(status=OrderStatus.COMPLETED)
    )
    if result.rowcount == 1:
        session.add(
            ActionLog(
                order_id=order_id, actor=ActorType.DRIVER, action="order_completed",
                details=f"token={token[:8]}",
            )
        )
        await session.commit()
        return ActionResult(True, "", order_id)
    await session.rollback()
    reason = await _failure_reason(session, order_id, token)
    if reason == "already_mine":
        reason = "not_active"  # заказ мой, но ещё не «Договорились» (старое «взят»)
    return ActionResult(False, reason, order_id)


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
    if order.taken_by_token == token and order.status == OrderStatus.COMPLETED:
        return "completed"
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
        .order_by(Order.pickup_asap.desc(), Order.pickup_at.is_(None), Order.pickup_at.asc())
    )
    return list(rows.scalars().all())


async def recent_driver_orders(session: AsyncSession, token: str, limit: int = 5) -> list:
    """Последние заказы водителя для профиля — самые недавно взятые сверху."""
    rows = await session.execute(
        select(Order)
        .where(Order.taken_by_token == token)
        .order_by(Order.taken_at.is_(None), Order.taken_at.desc(), Order.id.desc())
        .limit(limit)
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
    active_total: int
    completed_total: int
    earned_total: float
    member_since: object  # datetime — object, чтобы не тащить сюда лишний импорт типов


async def driver_stats(session: AsyncSession, token: str, member_since) -> DriverStats:
    """Статистика для профиля: сколько взял, сколько выполнил, сколько заработал.

    "Заработал" — сумма client_price по заказам со статусом «Выполнен»
    (водитель нажал «Выполнил» или прошло 72 часа после дедлайна). Агрегатор
    передаёт заявки 1 в 1 без наценки и не участвует в оплате, поэтому это не
    факт поступления денег, а честная оценка по тем ценам, что были в заявках, —
    другой цифры у нас просто нет.
    """
    taken_total = (
        await session.execute(
            select(func.count()).select_from(Order).where(Order.taken_by_token == token)
        )
    ).scalar_one()

    active_total = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.taken_by_token == token,
                Order.status.in_((OrderStatus.AGREED, OrderStatus.IN_PROGRESS)),
            )
        )
    ).scalar_one()

    completed_total, earned_total = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Order.client_price), 0))
            .select_from(Order)
            .where(
                Order.taken_by_token == token,
                Order.status == OrderStatus.COMPLETED,
            )
        )
    ).one()

    return DriverStats(
        taken_total=taken_total,
        active_total=active_total,
        completed_total=completed_total,
        earned_total=float(earned_total or 0),
        member_since=member_since,
    )

