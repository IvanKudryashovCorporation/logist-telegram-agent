"""Ограничение частоты запросов (rate limiting).

Зачем: сайт публичный, без регистрации и показывает телефоны клиентов вместе
с @username диспетчеров. Без ограничения частоты его можно (а) выкачать
целиком скриптом за минуты и (б) зафлудить POST-запросами «взять заказ»,
перебрав все id. Ни того ни другого в логах не заметишь, пока не станет поздно.

Реализация — скользящее окно в памяти процесса. Для одного uvicorn-воркера
этого достаточно; если воркеров станет больше, limiter нужно выносить в Redis
(интерфейс :class:`SlidingWindowLimiter` для этого подходит как есть).

Память ограничена: устаревшие ключи вычищаются, а при превышении числа
отслеживаемых адресов сбрасываются самые старые — иначе злоумышленник с
ботнетом мог бы раздуть словарь до OOM.
"""

import logging
import time
from collections import OrderedDict, deque
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.config import settings
from app.web.deps import client_ip

log = logging.getLogger("web.rate_limit")

#: Пути, которые не лимитируем: статика, мониторинг, favicon.
EXEMPT_PREFIXES = ("/static/", "/healthz", "/robots.txt", "/favicon.ico")

#: Сколько адресов храним одновременно, прежде чем начать вытеснять старые.
MAX_TRACKED_KEYS = 20_000

_WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    """Скользящее окно: не больше ``limit`` обращений за ``window`` секунд."""

    def __init__(
        self,
        limit: int,
        window: float = _WINDOW_SECONDS,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window)
        self.max_keys = max(16, int(max_keys))
        self._hits: "OrderedDict[str, deque]" = OrderedDict()
        self.rejected_total = 0

    def allow(self, key: str, *, now: Optional[float] = None) -> tuple[bool, int]:
        """Пропускаем ли запрос. Второе значение — сколько секунд подождать."""
        moment = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits

        cutoff = moment - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = max(1, int(self.window - (moment - hits[0])) + 1)
            self.rejected_total += 1
            self._evict_if_full(moment)
            return False, retry_after

        hits.append(moment)
        self._hits.move_to_end(key)
        self._evict_if_full(moment)
        return True, 0

    def _evict_if_full(self, now: float) -> None:
        if len(self._hits) <= self.max_keys:
            return
        cutoff = now - self.window
        while len(self._hits) > self.max_keys:
            key, hits = next(iter(self._hits.items()))
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if hits:
                # Все ключи «свежие» — вытесняем самый старый по использованию.
                self._hits.move_to_end(key)
                self._hits.popitem(last=False)
            else:
                self._hits.popitem(last=False)

    def reset(self) -> None:
        self._hits.clear()
        self.rejected_total = 0

    def tracked_keys(self) -> int:
        return len(self._hits)


#: Отдельные лимиты для чтения и для действий: открыть 120 страниц в минуту —
#: нормальное поведение человека, а 120 POST «взять заказ» — уже скрипт.
_get_limiter = SlidingWindowLimiter(settings.rate_limit_get_per_minute)
_post_limiter = SlidingWindowLimiter(settings.rate_limit_post_per_minute)


def _is_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Возвращает 429 + Retry-After, если клиент превысил лимит."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.rate_limit_enabled or _is_exempt(request.url.path):
            return await call_next(request)

        is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        limiter = _post_limiter if is_write else _get_limiter
        key = f"{client_ip(request)}:{request.url.path if is_write else 'read'}"

        allowed, retry_after = limiter.allow(key)
        if not allowed:
            log.warning(
                "Rate limit: %s %s от %s (retry after %ss)",
                request.method, request.url.path, key, retry_after,
            )
            return PlainTextResponse(
                "Слишком много запросов. Подождите минуту и попробуйте снова.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


def limiter_stats() -> dict:
    """Для /admin: видно, не слишком ли жёстко настроены лимиты."""
    return {
        "enabled": settings.rate_limit_enabled,
        "get_limit_per_min": _get_limiter.limit,
        "post_limit_per_min": _post_limiter.limit,
        "tracked_clients": _get_limiter.tracked_keys() + _post_limiter.tracked_keys(),
        "rejected_total": _get_limiter.rejected_total + _post_limiter.rejected_total,
    }


def reset_limiters() -> None:
    """Сброс состояния (тесты, рестарт без перезапуска процесса)."""
    _get_limiter.reset()
    _post_limiter.reset()
