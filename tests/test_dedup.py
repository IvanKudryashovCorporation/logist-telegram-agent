"""Дедупликация: защита от повторной обработки событий и от дублей в БД.

Уровней два, и оба важны:

* in-memory LRU (:mod:`app.telegram.dedup`) — экономит вызовы LLM, когда
  Telethon переигрывает уже обработанные события после реконнекта;
* ``uq_orders_source`` в БД — железная гарантия: память процесса рестарт
  агента не переживает, а дубль заявки в ленте водитель видит как обман.
"""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import Order, OrderStatus
from app.parsing.llm_parser import ParseResult
from app.parsing.schema import ParsedOrder
from app.telegram import dedup, work_group
from app.telegram.work_group import _dedup_variant

CHAT_ID = -1001234567890


class FakeMessage:
    """Миниатюра Telethon-сообщения: для _dedup_variant нужен только edit_date."""

    def __init__(self, edit_date=None) -> None:
        self.edit_date = edit_date


# --- In-memory LRU -----------------------------------------------------------


def test_first_event_is_processed_once():
    assert dedup.mark_processed(CHAT_ID, 101) is True
    assert dedup.mark_processed(CHAT_ID, 101) is False
    assert dedup.is_processed(CHAT_ID, 101) is True


def test_unmark_allows_reprocessing():
    """Если обработка упала до коммита, событие нельзя терять."""
    dedup.mark_processed(CHAT_ID, 102)
    dedup.unmark_processed(CHAT_ID, 102)

    assert dedup.is_processed(CHAT_ID, 102) is False
    assert dedup.mark_processed(CHAT_ID, 102) is True


def test_unmark_of_unknown_event_is_safe():
    dedup.unmark_processed(CHAT_ID, 999999)  # не должно бросать исключение


def test_different_chats_are_independent():
    assert dedup.mark_processed(-1, 500) is True
    assert dedup.mark_processed(-2, 500) is True


def test_edit_of_processed_message_is_not_dropped():
    """Исправленный баг: ключ только из (chat, msg) отбрасывал ЛЮБУЮ правку.

    Диспетчер добавляет в сообщение цену или исправляет город — без разделения
    вариантов в ленте оставалась старая версия заявки.
    """
    assert dedup.mark_processed(CHAT_ID, 200, "new") is True
    assert dedup.mark_processed(CHAT_ID, 200, "edit:1700000000") is True
    assert dedup.mark_processed(CHAT_ID, 200, "edit:1700000000") is False
    assert dedup.mark_processed(CHAT_ID, 200, "edit:1700000600") is True


def test_overflow_evicts_oldest_not_everything():
    """Раньше при переполнении множество очищалось ЦЕЛИКОМ.

    Сразу после такой очистки повторно доставленное старое сообщение
    обрабатывалось заново — дубли заявок появлялись именно так.
    """
    limit = dedup.PROCESSED_MESSAGES_LIMIT
    dedup.mark_processed(CHAT_ID, 1)

    for index in range(limit + 10):
        dedup.mark_processed(CHAT_ID, 1000 + index)

    assert dedup.processed_count() == limit
    assert dedup.is_processed(CHAT_ID, 1) is False, "самое старое событие вытеснено"
    assert dedup.is_processed(CHAT_ID, 1000 + limit + 9) is True, "последние события на месте"
    # И главное: окно защиты не обнулилось — свежие ключи по-прежнему отсекаются.
    assert dedup.mark_processed(CHAT_ID, 1000 + limit + 9) is False


def test_recent_events_are_refreshed_on_repeat():
    """Повторная доставка обновляет «свежесть» ключа, а не вытесняет его."""
    dedup.mark_processed(CHAT_ID, 300)
    limit = dedup.PROCESSED_MESSAGES_LIMIT

    for _ in range(3):
        for index in range(limit // 2):
            dedup.mark_processed(CHAT_ID, 5000 + index)
        dedup.mark_processed(CHAT_ID, 300)

    assert dedup.is_processed(CHAT_ID, 300) is True


# --- Ключ варианта события ---------------------------------------------------


def test_variant_for_new_message():
    assert _dedup_variant(FakeMessage(), is_edit=False) == "new"


def test_variant_differs_per_edit():
    first = FakeMessage(edit_date=datetime(2026, 5, 1, 10, 0))
    second = FakeMessage(edit_date=datetime(2026, 5, 1, 12, 30))

    assert _dedup_variant(first, is_edit=True) != _dedup_variant(second, is_edit=True)


def test_variant_is_stable_for_same_edit():
    moment = datetime(2026, 5, 1, 10, 0)

    assert _dedup_variant(FakeMessage(moment), True) == _dedup_variant(FakeMessage(moment), True)


def test_variant_without_edit_date_does_not_crash():
    """Telethon может отдать правку без edit_date — обработчик не должен падать."""
    assert _dedup_variant(FakeMessage(None), is_edit=True) == "edit:0"


# --- Гарантия на уровне БД ---------------------------------------------------


def _order(chat_id: int, message_id: int, sub_index: int = 0, text: str = "Симферополь — Сочи 14000"):
    return Order(
        source_chat_id=chat_id,
        source_message_id=message_id,
        source_sub_index=sub_index,
        raw_text=text,
        status=OrderStatus.NEW,
    )


async def test_unique_source_constraint_blocks_duplicates(session):
    """Даже если память подвела, БД не пропустит две копии одной заявки."""
    session.add(_order(CHAT_ID, 777))
    await session.commit()

    session.add(_order(CHAT_ID, 777, text="то же сообщение, доставлено повторно"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    total = (await session.execute(select(func.count(Order.id)))).scalar_one()
    assert total == 1


async def test_several_orders_in_one_message_are_allowed(session):
    """Диспетчер кидает список заявок одним сообщением — их различает sub_index."""
    for index in (0, 1, 2):
        session.add(_order(CHAT_ID, 888, sub_index=index, text=f"заявка {index}"))
    await session.commit()

    total = (
        await session.execute(
            select(func.count(Order.id)).where(Order.source_message_id == 888)
        )
    ).scalar_one()
    assert total == 3


async def test_same_message_id_in_different_chats_is_allowed(session):
    session.add(_order(-1, 10))
    session.add(_order(-2, 10))

    await session.commit()

    total = (await session.execute(select(func.count(Order.id)))).scalar_one()
    assert total == 2


# --- Гонка между конкурентными сообщениями из разных групп -------------------


async def test_concurrent_messages_same_route_price_time_are_deduped(session, monkeypatch):
    """Три сообщения с одинаковым маршрутом/ценой/временем, пришедшие из трёх
    разных групп практически одновременно, не должны создать три заказа.

    Реальный кейс с прода: диспетчеры репостнули один и тот же рейс
    (Рубановка -> Стерлитамак, 90000₽, 30.09 06:00) в три группы почти
    вплотную по времени. Telethon разбирает события НЕЗАВИСИМЫМИ задачами —
    без сериализации все три вызова успевали пройти проверку "дубликатов
    нет" ДО того, как первый из них коммитил INSERT, и получались три
    карточки одного и того же заказа. Тест намеренно вставляет задержку в
    разбор, чтобы гарантированно открыть окно гонки, и проверяет, что
    _dedup_lock в work_group.py его закрывает.
    """

    async def fake_parse_orders(text: str) -> ParseResult:
        await asyncio.sleep(0.05)  # имитация сетевой задержки LLM
        return ParseResult(
            orders=[
                ParsedOrder(
                    pickup_date="2026-09-30",
                    pickup_time="06:00",
                    from_city="Рубановка",
                    to_city="Стерлитамак",
                    passengers=4,
                    client_price=90000,
                )
            ]
        )

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)

    text = "Рубановка -> Стерлитамак 90000, 4 чел, минивэн, 30.09 06:00"
    results = await asyncio.gather(
        work_group.upsert_order_text(chat_id=-100111, message_id=1, text=text),
        work_group.upsert_order_text(chat_id=-100222, message_id=1, text=text),
        work_group.upsert_order_text(chat_id=-100333, message_id=1, text=text),
    )

    total = (await session.execute(select(func.count(Order.id)))).scalar_one()
    assert total == 1, f"ожидали один заказ, получили {total} — дубль проскочил через гонку"

    # Все три вызова обязаны вернуть id одного и того же заказа (первого
    # созданного), а не создавать каждый свой.
    order_id = results[0][0]
    assert all(ids == [order_id] for ids in results)
