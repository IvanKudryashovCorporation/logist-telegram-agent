"""Шаблонный разбор заявок бота ProPoisk_zakazov: формат разбирается без LLM, остальное уходит в LLM + уведомление."""

import asyncio
from datetime import date

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import Order, ParseStat
from app.parsing import template_alerts
from app.parsing.bot_noise import strip_bot_noise
from app.parsing.llm_parser import ParseResult
from app.parsing.pro_template import FALLBACK_ERROR_PREFIX, parse_pro_message
from app.parsing.schema import ParsedOrder
from app.telegram import work_group

CHAT = -1004413141153
TODAY = date(2026, 10, 27)

SAMPLE = """✅ Заказ · ⚡ СРОЧНО · сегодня
📍 Сочи → Анапа
👥 1 чел · 💰 8 000 ₽
🚗 Класс: Стандарт
🕒 Опубликован: только что

Сообщение диспетчеру уже вписано, проверьте и отправьте:
Здравствуйте! Возьму заказ сегодня Сочи-Анапа 8 000 ₽
👤 Диспетчер: @IURII1606
ℹ️ Группа закрытая: «Источник» откроется, если вы в ней состоите."""


def one(text: str):
    outcome = parse_pro_message(text, today=TODAY)
    assert outcome.result is not None, outcome.reason
    assert len(outcome.result.orders) == 1
    return outcome.result.orders[0]


def test_sample_from_group():
    order = one(SAMPLE)
    assert (order.from_city, order.to_city) == ("Сочи", "Анапа")
    assert order.client_price == 8000
    assert order.passengers == 1
    assert order.is_urgent is True
    assert order.pickup_date == "2026-10-27" and order.pickup_time is None
    assert order.car_class is None  # «Стандарт» — обычная машина
    assert order.via_points == []


def test_draft_line_on_next_line_is_noise():
    cleaned = strip_bot_noise(SAMPLE)
    assert "Возьму заказ" not in cleaned and "Опубликован" not in cleaned
    assert "@IURII1606" in cleaned


def test_date_and_time_in_header():
    order = one("✅ Заказ  ·  29.10 в 01:30\n📍 Сальск → Ростов-на-Дону\n💰 5 000 ₽\n👤 Диспетчер: @annakoropenko")
    assert order.pickup_date == "2026-10-29"
    assert one("✅ Заказ  ·  03.01 в 01:30\n📍 Сальск → Ростов-на-Дону\n💰 5 000 ₽").pickup_date == "2027-01-03"
    assert order.pickup_time == "01:30"
    assert order.is_urgent is False
    assert order.passengers is None


def test_tomorrow_with_time_and_class():
    order = one("✅ Заказ · завтра в 9:05\n📍 Краснодар → Сочи\n👥 4 чел · 💰 12 500 ₽\n🚗 Класс: Минивэн")
    assert (order.pickup_date, order.pickup_time) == ("2026-10-28", "09:05")
    assert order.car_class == "Минивэн"
    assert order.passengers == 4


def test_date_without_year_in_near_future():
    order = one("✅ Заказ · 30.10 в 14:00\n📍 Керчь → Симферополь\n💰 6000 ₽")
    assert order.pickup_date == "2026-10-30"


def test_via_points_and_address():
    order = one("✅ Заказ · сегодня в 18:00\n📍 Пермь → Соликамск → Пермь, аэропорт Большое Савино\n💰 9 000 ₽")
    assert order.from_city == "Пермь"
    assert order.via_points == ["Соликамск"]
    assert order.to_city == "Пермь" and order.to_address == "аэропорт Большое Савино"


def test_address_and_info_lines_from_real_message():
    text = (
        "✅ Заказ  ·  28.10 в 12:00\n📍 Псков → Печоры\n"
        "📌 Куда, как в заказе: Садово-дачное товарищество Куничина Гора (Псковская обл., Печор.р-н)\n"
        "👥 1 чел · 💰 3 500 ₽\n🚗 Класс: Стандарт\nℹ️ 500\n\n👤 Диспетчер: @liderdisp"
    )
    order = one(text)
    assert (order.from_city, order.to_city, order.client_price) == ("Псков", "Печоры", 3500)
    assert order.to_address.startswith("Садово-дачное товарищество Куничина Гора")


def test_time_window_in_header():
    order = one("✅ Заказ  ·  29.10 10:00–11:00\n📍 Москва → Жерновка\n👥 2 чел · 💰 4 000 ₽\n🚗 Класс: Стандарт\nℹ️ два маленьких попугая в переноске\n👤 Диспетчер: @MosVipTaxi")
    assert (order.pickup_date, order.pickup_time) == ("2026-10-29", "10:00")
    assert order.passengers == 2


def test_no_time_marker_in_header():
    order = one("✅ Заказ · завтра · время не указано\n📍 Москва → Тула\n💰 5 000 ₽")
    assert (order.pickup_date, order.pickup_time) == ("2026-10-28", None)
    order = one("✅ Заказ · 30.10 время не указано\n📍 Москва → Тула\n💰 5 000 ₽")
    assert order.pickup_date == "2026-10-30"


def test_non_breaking_spaces_in_price():
    assert one("✅ Заказ · сегодня\n📍 Сочи → Анапа\n💰 12 000 ₽").client_price == 12000


def test_taken_message_is_not_an_order_and_not_a_failure():
    outcome = parse_pro_message("⛔️ Заказ снят/взят\n⚡️ СРОЧНО Ровеньки → Старый Оскол, 8 000 ₽", today=TODAY)
    assert outcome.result is not None and outcome.result.orders == []


@pytest.mark.parametrize(
    "text, reason_part",
    [
        ("✅ Заказ · сегодня\n💰 5 000 ₽", "маршрута"),
        ("✅ Заказ · сегодня\n📍 Сочи → Анапа", "цены"),
        ("✅ Заказ · сегодня\n📍 Сочи → Анапа\n💰 по запросу", "цена не число"),
        ("✅ Заказ · сегодня\n📍 Сочи\n💰 5 000 ₽", "меньше двух"),
        ("✅ Заказ · когда-нибудь\n📍 Сочи → Анапа\n💰 5 000 ₽", "заголовка"),
        ("✅ Заказ · сегодня\n📍 Сочи → Анапа\n💰 5 000 ₽\n🧳 Багаж: 3 чемодана", "незнакомая строка"),
        ("✅ Заказ · 31.02 в 10:00\n📍 Сочи → Анапа\n💰 5 000 ₽", "дата"),
    ],
)
def test_unparseable_formats_fail_with_reason(text, reason_part):
    outcome = parse_pro_message(text, today=TODAY)
    assert outcome.result is None and outcome.bot_format
    assert reason_part in outcome.reason


def test_chatter_is_not_bot_format():
    outcome = parse_pro_message("Коллеги, кто сегодня едет в Сочи?", today=TODAY)
    assert outcome.result is None and not outcome.bot_format


# --- Встраивание в upsert_order_text -------------------------------------------------------------------------


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    template_alerts.reset()
    monkeypatch.setattr(template_alerts, "sender", fake_send)
    monkeypatch.setattr(work_group.settings, "priority_chat_ids", str(CHAT))
    monkeypatch.setattr(work_group.settings, "parse_stats_enabled", True)
    monkeypatch.setattr(work_group.settings, "template_alert_per_hour", 2)
    yield sent
    template_alerts.reset()


@pytest.fixture
def llm_calls(monkeypatch):
    calls: list[str] = []

    async def fake_parse_orders(text, **kwargs):
        calls.append(text)
        return ParseResult(
            orders=[ParsedOrder(from_city="Сочи", to_city="Анапа", client_price=7000)], model="fake-llm"
        )

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    return calls


async def stats() -> list[ParseStat]:
    async with SessionLocal() as db:
        return list((await db.execute(select(ParseStat).order_by(ParseStat.id))).scalars().all())


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_template_creates_order_without_llm(alerts, llm_calls):
    ids = await work_group.upsert_order_text(chat_id=CHAT, message_id=1, text=SAMPLE)
    await settle()
    assert len(ids) == 1 and llm_calls == [] and alerts == []
    async with SessionLocal() as db:
        order = await db.get(Order, ids[0])
    assert (order.from_city, order.to_city, order.client_price) == ("Сочи", "Анапа", 8000)
    assert order.contact_username == "IURII1606"
    assert order.is_urgent and order.pickup_asap
    (stat,) = await stats()
    assert stat.model == "template" and stat.error is None and stat.orders_found == 1


async def test_template_failure_goes_to_llm_and_alerts_owner(alerts, llm_calls):
    text = "✅ Заказ · сегодня\n📍 Сочи → Анапа\n💰 по запросу\n👤 Диспетчер: @IURII1606"
    ids = await work_group.upsert_order_text(chat_id=CHAT, message_id=2, text=text)
    await settle()
    assert len(ids) == 1 and len(llm_calls) == 1
    assert len(alerts) == 1
    assert "по запросу" in alerts[0] and "цена не число" in alerts[0] and str(CHAT) in alerts[0]
    (stat,) = await stats()
    assert stat.model == "fake-llm" and stat.error.startswith(FALLBACK_ERROR_PREFIX)


async def test_chatter_goes_to_llm_silently(alerts, llm_calls, monkeypatch):
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)
    await work_group.upsert_order_text(chat_id=CHAT, message_id=3, text="Кто едет Сочи-Анапа сегодня, 7000, пишите")
    await settle()
    assert len(llm_calls) == 1 and alerts == []


async def test_other_group_is_not_templated(alerts, llm_calls, monkeypatch):
    monkeypatch.setattr(work_group.settings, "template_chat_ids", str(CHAT))
    await work_group.upsert_order_text(chat_id=-100777, message_id=4, text=SAMPLE)
    assert len(llm_calls) == 1  # шаблон только для группы бота


async def test_template_can_be_switched_off(alerts, llm_calls, monkeypatch):
    monkeypatch.setattr(work_group.settings, "template_parse_enabled", False)
    await work_group.upsert_order_text(chat_id=CHAT, message_id=5, text=SAMPLE)
    assert len(llm_calls) == 1


async def test_alerts_are_throttled_and_deduplicated(alerts, llm_calls):
    def bad(n: int) -> str:
        return f"✅ Заказ · сегодня\n📍 Сочи → Анапа\n💰 цена {n}\n👤 Диспетчер: @IURII1606"

    for n in range(1, 5):
        await work_group.upsert_order_text(chat_id=CHAT, message_id=10 + n, text=bad(n))
    # тот же текст ещё раз (повтор из очереди) — заново не шлём
    await work_group.upsert_order_text(chat_id=CHAT, message_id=10, text=bad(1), is_edit=True)
    await settle()
    assert len(alerts) == 2  # лимит 2 в час
    template_alerts._sent_at.clear()  # прошёл час
    await work_group.upsert_order_text(chat_id=CHAT, message_id=20, text=bad(9))
    await settle()
    assert len(alerts) == 3
    assert "ещё 2" in alerts[2]  # пропущенные склеились в счётчик
