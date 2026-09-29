"""Эквивалентность SQL-фильтров ленты и эталонной Python-реализации.

Зачем этот тест: фильтрация переехала из Python в SQL (без этого невозможна
пагинация), а :meth:`Filters.matches` осталась как читаемый эталон семантики.
Если кто-то поправит одну реализацию и забудет другую, расхождение всплывёт
здесь сразу — на проде его заметили бы только по «пропавшим» заявкам.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import HIDDEN_STATUSES, Order, OrderStatus
from app.timeutil import now_msk_naive
from app.web import queries
from app.web.filters import Filters

#: (название, структурные фильтры, поисковый запрос)
CASES = [
    ("без фильтров", {}, ""),
    ("город отправления", {"from_city": "Симферополь"}, ""),
    ("город назначения", {"to_city": "Сочи"}, ""),
    ("город в другом регистре", {"from_city": "симферополь"}, ""),
    ("сокращение города", {"from_city": "симф"}, ""),
    ("города через запятую", {"from_city": "Симферополь, Севастополь"}, ""),
    ("минимум пассажиров", {"passengers": "3"}, ""),
    ("минимум пассажиров = 1", {"passengers": "1"}, ""),
    ("диапазон цены", {"price_min": "5000", "price_max": "15000"}, ""),
    ("только нижняя граница цены", {"price_min": "10000"}, ""),
    ("только верхняя граница цены", {"price_max": "4000"}, ""),
    ("окно времени суток", {"time_from": "06:00", "time_to": "12:00"}, ""),
    ("поиск по имени", {}, "иван"),
    ("поиск по телефону", {}, "79990000000"),
    ("поиск по городу", {}, "севастополь"),
    ("поиск без результата", {}, "абракадабра"),
    ("фильтр + поиск", {"to_city": "Сочи"}, "иван"),
    ("всё сразу", {"from_city": "Симферополь", "price_min": "1000", "passengers": "2"}, ""),
]


async def _seed(make_order) -> None:
    """Набор заказов, покрывающий «неудобные» значения полей.

    Даты намеренно разносятся на дни от текущего момента: ``fetch_feed``
    вычисляет «сейчас» внутри себя, и заявка в паре секунд от границы
    давала бы плавающий результат теста.
    """
    base = now_msk_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    # Обычные живые заявки.
    await make_order(from_city="Симферополь", to_city="Сочи", price="14000",
                     passengers=2, client_name="Иван", pickup_at=base + timedelta(days=1, hours=8))
    await make_order(from_city="СИМФЕРОПОЛЬ", to_city="Адлер", price="3500",
                     passengers=4, client_name="Пётр", pickup_at=base + timedelta(days=2, hours=6))
    await make_order(from_city="г. Севастополь", to_city="Сочи", price="9000",
                     passengers=None, client_name=None, client_phone=None,
                     pickup_at=base + timedelta(days=3, hours=18))
    # Цена неизвестна: фильтр по цене должен её отсечь, а по пассажирам — нет.
    await make_order(from_city="Симферополь", to_city="Краснодар", price="",
                     passengers=None, client_name="Иван", pickup_at=base + timedelta(days=4, hours=10))
    # Время подачи неизвестно.
    await make_order(from_city="Севастополь", to_city="Сочи", price="7000",
                     passengers=1, client_name="Мария", pickup_at=None)
    # Закрытые/протухшие — в ленту не попадают ни в SQL, ни в Python.
    await make_order(from_city="Симферополь", to_city="Сочи", price="12000", passengers=3,
                     status=OrderStatus.CANCELLED, pickup_at=base + timedelta(days=5, hours=9))
    await make_order(from_city="Симферополь", to_city="Сочи", price="12500", passengers=3,
                     status=OrderStatus.AGREED, pickup_at=base + timedelta(days=5, hours=11))
    await make_order(from_city="Симферополь", to_city="Сочи", price="13000", passengers=3,
                     status=OrderStatus.EXPIRED, pickup_at=base + timedelta(days=5, hours=12))
    # Взята другим водителем — из общей ленты исчезает.
    await make_order(from_city="Симферополь", to_city="Сочи", price="16000", passengers=2,
                     taken_by_token="other-driver-token", pickup_at=base + timedelta(days=6, hours=7))
    # Уже в прошлом — не показываем.
    await make_order(from_city="Симферополь", to_city="Сочи", price="8000", passengers=2,
                     pickup_at=base - timedelta(days=3, hours=5))


async def _python_ids(session, filters: Filters, q: str) -> set[int]:
    """Эталон: те же правила, но перебором строк в Python."""
    reference = now_msk_naive()
    orders = list((await session.execute(select(Order))).scalars().all())
    ids: set[int] = set()
    for order in orders:
        if order.status in HIDDEN_STATUSES:
            continue
        if order.taken_by_token is not None:
            continue
        if order.pickup_at is not None and order.pickup_at < reference:
            continue
        if not filters.matches(order):
            continue
        if q and q.lower() not in (order.search_text or ""):
            continue
        ids.add(order.id)
    return ids


@pytest.mark.parametrize(("name", "filter_kwargs", "q"), CASES, ids=[case[0] for case in CASES])
async def test_sql_and_python_select_the_same_orders(session, make_order, name, filter_kwargs, q):
    await _seed(make_order)

    filters = Filters(**filter_kwargs)
    page = await queries.fetch_feed(session, filters=filters, q=q, page=1, page_size=200)

    assert {order.id for order in page.items} == await _python_ids(session, filters, q), name


async def test_date_window_filter(session, make_order):
    """Окно дат задаётся относительно «сейчас», поэтому отдельным тестом."""
    await _seed(make_order)
    today = now_msk_naive().date()

    filters = Filters(date_from=today.isoformat(), date_to=(today + timedelta(days=2)).isoformat())
    page = await queries.fetch_feed(session, filters=filters, page=1, page_size=200)

    assert {order.id for order in page.items} == await _python_ids(session, filters, "")
    # Заявка без даты окна не проходит: в SQL NULL не удовлетворяет сравнению,
    # и matches() ведёт себя так же.
    assert all(order.pickup_at is not None for order in page.items)


async def test_passengers_filter_keeps_unknown(session, make_order):
    """Исправленный баг: заявка без числа пассажиров НЕ отсеивается фильтром «от 1».

    Диспетчеры часто не пишут пассажиров, и старый фильтр прятал из-за этого
    большую часть ленты.
    """
    await _seed(make_order)

    filters = Filters(passengers="1")
    page = await queries.fetch_feed(session, filters=filters, page=1, page_size=200)
    passengers = [order.passengers for order in page.items]

    assert None in passengers, "заявки с неизвестным числом пассажиров должны оставаться"
    assert all(value is None or value >= 1 for value in passengers)


async def test_closed_and_taken_orders_never_in_feed(session, make_order):
    await _seed(make_order)

    page = await queries.fetch_feed(session, page=1, page_size=200)

    assert {order.status for order in page.items} <= {
        OrderStatus.NEW, OrderStatus.NEEDS_CLARIFICATION
    }
    assert all(order.taken_by_token is None for order in page.items)
    assert all(
        order.pickup_at is None or order.pickup_at >= now_msk_naive() - timedelta(minutes=1)
        for order in page.items
    )


async def test_search_is_case_insensitive_for_cyrillic(session, make_order):
    """Ради этого и нужен search_text: LOWER() в SQLite не знает кириллицу."""
    order = await make_order(from_city="СИМФЕРОПОЛЬ", to_city="СОЧИ", client_name="ИВАН")

    for query in ("симферополь", "СИМФЕРОПОЛЬ", "иван", "Иван"):
        page = await queries.fetch_feed(session, q=query, page=1, page_size=50)
        assert order.id in {item.id for item in page.items}, f"поиск {query!r} не нашёл заказ"
