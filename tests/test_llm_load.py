"""Меры, снижающие нагрузку на LLM: нормализованный кэш, склейка одинаковых запросов,
пропуск протухших сообщений и правок без изменений."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Order, ParseOutcome, ParseStat
from app.parsing import llm_parser
from app.parsing.llm_parser import ParseResult, cache_text, parse_orders
from app.parsing.schema import ParsedOrder
from app.parsing.stale import has_date_hint, is_stale
from app.telegram import work_group
from app.timeutil import now_utc_naive

# --- Нормализация текста для кэша ----------------------------------------------------------


def test_cache_text_ignores_case_emoji_and_spaces():
    a = "🚖 Курск → Тольятти   30000 ₽"
    b = "курск тольятти 30000"
    assert cache_text(a) == cache_text(b)


def test_cache_text_keeps_digits_so_different_prices_differ():
    assert cache_text("Курск Тольятти 7.5к") != cache_text("Курск Тольятти 75к")
    assert cache_text("Курск Тольятти 30000") != cache_text("Курск Тольятти 3000")


# --- Кэш и склейка одинаковых запросов -----------------------------------------------------


def _response(orders):
    import json

    call = SimpleNamespace(function=SimpleNamespace(name="record_orders", arguments=json.dumps({"orders": orders})))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    llm_parser.clear_parse_cache()
    llm_parser._inflight.clear()
    yield
    llm_parser.clear_parse_cache()
    llm_parser._inflight.clear()


async def test_decorated_copy_of_the_same_order_hits_the_cache(monkeypatch):
    calls = []

    async def fake(text, today, fallback=False):
        calls.append(text)
        return _response([{"from_city": "Курск", "to_city": "Тольятти", "client_price": 30000}])

    monkeypatch.setattr(llm_parser, "_call_llm", fake)

    first = await parse_orders("Курск Тольятти 30000 водителю")
    second = await parse_orders("🚖 КУРСК   Тольятти 30000 водителю 🚖")

    assert len(calls) == 1
    assert first.from_cache is False and second.from_cache is True
    assert second.orders[0].client_price == 30000


async def test_concurrent_identical_messages_make_one_llm_call(monkeypatch):
    calls = []

    async def slow(text, today, fallback=False):
        calls.append(text)
        await asyncio.sleep(0.05)
        return _response([{"from_city": "Курск", "to_city": "Тольятти", "client_price": 30000}])

    monkeypatch.setattr(llm_parser, "_call_llm", slow)

    results = await asyncio.gather(*(parse_orders("Курск Тольятти 30000 одновременно") for _ in range(8)))

    assert len(calls) == 1  # восемь групп прислали одно и то же: LLM вызвана один раз
    assert all(r.orders[0].client_price == 30000 for r in results)
    assert sum(1 for r in results if not r.from_cache) == 1
    assert llm_parser._inflight == {}


async def test_failure_of_the_leader_reaches_waiters_and_is_not_cached(monkeypatch):
    from app.parsing.llm_parser import ParseUnavailable

    state = {"fail": True, "calls": 0}

    async def flaky(text, today, fallback=False):
        state["calls"] += 1
        await asyncio.sleep(0.02)
        if state["fail"]:
            raise ValueError("bad key")  # нетранзиентная для _is_transient? ValueError — транзиентная
        return _response([{"from_city": "А", "to_city": "Б", "client_price": 5000}])

    monkeypatch.setattr(llm_parser, "_call_llm", flaky)
    monkeypatch.setattr(settings, "llm_max_retries", 1)
    monkeypatch.setattr(llm_parser, "_fallback_client", None)

    outcomes = await asyncio.gather(*(parse_orders("А Б 5000 сбой") for _ in range(3)), return_exceptions=True)
    assert all(isinstance(o, ParseUnavailable) for o in outcomes)
    assert llm_parser._inflight == {}

    state["fail"] = False  # после сбоя следующая попытка идёт заново, а не из «отравленного» кэша
    ok = await parse_orders("А Б 5000 сбой")
    assert ok.orders[0].client_price == 5000


# --- Протухшие сообщения -------------------------------------------------------------------

NOW = datetime(2026, 10, 10, 12, 0)


def test_date_hints():
    assert has_date_hint("Завтра в 10:00 Курск Тольятти")
    assert has_date_hint("12.10 в 9:00 Сочи Адлер")
    assert has_date_hint("15 октября Москва Тула")
    assert has_date_hint("в пятницу в 8 утра")
    assert not has_date_hint("Сейчас Курск Тольятти 30000")
    assert not has_date_hint("Курск Тольятти 18:30 3000")


def test_old_message_without_date_is_stale():
    assert is_stale("Сейчас Курск Тольятти 30000", NOW - timedelta(hours=20), NOW)


def test_old_message_with_date_is_kept_until_hard_limit():
    assert not is_stale("Завтра в 10:00 Курск Тольятти 30000", NOW - timedelta(hours=20), NOW)
    assert is_stale("Завтра в 10:00 Курск Тольятти 30000", NOW - timedelta(hours=80), NOW)


def test_fresh_message_is_never_stale():
    assert not is_stale("Сейчас Курск Тольятти 30000", NOW - timedelta(minutes=30), NOW)
    assert not is_stale("Сейчас Курск Тольятти 30000", None, NOW)


# --- Пропуск в обработчике -----------------------------------------------------------------


async def test_stale_queued_message_skips_llm(session, monkeypatch):
    called = []

    async def fake_parse(text):
        called.append(text)
        return ParseResult(orders=[ParsedOrder(from_city="Курск", to_city="Тольятти", client_price=30000)])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse)
    monkeypatch.setattr(settings, "parse_stats_enabled", True)

    ids = await work_group.upsert_order_text(
        chat_id=-100900, message_id=1, text="Сейчас Курск Тольятти 30000",
        sent_at=now_utc_naive() - timedelta(hours=30),
    )

    assert ids == [] and called == []
    stats = (await session.execute(select(ParseStat))).scalars().all()
    assert [(s.outcome, s.error) for s in stats] == [(ParseOutcome.PREFILTERED, "stale")]


async def test_fresh_queued_message_is_parsed_normally(session, monkeypatch):
    async def fake_parse(text):
        return ParseResult(orders=[ParsedOrder(from_city="Курск", to_city="Тольятти", client_price=30000)])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse)

    ids = await work_group.upsert_order_text(
        chat_id=-100900, message_id=2, text="Сейчас Курск Тольятти 30000",
        sent_at=now_utc_naive() - timedelta(minutes=10),
    )
    assert len(ids) == 1


# --- Точные копии заявки отсеиваются без LLM -----------------------------------------------


async def _twin_setup(session, make_order, **order_fields):
    from app.parsing.llm_parser import text_key

    order = await make_order(from_city="Курск", to_city="Тольятти", price="30000", raw_text="x", **order_fields)
    order.text_key = text_key("Курск Тольятти 30000 водителю")
    await session.commit()
    return order


def _forbid_llm(monkeypatch):
    called = []

    async def fake_parse(text):
        called.append(text)
        return ParseResult(orders=[ParsedOrder(from_city="Курск", to_city="Тольятти", client_price=30000)])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse)
    return called


async def test_exact_copy_of_a_live_order_skips_llm(session, make_order, monkeypatch):
    original = await _twin_setup(session, make_order)
    called = _forbid_llm(monkeypatch)

    ids = await work_group.upsert_order_text(
        chat_id=-100950, message_id=10, text="🚖 курск  Тольятти 30000 водителю",
    )

    assert ids == [original.id] and called == []
    assert len((await session.execute(select(Order))).scalars().all()) == 1


async def test_copy_of_a_cancelled_order_goes_to_llm(session, make_order, monkeypatch):
    from app.models import OrderStatus

    original = await _twin_setup(session, make_order, status=OrderStatus.CANCELLED)
    called = _forbid_llm(monkeypatch)

    await work_group.upsert_order_text(chat_id=-100950, message_id=11, text="Курск Тольятти 30000 водителю")

    assert called  # заказ снят, свежая публикация — новая возможность
    assert original.id


async def test_copy_posted_on_another_day_goes_to_llm(session, make_order, monkeypatch):
    original = await _twin_setup(session, make_order)
    original.created_at = now_utc_naive() - timedelta(hours=30)
    await session.commit()
    called = _forbid_llm(monkeypatch)

    await work_group.upsert_order_text(chat_id=-100950, message_id=12, text="Курск Тольятти 30000 водителю")

    assert called


async def test_different_price_is_not_a_copy(session, make_order, monkeypatch):
    await _twin_setup(session, make_order)
    called = _forbid_llm(monkeypatch)

    await work_group.upsert_order_text(chat_id=-100950, message_id=13, text="Курск Тольятти 35000 водителю")

    assert called


async def test_edit_is_never_treated_as_a_copy(session, make_order, monkeypatch):
    await _twin_setup(session, make_order)
    called = _forbid_llm(monkeypatch)

    await work_group.upsert_order_text(
        chat_id=-100950, message_id=14, text="Курск Тольятти 30000 водителю", is_edit=True,
    )

    assert called


async def test_saved_order_gets_its_text_key(session, monkeypatch):
    from app.parsing.llm_parser import text_key

    _forbid_llm(monkeypatch)
    ids = await work_group.upsert_order_text(chat_id=-100951, message_id=1, text="Курск Тольятти 30000 водителю")

    order = (await session.execute(select(Order).where(Order.id == ids[0]))).scalar_one()
    assert order.text_key == text_key("Курск Тольятти 30000 водителю")
