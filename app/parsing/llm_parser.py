"""Разбор свободного текста заявки диспетчера в структуру ParsedOrder через LLM.

Провайдер — любой OpenAI-совместимый chat/completions API (сейчас DashScope,
модель настраивается через LLM_MODEL). Разбор идёт через function-calling
(tool record_orders), а не через свободный текст — так поля приходят готовой
структурой без дополнительного парсинга ответа.

Одно сообщение может содержать несколько заявок сразу — LLM возвращает
МАССИВ, поэтому parse_order_texts всегда отдаёт list[ParsedOrder] (пустой
список — сообщение не заявка).

Надёжность. Раньше один таймаут или 429 от провайдера означали, что заявка
терялась НАВСЕГДА: Telethon не переигрывает уже доставленное событие, а
обработчик просто ронял исключение в лог. Теперь:

* у клиента есть явный таймаут;
* транзиентные ошибки (таймаут, сеть, 429, 5xx) повторяются с экспоненциальной
  задержкой и джиттером;
* фатальные ошибки (401/403/404 — неверный ключ или модель) НЕ повторяются,
  а сразу отдают ParseUnavailable с внятным текстом;
* успешные разборы кэшируются по хэшу текста (диспетчеры копируют заявки
  один в один, а каждый вызов стоит денег);
* вместе с результатом возвращаются расход токенов и латентность — их пишет
  в parse_stats вызывающий код, иначе качество разбора невозможно измерить.

Если все попытки исчерпаны, вызывающий код обязан отложить сообщение в очередь
(app/models/pending_message.py), а не проглотить ошибку.
"""

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.parsing.schema import PARSE_ORDERS_TOOL_OPENAI, ParsedOrder

log = logging.getLogger("agent.parse")

# trust_env=False — иначе httpx подхватывает системные HTTP_PROXY/HTTPS_PROXY
# (в этом окружении прокси подменяет заголовок авторизации на чужой токен).
_client = AsyncOpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url or None,
    timeout=settings.llm_timeout_seconds,
    # max_retries=0 — повторы делаем сами, чтобы считать попытки и логировать их.
    max_retries=0,
    http_client=httpx.AsyncClient(trust_env=False),
)

_SYSTEM_PROMPT = """Ты разбираешь сообщение в рабочем чате диспетчеров такси на
заявки пассажирской перевозки (такси/трансфер). В ОДНОМ сообщении может быть
НЕСКОЛЬКО заявок сразу (списком, через пустую строку и т.п.) — тогда каждая
идёт отдельным элементом массива orders инструмента record_orders, поля
разных поездок нельзя смешивать в одну заявку. Если поле явно не следует из
текста конкретной заявки — оставь пустым, не придумывай.

Верни пустой массив orders, если сообщение НЕ является заявкой диспетчера на
перевозку клиента. В частности, пустой массив — если:
- это обычная переписка, вопрос, подтверждение и т.п.;
- это ВОДИТЕЛЬ предлагает СЕБЯ на маршрут (а не диспетчер ищет машину для
  клиента) — признаки: "готов ехать", "свободен", "еду в сторону...",
  "ищу попутчиков/пассажиров", "пишите время подачи" (то есть автор сам ждёт,
  чтобы ЕМУ написали заказ, а не публикует заказ для кого-то другого). Такие
  сообщения выглядят структурно похоже на заявку (есть маршрут, цена,
  пассажиры), но это предложение водителя, а не заказ от диспетчера — сайт
  показывает только реальные заказы клиентов, не предложения водителей.

Пример сообщения, которое НЕ заявка (водитель предлагает себя, вернуть пустой
массив orders): "Готов ехать, пишите время подачи. Откуда: Керчь. Куда:
Краснодар. Пассажиров: 1. Сумма: 8000".

Сегодняшняя дата: {today}."""


class ParseUnavailable(RuntimeError):
    """Разбор не удался: сеть, лимиты провайдера или неверная конфигурация.

    Отличается от «сообщение не заявка» (там возвращается пустой список):
    это сигнал ВЫЗЫВАЮЩЕМУ коду отложить сообщение и попробовать позже.
    """



@dataclass
class ParseResult:
    """Результат разбора вместе с метриками, которые хочется хранить."""

    orders: list[ParsedOrder] = field(default_factory=list)
    model: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    attempts: int = 1
    from_cache: bool = False
    text_hash: str = ""

    @property
    def total_tokens(self) -> int:
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


def text_hash(text: str) -> str:
    """Стабильный отпечаток текста сообщения — ключ кэша и поле в parse_stats."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _ParseCache:
    """LRU-кэш успешных разборов.

    Дата входит в ключ: системный промпт содержит сегодняшнее число, и «завтра»
    из закэшированного вчерашнего ответа превратилось бы в прошедшую дату.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = max(0, maxsize)
        self._data: "OrderedDict[str, ParseResult]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(text: str) -> str:
        return f"{date.today().isoformat()}::{text_hash(text)}"

    def get(self, text: str) -> Optional[ParseResult]:
        if not self._maxsize:
            self.misses += 1
            return None
        key = self._key(text)
        cached = self._data.get(key)
        if cached is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return cached

    def put(self, text: str, result: ParseResult) -> None:
        if not self._maxsize:
            return
        key = self._key(text)
        self._data[key] = result
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_cache = _ParseCache(settings.parse_cache_size)


def cache_stats() -> dict[str, int]:
    """Счётчики попаданий в кэш — видно в /admin и в scripts/parse_stats.py."""
    return {"hits": _cache.hits, "misses": _cache.misses, "size": len(_cache)}


def clear_parse_cache() -> None:
    """Сброс кэша (нужен в тестах и после смены модели/промпта)."""
    _cache.clear()


def _is_transient(exc: BaseException) -> bool:
    """Повторять ли запрос после этой ошибки."""
    if isinstance(exc, (APITimeoutError, APIConnectionError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        # 429 (лимит) и 5xx (сбой провайдера) — временные; 400/401/403/404 —
        # ошибка конфигурации, повторять её бессмысленно.
        status = getattr(exc, "status_code", None)
        return status == 429 or (status is not None and status >= 500)
    if isinstance(exc, httpx.HTTPError):
        return True
    # Кривой JSON в аргументах tool-call или невалидная схема — недетерминизм
    # модели, вторая попытка часто проходит.
    return isinstance(exc, (json.JSONDecodeError, ValueError))


def _delay_for(attempt: int) -> float:
    """Экспоненциальная задержка с потолком и джиттером (чтобы не синхронизироваться)."""
    base = settings.llm_retry_backoff_seconds * (2 ** max(0, attempt - 1))
    return min(base, 30.0) + random.uniform(0, 0.35)


async def _call_llm(text: str, today: str):
    return await _client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT.format(today=today)},
            {"role": "user", "content": text},
        ],
        tools=[PARSE_ORDERS_TOOL_OPENAI],
        tool_choice={"type": "function", "function": {"name": "record_orders"}},
        # Qwen3 в DashScope по умолчанию включает "thinking mode", которая
        # несовместима с принудительным tool_choice — отключаем явно.
        extra_body={"enable_thinking": False},
    )


def _extract_orders(response) -> list[ParsedOrder]:
    tool_calls = response.choices[0].message.tool_calls or []
    for call in tool_calls:
        if call.function.name == "record_orders":
            payload = json.loads(call.function.arguments)
            return [ParsedOrder.model_validate(item) for item in payload.get("orders", [])]
    return []


async def parse_orders(text: str) -> ParseResult:
    """Основная точка входа: разбор текста с повторами, кэшем и метриками."""
    cached = _cache.get(text)
    if cached is not None:
        log.debug("Разбор из кэша (%s заявок)", len(cached.orders))
        return ParseResult(
            orders=list(cached.orders),
            model=cached.model,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            attempts=0,
            from_cache=True,
            text_hash=cached.text_hash,
        )

    today = date.today().isoformat()
    max_attempts = max(1, settings.llm_max_retries)
    last_error: Optional[BaseException] = None

    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            response = await _call_llm(text, today)
            orders = _extract_orders(response)
        except Exception as exc:
            last_error = exc
            if not _is_transient(exc):
                log.error("Фатальная ошибка LLM (не повторяем): %s: %s", type(exc).__name__, exc)
                raise ParseUnavailable(f"{type(exc).__name__}: {exc}") from exc
            if attempt < max_attempts:
                delay = _delay_for(attempt)
                log.warning(
                    "LLM попытка %s/%s не удалась (%s: %s) — повтор через %.1fs",
                    attempt, max_attempts, type(exc).__name__, exc, delay,
                )
                await asyncio.sleep(delay)
                continue
            break

        usage = getattr(response, "usage", None)
        result = ParseResult(
            orders=orders,
            model=settings.llm_model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempt,
            text_hash=text_hash(text),
        )
        _cache.put(text, result)
        return result

    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "неизвестная ошибка"
    log.error("LLM недоступна после %s попыток: %s", max_attempts, detail)
    raise ParseUnavailable(detail)


async def parse_order_texts(text: str) -> list[ParsedOrder]:
    """Совместимая обёртка: только список заявок.

    Бросает ParseUnavailable, если разбор не удался. Молча вернуть пустой
    список здесь нельзя: сбой провайдера выглядел бы как «сообщение не заявка»
    и заявка потерялась бы навсегда.
    """
    return (await parse_orders(text)).orders

    """Сброс кэша (нужен в тестах и после смены модели/промпта)."""
    _cache.clear()
