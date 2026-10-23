"""Заказ не должен залёживаться: при сбое основной модели запрос сразу идёт резервной, медленный основной обгоняется."""

import asyncio
from datetime import timedelta

import pytest

from app.models import ParseOutcome
from app.parsing import llm_parser
from app.parsing.llm_parser import ParseResult, ParseUnavailable
from app.parsing.schema import ParsedOrder
from app.timeutil import now_utc_naive


def _result(model, city="Ростов"):
    return ParseResult(orders=[ParsedOrder(from_city=city, to_city="Таганрог", client_price=5000)], model=model)


@pytest.fixture
def two_providers(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    monkeypatch.setattr(llm_parser.settings, "llm_fast_failover", True)
    monkeypatch.setattr(llm_parser.settings, "llm_model", "primary")
    monkeypatch.setattr(llm_parser.settings, "llm_fallback_model", "second")
    monkeypatch.setattr(llm_parser.settings, "llm_hedge_after_seconds", 0.2)


async def test_fast_primary_needs_no_second_call(two_providers, monkeypatch):
    calls = []

    async def fake(text, today, fallback, priority=False, max_attempts=None):
        calls.append(fallback)
        return _result("primary")

    monkeypatch.setattr(llm_parser, "_parse_with", fake)

    result = await llm_parser._race("Ростов Таганрог 5000", "2026-10-22", False)

    assert calls == [False] and result.model == "primary"


async def test_primary_error_goes_to_second_provider_at_once(two_providers, monkeypatch):
    calls = []

    async def fake(text, today, fallback, priority=False, max_attempts=None):
        calls.append((fallback, max_attempts))
        if not fallback:
            raise ParseUnavailable("429", rate_limited=True)
        return _result("second")

    monkeypatch.setattr(llm_parser, "_parse_with", fake)
    started = asyncio.get_running_loop().time()

    result = await llm_parser._race("Ростов Таганрог 5000", "2026-10-22", False)

    assert result.model == "second" and calls[0] == (False, 1)  # основному — одна попытка, без ожидания повторов
    assert asyncio.get_running_loop().time() - started < 0.15


async def test_slow_primary_is_overtaken_by_second_provider(two_providers, monkeypatch):
    async def fake(text, today, fallback, priority=False, max_attempts=None):
        if not fallback:
            await asyncio.sleep(5)  # основной завис
            return _result("primary")
        return _result("second")

    monkeypatch.setattr(llm_parser, "_parse_with", fake)
    started = asyncio.get_running_loop().time()

    result = await llm_parser._race("Ростов Таганрог 5000", "2026-10-22", False)

    assert result.model == "second"
    assert asyncio.get_running_loop().time() - started < 1  # не ждём 5 секунд


async def test_both_providers_down_is_reported_for_the_queue(two_providers, monkeypatch):
    async def fake(text, today, fallback, priority=False, max_attempts=None):
        raise ParseUnavailable("503" if fallback else "429", rate_limited=not fallback)

    monkeypatch.setattr(llm_parser, "_parse_with", fake)

    with pytest.raises(ParseUnavailable) as caught:
        await llm_parser._race("Ростов Таганрог 5000", "2026-10-22", False)

    assert "429" in str(caught.value) and "503" in str(caught.value)


def test_queue_defaults_retry_within_seconds():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["queue_base_delay_seconds"].default <= 10  # первый повтор через секунды, а не через минуту
    assert fields["queue_poll_seconds"].default <= 5
    assert fields["queue_max_attempts"].default >= 8  # частые попытки не должны быстро сдаваться


async def test_order_records_publication_delay(session, monkeypatch):
    from decimal import Decimal

    from sqlalchemy import select

    from app.models import ParseStat
    from app.services import reporting
    from app.telegram import work_group

    async def fake_parse_orders(text, **kwargs):
        return ParseResult(orders=[ParsedOrder(from_city="Гагра", to_city="Сочи", client_price=Decimal("2000"),
                                               pickup_time="12:00")])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)
    monkeypatch.setattr(work_group.settings, "parse_stats_enabled", True)

    await work_group.upsert_order_text(
        chat_id=-100777, message_id=1, text="Гагра Сочи 2000 в 12:00 заявка", dispatcher_tg_id=1,
        dispatcher_username="disp", published_at=now_utc_naive() - timedelta(seconds=3),
    )

    stat = (await session.execute(select(ParseStat).where(ParseStat.outcome == ParseOutcome.ORDER))).scalars().one()
    assert 2500 <= stat.delay_ms <= 8000
    summary = await reporting.delay_summary(hours=1)
    assert summary["count"] == 1 and summary["within_5s_pct"] == 100
