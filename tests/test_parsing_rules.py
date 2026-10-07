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
from app.parsing.schema import ParsedOrder, normalize_price
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
        from_city="Осиново", to_city="Краснодар", client_price=25,
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
        from_city="Осиново",
        missing_fields=["нет времени подачи", "не указана стоимость"],
    )

    apply_parsed_fields(order, parsed)

    assert order.status == OrderStatus.NEEDS_CLARIFICATION


def test_named_time_beats_asap_flag():
    order = _fresh_order()
    parsed = ParsedOrder(
        from_city="A", to_city="B", client_price=5000,
        pickup_date="2026-10-05", pickup_time="15:00",
    )

    apply_parsed_fields(order, parsed)

    assert order.pickup_asap is False
    assert order.pickup_at == datetime(2026, 10, 5, 15, 0)


def test_edit_that_adds_a_time_clears_the_flag():
    order = _fresh_order()
    apply_parsed_fields(order, ParsedOrder(from_city="A", to_city="B"))
    assert order.pickup_asap is True

    apply_parsed_fields(
        order,
        ParsedOrder(from_city="A", to_city="B", pickup_date="2026-10-05", pickup_time="15:00"),
    )

    assert order.pickup_asap is False


def test_order_without_any_time_is_treated_as_asap():
    """«3ч» без времени суток, «сейчас» или время просто не названо — одно и то
    же: заказ «в ближайшее время»."""
    order = _fresh_order()

    apply_parsed_fields(order, ParsedOrder(from_city="A", to_city="B", client_price=1000))

    assert order.pickup_asap is True and order.pickup_at is None
    assert order.status == OrderStatus.NEW


# --- Отображение --------------------------------------------------------------


def test_message_to_dispatcher_says_asap():
    order = Order(
        id=381, from_city="Осиново, ЛНР", to_city="Краснодар", pickup_at=None, pickup_asap=True,
        client_price=Decimal(25000), dispatcher_username="d",
    )

    assert dispatcher_message(order) == (
        "Здравствуйте! Заказ Осиново, ЛНР → Краснодар, в ближайшее время, 25000 ₽ — актуально?"
    )


def test_pickup_subtext_is_empty_without_time():
    assert pickup_subtext(Order(pickup_at=None, pickup_asap=True)) == ""
    assert pickup_subtext(Order(pickup_at=None, pickup_asap=False)) == ""


def test_message_says_asap_even_if_flag_is_missing():
    order = Order(id=7, from_city="Краснодар", to_city="Алушта", pickup_at=None, client_price=Decimal(10000))

    assert "в ближайшее время" in dispatcher_message(order)


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
                    client_price=25, is_urgent=True,
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


async def _asap_parse(monkeypatch, price=3000):
    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(
            orders=[
                ParsedOrder(
                    from_city="посёлок Ленинский", to_city="Ревда", client_price=price,
                    is_urgent=True,
                )
            ]
        )

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)


async def test_asap_order_posted_in_two_groups_is_one_order(session, monkeypatch):
    """Диспетчер разослал «НА СЕЙЧАС» в две группы: времени подачи нет,
    но маршрут и цена те же — второй заказ не создаётся."""
    await _asap_parse(monkeypatch)

    first = await work_group.upsert_order_text(chat_id=-100001, message_id=1, text="НА СЕЙЧАС Ленинский-Ревда 3000")
    second = await work_group.upsert_order_text(chat_id=-100002, message_id=9, text="НА СЕЙЧАС Ленинский-Ревда 3000")

    assert second == first
    assert len((await session.execute(select(Order))).scalars().all()) == 1


async def test_asap_orders_with_different_price_are_not_duplicates(session, monkeypatch):
    await _asap_parse(monkeypatch, price=3000)
    await work_group.upsert_order_text(chat_id=-100001, message_id=1, text="НА СЕЙЧАС Ленинский-Ревда 3000")
    await _asap_parse(monkeypatch, price=3500)
    await work_group.upsert_order_text(chat_id=-100002, message_id=9, text="НА СЕЙЧАС Ленинский-Ревда 3500")

    assert len((await session.execute(select(Order))).scalars().all()) == 2


async def test_old_asap_order_is_not_a_duplicate_of_a_new_one(session, monkeypatch):
    """Позавчерашний висящий «сейчас» не должен поглощать сегодняшнюю заявку (повторы — в сутки)."""
    await _asap_parse(monkeypatch)
    ids = await work_group.upsert_order_text(chat_id=-100001, message_id=1, text="НА СЕЙЧАС Ленинский-Ревда 3000")
    old = (await session.execute(select(Order).where(Order.id == ids[0]))).scalar_one()
    old.created_at = old.created_at - timedelta(hours=25)
    await session.commit()

    await work_group.upsert_order_text(chat_id=-100002, message_id=9, text="НА СЕЙЧАС Ленинский-Ревда 3000")

    assert len((await session.execute(select(Order))).scalars().all()) == 2


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
    assert label(None) == "в ближайшее время"  # время определить не удалось


# --- Объявления без маршрута (не перевозка пассажиров) -------------------------


def _patch_parser(monkeypatch, orders):
    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(orders=orders)

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)


async def test_announcement_without_route_never_becomes_an_order(session, monkeypatch):
    """«Нужен исполнитель на 4 часа 12000р» — не заказ такси, в ленту не попадает."""
    _patch_parser(monkeypatch, [ParsedOrder(client_price=12000)])

    ids = await work_group.upsert_order_text(
        chat_id=-100901, message_id=1, text="Нужен исполнитель на 4 часа 12000р"
    )

    assert ids == []
    assert (await session.execute(select(Order))).scalars().all() == []


async def test_same_routeless_message_from_three_groups_creates_nothing(session, monkeypatch):
    _patch_parser(monkeypatch, [ParsedOrder(client_price=12000)])

    for chat in (-100911, -100912, -100913):
        await work_group.upsert_order_text(chat_id=chat, message_id=5, text="Нужен исполнитель на 4 часа 12000р")

    assert (await session.execute(select(Order))).scalars().all() == []


async def test_order_with_only_one_city_is_still_kept(session, monkeypatch):
    _patch_parser(monkeypatch, [ParsedOrder(from_city="Краснодар", client_price=5000)])

    ids = await work_group.upsert_order_text(chat_id=-100921, message_id=1, text="Краснодар -> ? 5000")

    assert len(ids) == 1


async def test_message_with_a_route_and_a_routeless_part_keeps_only_the_route(session, monkeypatch):
    _patch_parser(
        monkeypatch,
        [ParsedOrder(client_price=3000), ParsedOrder(from_city="Ялта", to_city="Керчь", client_price=7000)],
    )

    ids = await work_group.upsert_order_text(chat_id=-100931, message_id=1, text="две заявки")

    orders = (await session.execute(select(Order))).scalars().all()
    assert len(ids) == 1 and [(o.from_city, o.to_city) for o in orders] == [("Ялта", "Керчь")]
    assert orders[0].source_sub_index == 1  # индекс сохранён: правка сообщения найдёт свой заказ


# --- «4000 тыс»: тысячи применили дважды ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4000000", "4000"),  # «4000 тыс»
        ("12000000", "12000"),
        ("7500000", "7500"),
        ("2000000", "2000"),
        ("4000000000", "4000"),  # дважды
        ("300000", "300"),  # граница: уже не цена поездки
        ("250000", "250000"),  # правдоподобно (дальняя поездка) — не трогаем
        ("52000", "52000"),
        ("299999", "299999"),
    ],
)
def test_price_with_thousands_applied_twice_is_fixed(raw, expected):
    assert str(ParsedOrder(client_price=raw).client_price) == expected


def test_absurd_price_that_cannot_be_fixed_becomes_unknown():
    assert ParsedOrder(client_price="1234567").client_price is None
    assert ParsedOrder(client_price="4500500.5").client_price is None


def test_fixed_price_is_normalized():
    order = ParsedOrder(from_city="Москва", to_city="Тула", client_price="4000000")

    assert order.client_price == Decimal(4000)


def test_migration_rule_matches_the_parser_rule():
    """Правило в миграции скопировано (она не зависит от кода приложения) — сверяем."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "b9f5e7a2c4d6_fix_double_thousand_prices.py"
    spec = importlib.util.spec_from_file_location("price_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for raw in ("4000000", "12000000", "300000", "250000", "1234567", "4000000000", "52000"):
        assert module._fixed(Decimal(raw)) == normalize_price(Decimal(raw)), raw
