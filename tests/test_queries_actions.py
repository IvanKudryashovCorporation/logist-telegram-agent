"""Пагинация ленты и действия водителя (взять / отпустить / договориться / жалоба).

Проверяется именно то, ради чего переписывали запросы:

* пагинация режет ленту в SQL и не теряет/не дублирует заказы между страницами;
* взятие заказа атомарно — второй водитель получает отказ, а не «оба взяли»;
* состояние после действий соответствует ожиданиям ленты и «моих заказов».
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.base import SessionLocal
from app.models import ActionLog, Order, OrderStatus
from app.timeutil import now_msk_naive
from app.web import queries
from app.web.filters import Filters

TOKEN_A = "driver-token-aaaa"
TOKEN_B = "driver-token-bbbb"


async def _seed_many(make_order, count: int = 12) -> list[Order]:
    base = now_msk_naive() + timedelta(days=1)
    orders = []
    for index in range(count):
        orders.append(
            await make_order(
                from_city="Симферополь",
                to_city="Сочи",
                price=str(5000 + index * 1000),
                passengers=(index % 4) + 1,
                pickup_at=base + timedelta(hours=index),
            )
        )
    return orders


# --- Пагинация ---------------------------------------------------------------


async def test_feed_is_split_into_pages_without_loss(session, make_order):
    created = await _seed_many(make_order, 12)

    pages = []
    for number in (1, 2, 3):
        page = await queries.fetch_feed(session, page=number, page_size=5)
        assert page.total == 12
        assert page.pages == 3
        assert page.page == number
        pages.append(page)

    assert [len(page.items) for page in pages] == [5, 5, 2]
    seen = [order.id for page in pages for order in page.items]
    assert len(seen) == len(set(seen)), "страницы не должны пересекаться"
    assert set(seen) == {order.id for order in created}


async def test_page_navigation_flags_and_window(session, make_order):
    await _seed_many(make_order, 12)

    first = await queries.fetch_feed(session, page=1, page_size=5)
    middle = await queries.fetch_feed(session, page=2, page_size=5)
    last = await queries.fetch_feed(session, page=3, page_size=5)

    assert (first.has_prev, first.has_next) == (False, True)
    assert (middle.has_prev, middle.has_next) == (True, True)
    assert (last.has_prev, last.has_next) == (True, False)
    # Окно страниц вокруг текущей — чтобы не рисовать сотни кнопок.
    assert middle.window == [1, 2, 3]


async def test_page_beyond_last_returns_empty_but_valid_total(session, make_order):
    await _seed_many(make_order, 3)

    page = await queries.fetch_feed(session, page=99, page_size=5)
    assert page.items == []
    assert page.total == 3
    assert page.pages == 1


async def test_page_size_is_clamped(session, make_order):
    await _seed_many(make_order, 3)

    # Потолок защищает от ?page_size=999999, а пустое/нулевое значение
    # откатывается к WEB_PAGE_SIZE из конфига (в тестах = 5).
    assert (await queries.fetch_feed(session, page_size=10_000)).page_size == queries.MAX_PAGE_SIZE
    assert (await queries.fetch_feed(session, page_size=0)).page_size == 5
    assert (await queries.fetch_feed(session, page_size=-3)).page_size == 1
    # Отрицательный/нулевой номер страницы не должен ронять OFFSET.
    assert (await queries.fetch_feed(session, page=0)).page == 1
    assert (await queries.fetch_feed(session, page=-5)).page == 1


async def test_filters_survive_pagination(session, make_order):
    """Пагинация обязана учитывать фильтры, иначе страница 2 показывает не то."""
    await _seed_many(make_order, 6)
    await make_order(from_city="Севастополь", to_city="Сочи", price="20000",
                     pickup_at=now_msk_naive() + timedelta(days=2))
    await make_order(from_city="Севастополь", to_city="Сочи", price="21000",
                     pickup_at=now_msk_naive() + timedelta(days=3))

    filters = Filters(from_city="Севастополь")
    page1 = await queries.fetch_feed(session, filters=filters, page=1, page_size=1)
    page2 = await queries.fetch_feed(session, filters=filters, page=2, page_size=1)

    assert page1.total == 2
    assert page2.total == 2
    assert all(order.from_city == "Севастополь" for order in page1.items + page2.items)
    assert page1.items[0].id != page2.items[0].id


# --- Сортировка --------------------------------------------------------------


async def test_sort_by_price_puts_unknown_last(session, make_order):
    await make_order(price="5000", pickup_at=now_msk_naive() + timedelta(days=1))
    await make_order(price="15000", pickup_at=now_msk_naive() + timedelta(days=1))
    await make_order(price="", pickup_at=now_msk_naive() + timedelta(days=1))

    page = await queries.fetch_feed(session, sort="price", page=1, page_size=50)
    prices = [order.client_price for order in page.items]

    assert prices[0] == Decimal("15000")
    assert prices[-1] is None


async def test_sort_by_date_ascending(session, make_order):
    base = now_msk_naive() + timedelta(days=1)
    await make_order(pickup_at=base + timedelta(hours=5))
    await make_order(pickup_at=base + timedelta(hours=1))
    await make_order(pickup_at=base + timedelta(hours=9))

    page = await queries.fetch_feed(session, sort="date", page=1, page_size=50)
    # По datetime целиком, не по голому часу: час без даты ломается, когда
    # +9 часов к base перескакивает через полночь на следующие сутки.
    pickups = [order.pickup_at for order in page.items]

    assert pickups == sorted(pickups)


async def test_sort_recent_is_stable_for_same_second(session, make_order):
    """created_at в SQLite имеет разрешение в секунду — порядок обязан быть полным."""
    orders = await _seed_many(make_order, 5)

    page = await queries.fetch_feed(session, sort="recent", page=1, page_size=50)
    ids = [order.id for order in page.items]

    assert ids == sorted((order.id for order in orders), reverse=True)


# --- Действия водителя -------------------------------------------------------


async def test_take_order_sets_token_and_hides_from_feed(session, make_order):
    order = await make_order()

    result = await queries.take_order(session, order.id, TOKEN_A)
    assert result.ok is True

    reloaded = await session.get(Order, order.id)
    assert reloaded.taken_by_token == TOKEN_A
    assert reloaded.taken_at is not None

    page = await queries.fetch_feed(session, page=1, page_size=50)
    assert order.id not in {item.id for item in page.items}, "взятый заказ должен уйти из ленты"


async def test_second_driver_cannot_steal_taken_order(session, make_order):
    """Главная гонка сайта: два водителя жмут «Взять» одновременно.

    Присвоение идёт одним ``UPDATE ... WHERE taken_by_token IS NULL``, поэтому
    второй запрос просто не находит строку — заказ не может достаться обоим.
    """
    order = await make_order()

    first = await queries.take_order(session, order.id, TOKEN_A)
    second = await queries.take_order(session, order.id, TOKEN_B)

    assert first.ok is True
    assert second.ok is False
    assert second.reason == "taken_by_other"
    assert second.message, "водитель должен увидеть внятную причину отказа"

    assert (await session.get(Order, order.id)).taken_by_token == TOKEN_A


async def test_repeat_take_by_owner_reports_already_mine(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    again = await queries.take_order(session, order.id, TOKEN_A)

    assert again.ok is False
    assert again.reason == "already_mine"
    assert again.message == "", "повторный клик не должен выглядеть как ошибка"


async def test_release_returns_order_to_feed(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    result = await queries.release_order(session, order.id, TOKEN_A)
    assert result.ok is True

    reloaded = await session.get(Order, order.id)
    assert reloaded.taken_by_token is None
    assert reloaded.taken_at is None
    assert reloaded.status == OrderStatus.NEW

    page = await queries.fetch_feed(session, page=1, page_size=50)
    assert order.id in {item.id for item in page.items}


async def test_release_by_another_driver_fails(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    result = await queries.release_order(session, order.id, TOKEN_B)

    assert result.ok is False
    assert (await session.get(Order, order.id)).taken_by_token == TOKEN_A


async def test_agree_closes_order_and_release_reverts(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    agreed = await queries.agree_order(session, order.id, TOKEN_A)
    assert agreed.ok is True
    assert (await session.get(Order, order.id)).status == OrderStatus.AGREED

    # «Договорились» скрывает заказ из общей ленты.
    page = await queries.fetch_feed(session, page=1, page_size=50)
    assert order.id not in {item.id for item in page.items}

    # Водитель передумал — откатывает в «новую», заказ снова доступен остальным.
    released = await queries.release_order(session, order.id, TOKEN_A)
    assert released.ok is True
    reloaded = await session.get(Order, order.id)
    assert reloaded.status == OrderStatus.NEW
    assert reloaded.taken_by_token is None


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [(OrderStatus.CANCELLED, "closed"), (OrderStatus.EXPIRED, "closed")],
    ids=["cancelled", "expired"],
)
async def test_cannot_take_closed_order(session, make_order, status, expected_reason):
    order = await make_order(status=status, pickup_at=now_msk_naive() + timedelta(days=1))

    result = await queries.take_order(session, order.id, TOKEN_A)

    assert result.ok is False
    assert result.reason == expected_reason
    assert (await session.get(Order, order.id)).taken_by_token is None


async def test_take_missing_order_returns_not_found(session):
    result = await queries.take_order(session, 999999, TOKEN_A)

    assert result.ok is False
    assert result.reason == "not_found"


async def test_report_problem_marks_order(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    result = await queries.report_problem(session, order.id, TOKEN_A, "Цена неактуальна")
    assert result.ok is True

    reloaded = await session.get(Order, order.id)
    assert reloaded.has_problem is True
    assert reloaded.problem_note == "Цена неактуальна"


async def test_report_problem_by_stranger_fails(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    result = await queries.report_problem(session, order.id, TOKEN_B, "Диспетчер не отвечает")

    assert result.ok is False
    assert (await session.get(Order, order.id)).has_problem is False


async def test_problem_note_is_truncated(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)

    await queries.report_problem(session, order.id, TOKEN_A, "ж" * 5000)

    assert len((await session.get(Order, order.id)).problem_note or "") <= 500


async def test_actions_are_written_to_history(session, make_order):
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)
    await queries.report_problem(session, order.id, TOKEN_A, "не дозвонился")
    await queries.release_order(session, order.id, TOKEN_A)

    actions = list(
        (
            await session.execute(
                select(ActionLog.action)
                .where(ActionLog.order_id == order.id)
                .order_by(ActionLog.id)
            )
        ).scalars().all()
    )

    assert actions == ["order_taken", "problem_reported", "order_released"]


async def test_header_counts(session, make_order):
    await make_order()
    await make_order()
    mine = await make_order()
    await queries.take_order(session, mine.id, TOKEN_A)
    await make_order(status=OrderStatus.CANCELLED, pickup_at=now_msk_naive() + timedelta(days=1))

    counts = await queries.header_counts(session, TOKEN_A)

    # Две свободные заявки: третья взята мной, четвёртая скрыта.
    assert counts["lenta_count"] == 2
    assert counts["my_count"] == 1
    assert counts["groups_count"] == 1


async def test_my_orders_includes_closed_history(session, make_order):
    first = await make_order()
    second = await make_order()
    stranger = await make_order()
    await queries.take_order(session, first.id, TOKEN_A)
    await queries.take_order(session, second.id, TOKEN_A)
    await queries.agree_order(session, second.id, TOKEN_A)

    mine = await queries.my_orders(session, TOKEN_A)

    # Закрытый («договорились») заказ остаётся в истории водителя,
    # а чужой в неё не попадает.
    assert {order.id for order in mine} == {first.id, second.id}
    assert stranger.id not in {order.id for order in mine}


async def test_release_writes_enum_name_not_value(session, make_order):
    """Регрессия на порчу данных.

    Откат «договорились» → «новая» был написан через ``case()``, и SQLAlchemy
    клал в БД ЗНАЧЕНИЕ enum (``'new'``) вместо ИМЕНИ (``'NEW'``), которое
    использует тип колонки. Строку после этого невозможно прочитать —
    ``LookupError``, то есть карточка заказа и лента падали в 500 навсегда.
    """
    order = await make_order()
    await queries.take_order(session, order.id, TOKEN_A)
    await queries.agree_order(session, order.id, TOKEN_A)

    result = await queries.release_order(session, order.id, TOKEN_A)
    assert result.ok is True

    raw = (
        await session.execute(
            text("SELECT status FROM orders WHERE id = :order_id"), {"order_id": order.id}
        )
    ).scalar_one()
    assert raw in {status.name for status in OrderStatus}, (
        f"в БД должно лежать имя enum, а не значение; получено {raw!r}"
    )

    # Главное следствие: строку можно прочитать в новой сессии.
    async with SessionLocal() as fresh:
        reloaded = await fresh.get(Order, order.id)
        assert reloaded is not None
        assert reloaded.status == OrderStatus.NEW


async def test_all_status_writes_are_readable(session, make_order):
    """Любая запись статуса через queries оставляет строку читаемой."""
    order = await make_order()

    await queries.take_order(session, order.id, TOKEN_A)
    await queries.agree_order(session, order.id, TOKEN_A)
    await queries.release_order(session, order.id, TOKEN_A)
    await queries.take_order(session, order.id, TOKEN_B)

    async with SessionLocal() as fresh:
        reloaded = await fresh.get(Order, order.id)
        assert reloaded.taken_by_token == TOKEN_B
        assert reloaded.status in set(OrderStatus)
