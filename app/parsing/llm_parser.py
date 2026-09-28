"""Разбор свободного текста заявки диспетчера в структуру ParsedOrder через LLM.

Провайдер — любой OpenAI-совместимый chat/completions API (сейчас DashScope,
модель настраивается через LLM_MODEL). Разбор идёт через function-calling
(tool record_orders), а не через свободный текст — так поля приходят готовой
структурой без дополнительного парсинга ответа.

Одно сообщение может содержать несколько заявок сразу — LLM возвращает
МАССИВ, поэтому parse_order_texts всегда отдаёт list[ParsedOrder] (пустой
список — сообщение не заявка).
"""

import json
from datetime import date

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.parsing.schema import PARSE_ORDERS_TOOL_OPENAI, ParsedOrder

# trust_env=False — иначе httpx подхватывает системные HTTP_PROXY/HTTPS_PROXY
# (в этом окружении прокси подменяет заголовок авторизации на чужой токен).
_client = AsyncOpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url or None,
    http_client=httpx.AsyncClient(trust_env=False),
)

_SYSTEM_PROMPT = """Ты разбираешь сообщение диспетчера в рабочем чате на заявки
пассажирской перевозки (такси/трансфер). В ОДНОМ сообщении может быть
НЕСКОЛЬКО заявок сразу (списком, через пустую строку и т.п.) — тогда каждая
идёт отдельным элементом массива orders инструмента record_orders, поля
разных поездок нельзя смешивать в одну заявку. Если поле явно не следует из
текста конкретной заявки — оставь пустым, не придумывай. Если сообщение не
является заявкой вовсе (обычная переписка, вопрос, подтверждение и т.п.) —
верни пустой массив orders. Сегодняшняя дата: {today}."""


async def parse_order_texts(text: str) -> list[ParsedOrder]:
    """Возвращает список заявок, найденных в сообщении (может быть пустым,
    если сообщение не заявка, или содержать несколько элементов)."""
    response = await _client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT.format(today=date.today().isoformat())},
            {"role": "user", "content": text},
        ],
        tools=[PARSE_ORDERS_TOOL_OPENAI],
        tool_choice={"type": "function", "function": {"name": "record_orders"}},
        # Qwen3 в DashScope по умолчанию включает "thinking mode", которая
        # несовместима с принудительным tool_choice — отключаем явно.
        extra_body={"enable_thinking": False},
    )

    tool_calls = response.choices[0].message.tool_calls or []
    for call in tool_calls:
        if call.function.name == "record_orders":
            payload = json.loads(call.function.arguments)
            return [ParsedOrder.model_validate(item) for item in payload.get("orders", [])]

    return []
