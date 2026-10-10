"""Предложения водителей и заявки с ❌❌❌ — не заказы (заказы №8351 и №8359 с прода)."""

import pytest
from sqlalchemy import select

from app.models import ActionLog, Order, OrderStatus
from app.parsing.llm_parser import ParseResult
from app.parsing.not_orders import has_cancel_mark, is_driver_offer, non_order_reason
from app.parsing.prefilter import prefilter
from app.parsing.schema import ParsedOrder
from app.services.cleanup import cancel_non_orders
from app.telegram import work_group

ORDER_8351 = (
    "В воскресенье 11.10 утром еду Горловка-Ростов.После обеда обратно Ростов-Горловка."
    "Есть места без пешего перехода."
)
ORDER_8359 = (
    "🚘🚖.Стандарт!!! Текущий! Время подачи!\n🗓️.10.10\n⏰.08:00\n🅰️.Коломна\n🅱️ ст метро Выхино\n"
    "💬  1 взр\n📈. 100 км\n💰 3200 на руки\n❌❌❌"
)


@pytest.mark.parametrize(
    "text",
    [
        ORDER_8351,
        "Еду Симферополь - Москва завтра, есть 2 свободных места",
        "Выезжаю в 6 утра Ростов-Краснодар, места есть",
        "Возьму 2 пассажиров Крым - Воронеж",
        "Ищу попутчиков Белгород Курск",
    ],
)
def test_driver_offers_are_detected(text):
    assert is_driver_offer(text)
    assert not prefilter(text).send_to_llm
    assert prefilter(text).reason == "driver_offer"


@pytest.mark.parametrize(
    "text",
    [
        "Краснодар - Анапа 18:00, 3 пассажира, 4000 руб, +79991112233",
        "Нужна машина Курск Тольятти 30000 водителю, 4 человека",
        "Заказ: Симферополь аэропорт, мест в багажнике нет, 1 чел",
        "Ищу машину Москва Тверь завтра утром",
        "Срочно! Адлер - Сочи 1 пасс 2500",
    ],
)
def test_real_orders_are_not_driver_offers(text):
    assert not is_driver_offer(text)
    assert prefilter(text).send_to_llm


def test_cancel_mark_needs_three_crosses():
    assert has_cancel_mark(ORDER_8359)
    assert has_cancel_mark("Курск Тольятти 30000 ❌ ❌ ❌")
    assert has_cancel_mark("Курск Тольятти ❌️❌️❌️")
    assert not has_cancel_mark("Курск Тольятти. Животные ❌ Курение ❌")
    assert not has_cancel_mark("Курск Тольятти 30000 ❌❌")


def test_non_order_reason():
    assert non_order_reason(ORDER_8359) == "cancel_mark"
    assert non_order_reason(ORDER_8351) == "driver_offer"
    assert non_order_reason("Курск Тольятти 30000") is None


async def test_edit_with_crosses_cancels_the_order(session, make_order, monkeypatch):
    order = await make_order(from_city="Коломна", to_city="Выхино", price="3200", raw_text="Коломна Выхино 3200")
    order.source_chat_id, order.source_message_id, order.source_sub_index = -100960, 7, 0
    await session.commit()

    async def no_llm(text):
        raise AssertionError("LLM не нужна: заказ закрыт крестами")

    monkeypatch.setattr(work_group, "parse_orders", no_llm)

    await work_group.upsert_order_text(chat_id=-100960, message_id=7, text=ORDER_8359, is_edit=True)

    await session.refresh(order)
    assert order.status == OrderStatus.CANCELLED


async def test_new_driver_offer_creates_no_order_and_no_llm_call(session, monkeypatch):
    async def no_llm(text):
        raise AssertionError("предложение водителя не должно доходить до LLM")

    monkeypatch.setattr(work_group, "parse_orders", no_llm)

    ids = await work_group.upsert_order_text(chat_id=-100961, message_id=1, text=ORDER_8351)

    assert ids == []
    assert (await session.execute(select(Order))).scalars().all() == []


async def test_cleanup_cancels_saved_non_orders_but_not_real_or_taken_ones(session, make_order):
    driver = await make_order(from_city="Горловка", to_city="Ростов", raw_text=ORDER_8351)
    crossed = await make_order(from_city="Коломна", to_city="Выхино", raw_text=ORDER_8359)
    real = await make_order(from_city="Курск", to_city="Тольятти", raw_text="Курск Тольятти 30000 водителю")
    taken = await make_order(from_city="Горловка", to_city="Ростов", raw_text=ORDER_8351, taken_by_token="tg:1")

    assert await cancel_non_orders() == 2

    for order in (driver, crossed, real, taken):
        await session.refresh(order)
    assert driver.status == OrderStatus.CANCELLED and crossed.status == OrderStatus.CANCELLED
    assert real.status == OrderStatus.NEW
    assert taken.status != OrderStatus.CANCELLED  # заказ водителя не трогаем
    actions = {a.action for a in (await session.execute(select(ActionLog))).scalars().all()}
    assert {"cancelled_driver_offer", "cancelled_cancel_mark"} <= actions
    assert await cancel_non_orders() == 0  # повторный проход ничего не находит


async def test_prompt_example_does_not_break_parse_result_type():
    # страховка: ParseResult/ParsedOrder по-прежнему собираются (схема не менялась)
    assert ParseResult(orders=[ParsedOrder(from_city="А", to_city="Б")]).orders


def test_cross_next_to_a_closing_word_cancels_but_a_bare_cross_does_not():
    assert has_cancel_mark("62000+ платка 💰\n\n❌закрыт❌")  # заказ №6373
    assert has_cancel_mark("ОТМЕНА КЛИЕНТОМ ❌")
    assert not has_cancel_mark("₽ + платная дорога + страховка❌❌")  # заказ №8267: страховка не включена
    assert not has_cancel_mark("Животные ❌")


ORDER_5584 = (
    "📅 Заказ на 16.10.2026 в 10:00\n\n📍 Откуда: Мариуполь\n\n🏁 Куда: Волгоград\n\n👥 Пассажиров: 2\n\n"
    "🤝 Водителю на руки: 20 700 ₽\n\n⚠️ Заказ закрыт!\n\n🔒🔒🔒"
)


async def test_cleanup_cancels_orders_closed_with_a_lock_before_the_rule_existed(session, make_order):
    closed = await make_order(from_city="Мариуполь", to_city="Волгоград", raw_text=ORDER_5584)
    open_lock = await make_order(from_city="Курск", to_city="Тольятти", raw_text="Курск Тольятти 30000 🔓 свободно")
    insurance = await make_order(from_city="Курск", to_city="Тольятти", raw_text="30000 + страховка❌❌")

    assert await cancel_non_orders() == 1

    for order in (closed, open_lock, insurance):
        await session.refresh(order)
    assert closed.status == OrderStatus.CANCELLED
    assert open_lock.status == OrderStatus.NEW and insurance.status == OrderStatus.NEW
    actions = {a.action for a in (await session.execute(select(ActionLog))).scalars().all()}
    assert "cancelled_closed_lock" in actions
