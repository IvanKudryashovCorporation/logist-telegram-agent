"""Правила разбора: цена «в тысячах» и срочная подача «в ближайшее время».

Реальный кейс с прода (заказ №381): «В течение часа / Осиново, ЛНР / Краснодар
/ 1 чел / 25+платка» показывался как «подача не указана, 25 ₽».
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Order, OrderStatus
from app.parsing.llm_parser import ParseResult
from app.parsing.order_builder import apply_parsed_fields, resolve_pickup_at
from app.parsing.schema import ParsedOrder
from app.services.cleanup import expire_stale_orders
from app.telegram import work_group
from app.timeutil import now_msk_naive
from app.web import queries
from app.web.presenters import dispatcher_message, pickup_label, pickup_subtext

TEXT_381 = "В течение часа\nОсиново, ЛНР\nКраснодар, адрес\n1 чел\n25+платка"


# --- Цена в тысячах -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25", "25000"),
        ("14", "14000"),
        ("7.5", "7500.0"),
        ("99", "99000"),
        ("100", "100"),  # не тысячи: граница включена сверху
        ("150", "150"),
        ("3000", "3000"),
        ("90000", "90000"),
        ("0", "0"),
    ],
)
def test_price_written_in_thousands_is_scaled(raw, expected):
    assert str(ParsedOrder(client_price=raw).client_price) == expected


def test_missing_price_stays_missing():
    assert ParsedOrder(client_price=None).client_price is None
    assert ParsedOrder().client_price is None


# --- Срочная подача -----------------------------------------------------------


def _fresh_order() -> Order:
    # Статус задан явно: у несохранённого Order он пуст до первого INSERT.
    return Order(
        source_chat_id=1, source_message_id=1, source_sub_index=0, raw_text="x",
        status=OrderStatus.NEW,
    )


def test_asap_order_gets_flag_and_no_clarification_status():
    order = _fresh_order()
    parsed = ParsedOrder(
        from_city="Осиново", to_city="Краснодар", client_price=25, pickup_asap=True,
        missing_fields=["нет времени подачи"],
    )

    apply_parsed_fields(order, parsed)

    assert order.pickup_asap is True
    assert order.pickup_at is None
    assert order.client_price == Decimal(25000)
    # «Нет времени подачи» у срочной заявки — не недостающие данные.
    assert order.status == OrderStatus.NEW


def test_asap_does_not_hide_other_missing_fields():
    order = _fresh_order()
    parsed = ParsedOrder(
        from_city="Осиново", pickup_asap=True,
        missing_fields=["нет времени подачи", "не указана стоимость"],
    )

    apply_parsed_fields(order, parsed)

    assert order.status == OrderStatus.NEEDS_CLARIFICATION


def test_named_time_beats_asap_flag():
    order = _fresh_order()
    parsed = ParsedOrder(
        from_city="A", to_city="B", client_price=5000, pickup_asap=True,
        pickup_date="2026-10-05", pickup_time="15:00",
    )

    apply_parsed_fields(order, parsed)

    assert order.pickup_asap is False
    assert order.pickup_at == datetime(2026, 10, 5, 15, 0)


def test_edit_that_adds_a_time_clears_the_flag():
    order = _fresh_order()
    apply_parsed_fields(order, ParsedOrder(from_city="A", to_city="B", pickup_asap=True))
    assert order.pickup_asap is True

    apply_parsed_fields(
        order,
        ParsedOrder(from_city="A", to_city="B", pickup_date="2026-10-05", pickup_time="15:00"),
    )

    assert order.pickup_asap is False


def test_order_without_time_and_without_asap_stays_unspecified():
    order = _fresh_order()

    apply_parsed_fields(order, ParsedOrder(from_city="A", to_city="B", client_price=1000))

    assert order.pickup_asap is False and order.pickup_at is None


# --- Отображение --------------------------------------------------------------


def test_message_to_dispatcher_says_asap():
    order = Order(
        id=381, from_city="Осиново, ЛНР", to_city="Краснодар", pickup_at=None, pickup_asap=True,
        client_price=Decimal(25000), dispatcher_username="d",
    )

    assert dispatcher_message(order) == (
        "Здравствуйте! Заказ Осиново, ЛНР → Краснодар, в ближайшее время, 25000 ₽ — актуально?"
    )


def test_pickup_subtext_for_asap_is_empty_but_unknown_time_is_flagged():
    asap = Order(pickup_at=None, pickup_asap=True)
    unknown = Order(pickup_at=None, pickup_asap=False)

    assert pickup_subtext(asap) == ""
    assert pickup_subtext(unknown) == "время не указано"


async def test_detail_page_shows_asap_instead_of_not_specified(client, make_order):
    order = await make_order(from_city="Осиново", price="25000", pickup_at=None)
    order_id = order.id
    # make_order не знает про флаг — ставим его так, как это делает разбор.
    from app.db.base import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(Order, order_id)
        row.pickup_asap = True
        await db.commit()

    page = await client.get(f"/orders/{order_id}")

    assert page.status_code == 200
    assert "в ближайшее время" in page.text
    assert "не указана" not in page.text


async def test_feed_card_shows_asap_label(client, make_order):
    from app.db.base import SessionLocal

    order = await make_order(pickup_at=None)
    async with SessionLocal() as db:
        row = await db.get(Order, order.id)
        row.pickup_asap = True
        await db.commit()

    feed = await client.get("/")

    assert "В ближайшее время" in feed.text


async def test_asap_orders_sort_first_by_date_and_never_expire(session, make_order):
    from app.db.base import SessionLocal

    later = await make_order(pickup_at=now_msk_naive() + timedelta(hours=3))
    unknown = await make_order(pickup_at=None)
    asap = await make_order(pickup_at=None)
    async with SessionLocal() as db:
        row = await db.get(Order, asap.id)
        row.pickup_asap = True
        await db.commit()

    page = await queries.fetch_feed(session, sort="date", page_size=50)
    assert [o.id for o in page.items] == [asap.id, later.id, unknown.id]

    # Без времени подачи заявка не протухает: висит, пока её не возьмут.
    assert await expire_stale_orders(grace_hours=0) == 0
    refreshed = (await session.execute(select(Order).where(Order.id == asap.id))).scalar_one()
    assert refreshed.status == OrderStatus.NEW


# --- Сквозной сценарий: заказ №381 -------------------------------------------


async def test_order_381_end_to_end(session, monkeypatch):
    """LLM вернула то, что вернула на проде («25» и без времени), — заказ
    всё равно должен получиться с ценой 25000 и пометкой «в ближайшее время»."""

    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(
            orders=[
                ParsedOrder(
                    from_city="Осиново, ЛНР", to_city="Краснодар", passengers=1,
                    client_price=25, pickup_asap=True, is_urgent=True,
                    missing_fields=["нет времени подачи"],
                )
            ]
        )

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)

    ids = await work_group.upsert_order_text(chat_id=-100555, message_id=381, text=TEXT_381)

    order = (await session.execute(select(Order).where(Order.id == ids[0]))).scalar_one()
    assert order.client_price == Decimal(25000)
    assert order.pickup_asap is True and order.pickup_at is None
    assert order.status == OrderStatus.NEW


async def test_duplicate_detection_sees_normalized_price(session, make_order, monkeypatch):
    """Повтор заявки с «25» находит уже сохранённый заказ на 25000 —
    нормализация цены происходит ДО поиска дубля."""
    existing = await make_order(
        from_city="Осиново", to_city="Краснодар", price="25000",
        pickup_at=datetime(2026, 10, 5, 12, 0),
    )

    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(
            orders=[
                ParsedOrder(
                    from_city="Осиново", to_city="Краснодар", client_price=25,
                    pickup_date="2026-10-05", pickup_time="12:00",
                )
            ]
        )

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)

    ids = await work_group.upsert_order_text(chat_id=-100777, message_id=5, text="Осиново-Краснодар 25")

    assert ids == [existing.id]
    count = len((await session.execute(select(Order))).scalars().all())
    assert count == 1


# --- Время без даты: «сегодня» или «завтра» ----------------------------------

NOW = datetime(2026, 10, 1, 12, 0)


@pytest.mark.parametrize(
    ("now", "time_text", "expected"),
    [
        (datetime(2026, 10, 1, 12, 0), "15:40", datetime(2026, 10, 1, 15, 40)),  # впереди — сегодня
        (datetime(2026, 10, 1, 23, 50), "00:30", datetime(2026, 10, 2, 0, 30)),  # около полуночи — завтра
        (datetime(2026, 10, 1, 0, 10), "00:30", datetime(2026, 10, 1, 0, 30)),   # уже после полуночи — сегодня
        (datetime(2026, 10, 1, 12, 0), "11:00", datetime(2026, 10, 1, 11, 0)),   # час назад — ещё сегодня
        (datetime(2026, 10, 1, 12, 0), "08:00", datetime(2026, 10, 2, 8, 0)),    # давно прошло — завтра
    ],
)
def test_time_without_date_resolves_to_today_or_tomorrow(now, time_text, expected):
    assert resolve_pickup_at(None, time_text, None, now=now) == expected


def test_explicit_date_is_never_overridden():
    assert resolve_pickup_at("2026-10-05", "15:00", None, now=NOW) == datetime(2026, 10, 5, 15, 0)


@pytest.mark.parametrize("bad", [None, "", "утром", "25:99"])
def test_unusable_time_keeps_existing_value(bad):
    existing = datetime(2026, 10, 3, 9, 0)
    assert resolve_pickup_at(None, bad, existing, now=NOW) == existing
    assert resolve_pickup_at(None, bad, None, now=NOW) is None


def test_edit_with_new_time_keeps_the_original_day():
    """Правка сообщения на следующий день не должна сдвигать дату заказа."""
    existing = datetime(2026, 10, 3, 9, 0)

    assert resolve_pickup_at(None, "10:30", existing, now=NOW) == datetime(2026, 10, 3, 10, 30)


def test_apply_parsed_fields_uses_time_only_pickup():
    order = _fresh_order()

    apply_parsed_fields(order, ParsedOrder(from_city="Москва", to_city="Демянск", pickup_time="00:30"))

    assert order.pickup_at is not None and (order.pickup_at.hour, order.pickup_at.minute) == (0, 30)
    assert order.pickup_asap is False


def test_pickup_label():
    now = datetime(2026, 10, 1, 12, 0)

    def label(pickup_at, asap=False):
        return pickup_label(Order(pickup_at=pickup_at, pickup_asap=asap), now=now)

    assert label(datetime(2026, 10, 1, 15, 40)) == "сегодня в 15:40"
    assert label(datetime(2026, 10, 2, 0, 30)) == "завтра в 00:30"
    assert label(datetime(2026, 10, 7, 9, 5)) == "09:05, 07.10"
    assert label(None, asap=True) == "в ближайшее время"
    assert label(None) == "не указана"
