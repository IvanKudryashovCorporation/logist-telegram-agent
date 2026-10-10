"""Резервный LLM: при отказе основного (401/лимит/сбой) разбор идёт через запасной провайдер."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from app.config import settings
from app.parsing import llm_parser
from app.parsing.llm_parser import ParseUnavailable, parse_orders

_ORDER = {"from_city": "Курск", "to_city": "Тольятти", "client_price": 30000}


def _response(orders):
    call = SimpleNamespace(function=SimpleNamespace(name="record_orders", arguments=json.dumps({"orders": orders})))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _status_error(cls, status):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    llm_parser.clear_parse_cache()
    monkeypatch.setattr(settings, "llm_max_retries", 1)
    monkeypatch.setattr(settings, "llm_fallback_model", "backup/model:free")
    yield
    llm_parser.clear_parse_cache()


def _patch_calls(monkeypatch, primary, fallback):
    seen = []

    async def fake(text, today, fallback_flag=False):
        seen.append("fallback" if fallback_flag else "primary")
        outcome = fallback if fallback_flag else primary
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(llm_parser, "_call_llm", fake)
    return seen


async def test_auth_error_switches_to_fallback(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    seen = _patch_calls(monkeypatch, _status_error(AuthenticationError, 401), _response([_ORDER]))

    result = await parse_orders("Курск Тольятти 30000 водителю")

    assert seen == ["primary", "fallback"]
    assert result.model == "backup/model:free"
    assert result.orders[0].client_price == 30000


async def test_rate_limit_switches_to_fallback(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    seen = _patch_calls(monkeypatch, _status_error(RateLimitError, 429), _response([_ORDER]))

    result = await parse_orders("Курск Тольятти 30000")

    assert seen == ["primary", "fallback"]
    assert result.orders


async def test_primary_ok_does_not_touch_fallback(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    seen = _patch_calls(monkeypatch, _response([_ORDER]), _status_error(AuthenticationError, 401))

    result = await parse_orders("Курск Тольятти 30000 основная")

    assert seen == ["primary"]
    assert result.model == settings.llm_model


async def test_without_fallback_the_error_is_raised(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", None)
    seen = _patch_calls(monkeypatch, _status_error(AuthenticationError, 401), _response([_ORDER]))

    with pytest.raises(ParseUnavailable):
        await parse_orders("Курск Тольятти 30000 без резерва")
    assert seen == ["primary"]


async def test_both_providers_down_reports_both(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    _patch_calls(monkeypatch, _status_error(AuthenticationError, 401), _status_error(AuthenticationError, 401))

    with pytest.raises(ParseUnavailable) as info:
        await parse_orders("Курск Тольятти 30000 оба лежат")
    assert "основной" in str(info.value) and "резервный" in str(info.value)


async def test_fallback_gets_its_own_extra_body_and_not_the_dashscope_one(monkeypatch):
    captured = {}

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _response([_ORDER])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(llm_parser, "_fallback_client", fake_client)
    monkeypatch.setattr(settings, "llm_fallback_extra_body", '{"thinking": {"type": "disabled"}}')

    await llm_parser._call_llm("текст", "2026-10-10", True)

    assert captured["model"] == "backup/model:free"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_proxy_is_passed_only_when_configured():
    plain = llm_parser._http_client("")
    proxied = llm_parser._http_client("http://user:pass@127.0.0.1:3128")
    socks = llm_parser._http_client("socks5://127.0.0.1:1080")

    assert plain._mounts == {}  # без прокси клиент ходит напрямую (и не берёт HTTP_PROXY из окружения)
    assert len(proxied._mounts) > 0
    assert len(socks._mounts) > 0


# --- Основной работает почти всегда, резерв — редко --------------------------------------------


async def test_rate_limit_on_a_queued_message_waits_instead_of_using_the_fallback(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    seen = _patch_calls(monkeypatch, _status_error(RateLimitError, 429), _response([_ORDER]))

    with pytest.raises(ParseUnavailable) as info:
        await parse_orders("Курск Тольятти 30000 из очереди", urgent=False)

    assert seen == ["primary"]  # платный резерв не тронут
    assert info.value.rate_limited is True


async def test_auth_error_on_a_queued_message_still_uses_the_fallback(monkeypatch):
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    seen = _patch_calls(monkeypatch, _status_error(AuthenticationError, 401), _response([_ORDER]))

    result = await parse_orders("Курск Тольятти 30000 из очереди 401", urgent=False)

    assert seen == ["primary", "fallback"] and result.orders  # ключ отвергнут — это поломка, а не «подожди»


def test_retry_after_is_honoured_and_capped(monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_after_cap_seconds", 30.0)
    request = httpx.Request("POST", "https://example.test/v1")

    def limited(retry_after):
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        return RateLimitError("slow", response=httpx.Response(429, request=request, headers=headers), body=None)

    assert 7.0 <= llm_parser._retry_delay(limited("7"), 1) < 7.5
    assert llm_parser._retry_delay(limited("600"), 1) < 30.5  # потолок
    assert llm_parser._retry_delay(limited(None), 1) >= 5.0  # заголовка нет — не секундная пауза
    assert llm_parser._retry_delay(ValueError("x"), 1) < 2  # не лимит — обычная короткая пауза


async def test_primary_requests_run_at_most_concurrency_at_a_time(monkeypatch):
    import asyncio

    monkeypatch.setattr(llm_parser, "_primary_gate", asyncio.Semaphore(2))
    state = {"now": 0, "peak": 0}

    async def slow(text, today, fallback=False):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.03)
        state["now"] -= 1
        return _response([_ORDER])

    monkeypatch.setattr(llm_parser, "_call_llm", slow)

    await asyncio.gather(*(parse_orders(f"Курск Тольятти {30000 + i} параллельно") for i in range(8)))

    assert state["peak"] == 2  # лишние запросы ждут очереди, а не дают лимит 429
