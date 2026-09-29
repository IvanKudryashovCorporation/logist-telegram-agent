"""Ограничение частоты запросов и определение реального IP клиента.

Сайт публичный, без регистрации, и показывает телефоны клиентов вместе с
@username диспетчеров. Без лимита его можно выкачать скриптом за минуты или
зафлудить POST «взять заказ», перебрав все id. Отдельно проверяется, что
заголовки прокси используются ТОЛЬКО когда прокси доверенный: иначе любой
посетитель подставляет свой X-Forwarded-For и обходит лимит.
"""

import pytest
from starlette.requests import Request

from app.config import settings
from app.web import rate_limit
from app.web.deps import client_ip
from app.web.rate_limit import EXEMPT_PREFIXES, SlidingWindowLimiter, _is_exempt


def _request(headers: dict | None = None, client=("93.120.45.10", 5555)) -> Request:
    """Миниатюра ASGI-scope, достаточная для client_ip()."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
            "client": client,
            "server": ("testserver", 80),
        }
    )


# --- Скользящее окно ---------------------------------------------------------


def test_allows_up_to_the_limit():
    limiter = SlidingWindowLimiter(limit=3, window=60)

    results = [limiter.allow("ip", now=float(i)) for i in range(3)]

    assert [allowed for allowed, _ in results] == [True, True, True]


def test_rejects_over_the_limit_with_retry_after():
    limiter = SlidingWindowLimiter(limit=2, window=60)
    limiter.allow("ip", now=0.0)
    limiter.allow("ip", now=1.0)

    allowed, retry_after = limiter.allow("ip", now=2.0)

    assert allowed is False
    assert retry_after >= 1
    assert limiter.rejected_total == 1


def test_window_slides_and_frees_quota():
    limiter = SlidingWindowLimiter(limit=2, window=60)
    limiter.allow("ip", now=0.0)
    limiter.allow("ip", now=10.0)
    # Оба обращения ещё в окне — квота исчерпана.
    assert limiter.allow("ip", now=20.0)[0] is False
    # t=0 выпало из окна (20+41=61 > 0+60), t=10 ещё нет — один слот свободен.
    assert limiter.allow("ip", now=61.0)[0] is True
    # Теперь в окне t=10 и t=61 — снова отказ.
    assert limiter.allow("ip", now=62.0)[0] is False
    # К t=71 из окна выпало t=10 — слот освободился.
    assert limiter.allow("ip", now=71.0)[0] is True


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window=60)

    assert limiter.allow("first", now=0.0)[0] is True
    assert limiter.allow("second", now=0.0)[0] is True
    assert limiter.allow("first", now=1.0)[0] is False


def test_limit_is_at_least_one():
    """Нулевой/отрицательный лимит заблокировал бы сайт целиком."""
    assert SlidingWindowLimiter(limit=0).limit == 1
    assert SlidingWindowLimiter(limit=-5).limit == 1


def test_memory_is_bounded_by_max_keys():
    """Ботнет не должен раздувать словарь лимитера до OOM."""
    limiter = SlidingWindowLimiter(limit=5, window=60, max_keys=32)

    for index in range(500):
        limiter.allow(f"10.0.{index // 250}.{index % 250}", now=float(index))

    assert limiter.tracked_keys() <= 32


def test_reset_clears_state():
    limiter = SlidingWindowLimiter(limit=1, window=60)
    limiter.allow("ip", now=0.0)
    assert limiter.allow("ip", now=1.0)[0] is False

    limiter.reset()

    assert limiter.allow("ip", now=2.0)[0] is True
    assert limiter.rejected_total == 0


# --- Исключения --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/static/style.css", "/static/app.js", "/healthz", "/robots.txt", "/favicon.ico"],
)
def test_service_paths_are_exempt(path):
    assert _is_exempt(path) is True


@pytest.mark.parametrize("path", ["/", "/orders/1", "/my", "/admin", "/staticfile"])
def test_normal_paths_are_not_exempt(path):
    assert _is_exempt(path) is False


def test_exempt_prefixes_cover_service_routes():
    assert "/static/" in EXEMPT_PREFIXES
    assert "/healthz" in EXEMPT_PREFIXES
    assert "/robots.txt" in EXEMPT_PREFIXES


# --- Определение IP ----------------------------------------------------------


def test_uses_socket_address_by_default():
    # TRUST_PROXY_HEADERS=false в тестах: заголовкам из запроса верить нельзя.
    assert client_ip(_request({"X-Forwarded-For": "1.1.1.1"})) == "93.120.45.10"


def test_spoofed_forwarded_for_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)

    assert client_ip(_request({"X-Forwarded-For": "8.8.8.8"})) == "93.120.45.10"


def test_trusted_proxy_first_hop_is_used(monkeypatch):
    """За nginx в X-Forwarded-For список: реальный клиент — первый элемент."""
    monkeypatch.setattr(settings, "trust_proxy_headers", True)

    request = _request({"X-Forwarded-For": "203.0.113.7, 10.0.0.1, 10.0.0.2"})

    assert client_ip(request) == "203.0.113.7"


def test_trusted_proxy_falls_back_to_real_ip(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)

    assert client_ip(_request({"X-Real-IP": "203.0.113.9"})) == "203.0.113.9"


def test_missing_client_does_not_crash():
    assert client_ip(_request(client=None)) == "unknown"


# --- Middleware --------------------------------------------------------------


async def test_middleware_returns_429_when_limit_exceeded(client, make_order, monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit._get_limiter, "limit", 3)
    await make_order()

    codes = [(await client.get("/")).status_code for _ in range(5)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


async def test_rate_limited_response_has_retry_after(client, monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit._get_limiter, "limit", 1)

    await client.get("/")
    blocked = await client.get("/")

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert "Слишком много запросов" in blocked.text


async def test_exempt_paths_survive_flood(client, monkeypatch):
    """Мониторинг и статика не должны отключаться вместе с сайтом."""
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit._get_limiter, "limit", 1)

    for _ in range(5):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/robots.txt")).status_code == 200


async def test_post_requests_have_their_own_bucket(client, make_order, monkeypatch):
    """Действия водителя лимитируются строже, чем чтение ленты."""
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(rate_limit._get_limiter, "limit", 100)
    monkeypatch.setattr(rate_limit._post_limiter, "limit", 2)
    order = await make_order()

    codes = [(await client.post(f"/orders/{order.id}/take")).status_code for _ in range(4)]

    assert codes[:2] == [303, 303]
    assert codes[2:] == [429, 429]


async def test_disabling_rate_limit_lets_everything_through(client, make_order, monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", False)
    monkeypatch.setattr(rate_limit._get_limiter, "limit", 1)
    await make_order()

    codes = [(await client.get("/")).status_code for _ in range(5)]

    assert codes == [200] * 5


def test_limiter_stats_reports_configuration(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", True)

    stats = rate_limit.limiter_stats()

    assert stats["enabled"] is True
    assert stats["get_limit_per_min"] >= 1
    assert stats["post_limit_per_min"] >= 1
    assert stats["tracked_clients"] >= 0
    assert stats["rejected_total"] >= 0
