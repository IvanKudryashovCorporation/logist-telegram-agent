"""Разбор свободного текста заявки диспетчера в структуру ParsedOrder через LLM.

Провайдер — любой OpenAI-совместимый chat/completions API (сейчас DashScope,
модель настраивается через LLM_MODEL). Разбор идёт через function-calling
(tool record_order), а не через свободный текст — так поля приходят готовой
структурой без дополнительного парсинга ответа.
"""

import json
from datetime import date

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.parsing.schema import PARSE_ORDER_TOOL_OPENAI, ParsedOrder

# trust_env=False — иначе httpx подхватывает системные HTTP_PROXY/HTTPS_PROXY
# (в этом окружении прокси подменяет заголовок авторизации на чужой токен).
_client = AsyncOpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url or None,
    http_client=httpx.AsyncClient(trust_env=False),
)

_SYSTEM_PROMPT = """Ты разбираешь заявки на пассажирскую перевозку (такси/трансфер),
которые диспетчер пишет в свободной форме в рабочем чате. Разложи текст в поля
инструмента record_order. Если поле явно не следует из текста — оставь пустым,
не придумывай. Сегодняшняя дата: {today}."""


async def parse_order_text(text: str) -> ParsedOrder:
    """Возвращает разобранные поля заявки. При неполных данных missing_fields непусто."""
    response = await _client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT.format(today=date.today().isoformat())},
            {"role": "user", "content": text},
        ],
        tools=[PARSE_ORDER_TOOL_OPENAI],
        tool_choice={"type": "function", "function": {"name": "record_order"}},
        # Qwen3 в DashScope по умолчанию включает "thinking mode", которая
        # несовместима с принудительным tool_choice — отключаем явно.
        extra_body={"enable_thinking": False},
    )

    tool_calls = response.choices[0].message.tool_calls or []
    for call in tool_calls:
        if call.function.name == "record_order":
            return ParsedOrder.model_validate(json.loads(call.function.arguments))

    return ParsedOrder(missing_fields=["не удалось разобрать заявку"])
