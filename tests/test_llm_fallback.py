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
