"""Группа «PRO все заказы»: служебные строки бота вырезаются, сообщения идут с высоким приоритетом."""

import asyncio
from datetime import timedelta

from app.config import settings
from app.db.base import SessionLocal
from app.parsing import llm_parser
from app.parsing.bot_noise import strip_bot_noise
from app.telegram import pending as queue
from app.timeutil import now_utc_naive

BOT_MESSAGE = """✅ Заказ  ·  18.10 в 01:30
📍 Сальск → Ростов-на-Дону
💰 5 000 ₽
🕒 Опубликован: только что

Сообщение диспетчеру уже вписано, проверьте и отправьте: Здравствуйте! Возьму заказ 18.10 01:30 Сальск–Ростов-на-Дону 5 000 ₽
👤 Диспетчер: @annakoropenko
ℹ️ Группа закрытая: «Источник» откроется, если вы в ней состоите."""


def test_bot_noise_removed_but_contact_kept():
    cleaned = strip_bot_noise(BOT_MESSAGE)
    assert "Возьму заказ" not in cleaned
    assert "Опубликован" not in cleaned
    assert "Источник" not in cleaned
    assert "📍 Сальск → Ростов-на-Дону" in cleaned
    assert "💰 5 000 ₽" in cleaned
    assert "👤 Диспетчер: @annakoropenko" in cleaned


def test_publish_time_does_not_change_text():
    older = BOT_MESSAGE.replace("только что", "16 минут назад")
    assert strip_bot_noise(older) == strip_bot_noise(BOT_MESSAGE)


def test_ordinary_text_untouched():
    text = "Керчь → Краснодар, завтра в 10:00, 8000"
    assert strip_bot_noise(text) == text


def test_priority_chats_setting(monkeypatch):
    monkeypatch.setattr(settings, "priority_chat_ids", "-1004413141153; 42, x")
    assert settings.priority_chats == {-1004413141153, 42}


def test_priority_gate_serves_priority_first():
    async def scenario() -> list[str]:
        gate = llm_parser._PriorityGate(1)
        order: list[str] = []

        async def use(name: str, priority: bool, hold: float = 0.0) -> None:
            async with gate.slot(priority):
                order.append(name)
                await asyncio.sleep(hold)

        first = asyncio.create_task(use("first", False, 0.05))
        await asyncio.sleep(0.01)
        rest = [
            asyncio.create_task(use("plain-1", False)),
            asyncio.create_task(use("plain-2", False)),
            asyncio.create_task(use("vip", True)),
        ]
        await asyncio.gather(first, *rest)
        return order

    assert asyncio.run(scenario()) == ["first", "vip", "plain-1", "plain-2"]


async def test_queue_takes_priority_chat_first(monkeypatch):
    monkeypatch.setattr(settings, "priority_chat_ids", "-1004413141153")
    async with SessionLocal() as session:
        for chat_id, message_id in ((-1001111111111, 1), (-1004413141153, 2)):
            item = await queue.enqueue(chat_id=chat_id, message_id=message_id, text="заявка на перевозку", session=session)
            item.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
            session.add(item)
        await session.commit()
        due = await queue.fetch_due(session, limit=10)
    assert [item.chat_id for item in due][0] == -1004413141153


def test_bot_taken_message_is_closed_mark():
    from app.parsing.closed import has_closed_lock

    assert has_closed_lock("⛔️ Заказ снят/взят\n⚡️ СРОЧНО Ровеньки → Старый Оскол, 8 000 ₽")
    assert has_closed_lock("⛔️ Заказ снят / взят\n18.10 01:30 Сальск → Ростов-на-Дону, 5 000 ₽")
    assert not has_closed_lock("✅ Заказ  ·  18.10 в 01:30\n📍 Сальск → Ростов-на-Дону\n💰 5 000 ₽")


def test_dispatcher_line_gives_contact():
    from app.parsing.order_builder import extract_contact_username

    text = "📍 Алупка → Симферополь\n👤 Диспетчер: @Mangotaxi24"
    assert extract_contact_username(text) == "Mangotaxi24"


def test_passengers_off_by_one_are_same_client():
    from app.dedupe_rules import LiveOrder, clearly_different_clients

    def live(passengers):
        return LiveOrder(None, None, None, None, None, None, None, passengers, None, None)

    assert not clearly_different_clients(live(5), live(6))
    assert clearly_different_clients(live(2), live(4))


async def test_bot_taken_edit_without_order_is_not_spam(monkeypatch):
    monkeypatch.setattr(settings, "parse_stats_enabled", True)
    from sqlalchemy import select

    from app.models import ParseStat
    from app.telegram.work_group import upsert_order_text

    text = "⛔️ Заказ снят/взят\n18.10 01:30 Сальск → Ростов-на-Дону, 5 000 ₽"
    assert await upsert_order_text(chat_id=-1004413141153, message_id=9001, text=text, is_edit=True) == []
    async with SessionLocal() as session:
        stat = (await session.execute(select(ParseStat).where(ParseStat.message_id == 9001))).scalars().one()
    assert stat.error == "closed_lock"
