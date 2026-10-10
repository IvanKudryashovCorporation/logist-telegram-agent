"""Дубли заявок: почему возникали (скриншоты с прода) и что их теперь склеивает.

Причины:
1. заявка с временем без даты («20:00 Элиста — Ростов») искалась как «срочная» и не
   находила уже сохранённую (с датой «сегодня/завтра»);
2. диспетчер меняет цену и публикует заново — а цена была обязательным условием;
3. то же место, написанное по-разному («Поселок/Посёлок», «М.О./М. О.», «Родионцева/Родионцево»);
4. тот же рейс из разных групп.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.city_aliases import city_key
from app.models import ActionLog, Order
from app.parsing.llm_parser import ParseResult
from app.parsing.schema import ParsedOrder
from app.telegram import work_group
from app.timeutil import now_msk_naive

DISPATCHER = {"dispatcher_tg_id": 777, "dispatcher_username": "fresh_taxi"}


def _parse(monkeypatch, **fields):
    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(orders=[ParsedOrder(**fields)])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)


async def _post(chat_id, message_id, text=None, **dispatcher):
    # Разные сообщения — разные тексты (реальный текст даёт один и тот же разбор, а точные копии
    # отсеиваются до LLM); ответ LLM в этих тестах подменяется, поэтому текст делаем уникальным.
    text = text or f"заявка {chat_id} {message_id}"
    return await work_group.upsert_order_text(chat_id=chat_id, message_id=message_id, text=text, **dispatcher)


async def _count(session) -> int:
    return (await session.execute(select(func.count(Order.id)))).scalar_one()


# --- 1. Время без даты ----------------------------------------------------------------------


async def test_time_only_orders_from_two_groups_are_one_order(session, monkeypatch):
    """Элиста → Ростов 16000, «20:00» без даты, две группы: раньше два заказа."""
    _parse(monkeypatch, pickup_time="20:00", from_city="Элиста", to_city="Ростов", client_price=16000)

    first = await _post(-100111, 1)
    second = await _post(-100222, 7)

    assert second == first
    assert await _count(session) == 1


async def test_time_only_duplicate_is_found_even_without_a_known_dispatcher(session, monkeypatch):
    """Строгая проверка (маршрут + цена + время) тоже обязана работать для времени без даты."""
    _parse(monkeypatch, pickup_time="06:00", from_city="Ясиноватая", to_city="Ростов-на-Дону", client_price=9000)

    await _post(-100111, 1)
    await _post(-100222, 7)

    assert await _count(session) == 1


# --- 2. Диспетчер меняет цену ---------------------------------------------------------------


async def test_same_dispatcher_changing_the_price_updates_the_order(session, monkeypatch):
    _parse(monkeypatch, pickup_time="19:30", from_city="Краснодар", to_city="Маяковского", client_price=5400)
    first = await _post(-100111, 1, **DISPATCHER)
    _parse(monkeypatch, pickup_time="19:30", from_city="Краснодар", to_city="Маяковского", client_price=6300)

    second = await _post(-100111, 2, **DISPATCHER)

    assert second == first
    order = (await session.execute(select(Order))).scalar_one()
    await session.refresh(order)
    assert order.client_price == 6300  # актуальна новая цена
    actions = (await session.execute(select(ActionLog.action))).scalars().all()
    assert "duplicate_price_updated" in actions


async def test_different_dispatchers_with_different_prices_are_separate_offers(session, monkeypatch):
    _parse(monkeypatch, pickup_time="06:00", from_city="Ясиноватая", to_city="Ростов-на-Дону", client_price=9450)
    await _post(-100111, 1, dispatcher_tg_id=1, dispatcher_username="obtatki_ug_bot")
    _parse(monkeypatch, pickup_time="06:00", from_city="Ясиноватая", to_city="Ростов-на-Дону", client_price=9000)

    await _post(-100222, 2, dispatcher_tg_id=2, dispatcher_username="UgtravelBot")

    assert await _count(session) == 2


async def test_same_dispatcher_but_a_different_client_is_a_different_order(session, monkeypatch):
    _parse(monkeypatch, pickup_time="12:00", from_city="Гагра", to_city="Сочи", client_price=1800,
           client_phone="+79990000001", passengers=2)
    await _post(-100111, 1, **DISPATCHER)
    _parse(monkeypatch, pickup_time="12:00", from_city="Гагра", to_city="Сочи", client_price=1800,
           client_phone="+79990000002", passengers=2)

    await _post(-100111, 2, **DISPATCHER)

    assert await _count(session) == 2


async def test_same_dispatcher_other_time_or_route_is_a_different_order(session, monkeypatch):
    _parse(monkeypatch, pickup_time="12:00", from_city="Гагра", to_city="Сочи", client_price=1800)
    await _post(-100111, 1, **DISPATCHER)
    _parse(monkeypatch, pickup_time="18:00", from_city="Гагра", to_city="Сочи", client_price=2000)
    await _post(-100111, 2, **DISPATCHER)  # другое время (и цена: при той же цене — дубль в пределах суток)
    _parse(monkeypatch, pickup_time="12:00", from_city="Гагра", to_city="Адлер", client_price=1800)
    await _post(-100111, 3, **DISPATCHER)  # другой маршрут

    assert await _count(session) == 3


# --- 3. Одно место — разное написание --------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Поселок Грицовский (Тульская область)", "Посёлок Грицовский (Тульская область)"),
        ("деревня Родионцева (М.О.)", "деревня Родионцева (М. О.)"),
        ("с.Лев Толстое( Дзержинский район, Калужская обл.)", "Лев Толстое (Дзержинский район, Калужская обл.)"),
        ("ст. Ладожская", "станица Ладожская"),
        ("Г. Симферополь", "симферополь"),
    ],
)
def test_city_key_ignores_yo_dots_and_settlement_type(first, second):
    assert city_key(first) == city_key(second)


def test_city_key_still_tells_different_cities_apart():
    assert city_key("Ростов-на-Дону") != city_key("Ростов Великий")
    assert city_key("Сочи") != city_key("Адлер")


async def test_same_dispatcher_loose_spelling_declension_and_prefix(session, monkeypatch):
    """«деревня Родионцева (М.О.)» и «Родионцево (Московская обл.)» — одно место."""
    _parse(monkeypatch, from_city="деревня Родионцева (М.О.)", to_city="Москва", client_price=4500)
    first = await _post(-100111, 1, dispatcher_tg_id=5, dispatcher_username="VezdeSvoi25")
    _parse(monkeypatch, from_city="Родионцево (Московская обл., Истринский р-н)", to_city="Москва", client_price=4500)

    second = await _post(-100222, 9, dispatcher_tg_id=5, dispatcher_username="VezdeSvoi25")

    assert second == first
    assert await _count(session) == 1


# --- 4. Один рейс — разные группы -------------------------------------------------------------


async def test_asap_order_reposted_to_another_group_by_the_same_dispatcher(session, monkeypatch):
    _parse(monkeypatch, from_city="Сосновка (ЛНР)", to_city="Поселок Грицовский (Тульская область)", client_price=18000)
    first = await _post(-100111, 1, dispatcher_tg_id=9, dispatcher_username="Transfer_R_F")
    _parse(monkeypatch, from_city="Сосновка (ЛНР)", to_city="Посёлок Грицовский (Тульская область)", client_price=18000)

    second = await _post(-100222, 5, dispatcher_tg_id=9, dispatcher_username="Transfer_R_F")

    assert second == first


# --- Миграция: уборка накопившихся дублей ----------------------------------------------------


def _migration():
    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "d1b7a9c4e6f8_dedupe_and_city_keys.py"
    spec = importlib.util.spec_from_file_location("dedupe_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(order_id, tg, name, frm, to, pickup, *, asap=False, phone=None, pax=None, created=None):
    return (order_id, tg, name, frm, to, pickup, asap, phone, pax, created or datetime(2026, 10, 4, 12, 0))


def test_migration_keeps_the_newest_of_a_dispatchers_repeats():
    pickup = datetime(2026, 10, 5, 19, 30)
    rows = [
        _row(1039, 7, "UgtravelBot", "Краснодар", "Маяковского", pickup),
        _row(1387, 7, "UgtravelBot", "Краснодар", "Маяковского", pickup),
        _row(1500, 7, "UgtravelBot", "Краснодар", "Севастополь", pickup),  # другой маршрут
        _row(2003, 1, "obtatki_ug_bot", "Краснодар", "Маяковского", pickup),  # другой диспетчер
    ]

    assert _migration().duplicate_ids(rows) == [1039]


def test_migration_handles_asap_spellings_and_different_clients():
    base = datetime(2026, 10, 4, 6, 0)
    rows = [
        _row(1661, 5, "VezdeSvoi25", "деревня Родионцева (М.О.)", "Москва", None, asap=True, created=base),
        _row(1662, 5, "VezdeSvoi25", "деревня Родионцева (М. О.)", "Москва", None, asap=True, created=base),
        _row(1800, 6, "disp", "Гагра", "Сочи", datetime(2026, 10, 6, 12, 0), phone="+7999000001"),
        _row(1801, 6, "disp", "Гагра", "Сочи", datetime(2026, 10, 6, 12, 0), phone="+7999000002"),
    ]

    assert _migration().duplicate_ids(rows) == [1661]


def test_migration_works_with_iso_strings_from_sqlite():
    rows = [
        _row(1, 7, "d", "Элиста", "Ростов", "2026-10-04 20:00:00"),
        _row(2, 7, "d", "Элиста", "Ростов-на-Дону", "2026-10-04 20:00:00"),
    ]

    assert _migration().duplicate_ids(rows) == [1]


# --- Одинаковый текст без известного диспетчера -----------------------------------------------


async def test_identical_text_from_another_group_is_a_repost_even_without_a_dispatcher(session, monkeypatch):
    """«Сатка → село Париж» без цены: у одного сообщения диспетчер не определился."""
    _parse(monkeypatch, from_city="Сатка (Челябинская область)", to_city="село Париж (Челябинская область)")
    text = "Сейчас +-один час\n\nОткуда Сатка Челябинская область\n\nКуда село Париж Челябинская область"
    first = await _post(-100111, 1, text=text)
    second = await _post(-100222, 4, text=text, dispatcher_tg_id=3, dispatcher_username="MYRAZ888_123_rus")

    assert second == first
    assert await _count(session) == 1


async def test_different_text_without_a_dispatcher_is_not_merged(session, monkeypatch):
    _parse(monkeypatch, from_city="Гагра", to_city="Сочи")
    await _post(-100111, 1, text="Гагра Сочи срочно, багаж большой, кто на месте")
    await _post(-100222, 2, text="Гагра Сочи сейчас, нужен комфорт, два пассажира")

    assert await _count(session) == 2


# --- Страховка: фоновая уборка повторов -------------------------------------------------------


async def test_background_pass_cancels_a_repost_that_slipped_through(session, make_order):
    from app.services.dedupe import cancel_duplicate_orders

    pickup = now_msk_naive() + timedelta(days=1)
    older = await make_order(from_city="Гагра", to_city="Сочи", price="1800", pickup_at=pickup,
                             dispatcher_username="UgtravelBot", client_phone=None, passengers=None)
    newer = await make_order(from_city="Гагра", to_city="Сочи", price="2700", pickup_at=pickup,
                             dispatcher_username="UgtravelBot", client_phone=None, passengers=None)
    other = await make_order(from_city="Гагра", to_city="Сочи", price="2700", pickup_at=pickup,
                             dispatcher_username="another_one", client_phone=None, passengers=None,
                             raw_text="Другой диспетчер, свой текст объявления про этот же маршрут")
    other.dispatcher_tg_id = 222  # фабрика ставит всем 111 — делаем другого диспетчера
    await session.commit()
    ids = (older.id, newer.id, other.id)

    assert await cancel_duplicate_orders() == 1

    from app.db.base import SessionLocal

    async with SessionLocal() as fresh:
        statuses = {i: (await fresh.get(Order, i)).status.value for i in ids}
        actions = (await fresh.execute(select(ActionLog.action))).scalars().all()
    assert statuses == {older.id: "cancelled", newer.id: "new", other.id: "new"}
    assert "duplicate_cancelled" in actions
    assert await cancel_duplicate_orders() == 0  # повторный проход ничего не трогает


async def test_background_pass_leaves_taken_orders_alone(session, make_order):
    from app.services.dedupe import cancel_duplicate_orders

    pickup = now_msk_naive() + timedelta(days=1)
    await make_order(price="1800", pickup_at=pickup, dispatcher_username="d", taken_by_token="driver",
                     client_phone=None, passengers=None)
    await make_order(price="1800", pickup_at=pickup, dispatcher_username="d", client_phone=None, passengers=None)

    assert await cancel_duplicate_orders() == 0


# --- 5. Правка сообщения делает заявку повтором другой ---------------------------------------


async def _edit(chat_id, message_id, text="заявка", **dispatcher):
    return await work_group.upsert_order_text(
        chat_id=chat_id, message_id=message_id, text=text, is_edit=True, **dispatcher
    )


async def _live_ids(session):
    from app.models import OrderStatus

    return {
        order.id
        for order in (await session.execute(select(Order).where(Order.status == OrderStatus.NEW))).scalars().all()
    }


async def test_edit_that_turns_an_order_into_a_repost_removes_the_twin_at_once(session, monkeypatch):
    """Заявка B опубликована с другим маршрутом, потом отредактирована — и стала копией A."""
    route = {"pickup_time": "18:00", "from_city": "Краснодар", "to_city": "Анапа", "client_price": 4000}
    _parse(monkeypatch, **route)
    first = (await _post(-100111, 1, **DISPATCHER))[0]
    _parse(monkeypatch, **{**route, "to_city": "Геленджик"})
    second = (await _post(-100111, 2, **DISPATCHER))[0]
    assert await _live_ids(session) == {first, second}

    _parse(monkeypatch, **route)  # диспетчер исправил маршрут во втором сообщении
    await _edit(-100111, 2, **DISPATCHER)

    assert await _live_ids(session) == {second}  # осталась отредактированная, двойник снят
    action = (
        await session.execute(select(ActionLog).where(ActionLog.action == "duplicate_cancelled_on_edit"))
    ).scalar_one()
    assert action.order_id == first


async def test_edit_does_not_touch_a_taken_order(session, monkeypatch):
    route = {"pickup_time": "18:00", "from_city": "Краснодар", "to_city": "Анапа", "client_price": 4000}
    _parse(monkeypatch, **route)
    first = (await _post(-100111, 1, **DISPATCHER))[0]
    taken = (await session.execute(select(Order).where(Order.id == first))).scalar_one()
    taken.taken_by_token = "tg:1"
    session.add(taken)
    await session.commit()
    _parse(monkeypatch, **{**route, "to_city": "Геленджик"})
    second = (await _post(-100111, 2, **DISPATCHER))[0]

    _parse(monkeypatch, **route)
    await _edit(-100111, 2, **DISPATCHER)

    assert await _live_ids(session) == {first, second}


async def test_edit_to_a_different_client_stays_a_separate_order(session, monkeypatch):
    route = {"pickup_time": "18:00", "from_city": "Краснодар", "to_city": "Анапа", "client_price": 4000}
    _parse(monkeypatch, **route, client_phone="+79990000001")
    first = (await _post(-100111, 1, **DISPATCHER))[0]
    _parse(monkeypatch, **{**route, "to_city": "Геленджик"}, client_phone="+79990000002")
    second = (await _post(-100111, 2, **DISPATCHER))[0]

    _parse(monkeypatch, **route, client_phone="+79990000002")
    await _edit(-100111, 2, **DISPATCHER)

    assert await _live_ids(session) == {first, second}
