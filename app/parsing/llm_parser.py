"""Разбор свободного текста заявки диспетчера в структуру ParsedOrder через Claude."""

from datetime import date

import httpx
from anthropic import AsyncAnthropic

from app.config import settings
from app.parsing.schema import PARSE_ORDER_TOOL, ParsedOrder

# trust_env=False — иначе httpx подхватывает системные HTTP_PROXY/HTTPS_PROXY
# (в этом окружении прокси подменяет заголовок авторизации на чужой токен).
_client = AsyncAnthropic(
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
    response = await _client.messages.create(
        model=settings.llm_model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT.format(today=date.today().isoformat()),
        tools=[PARSE_ORDER_TOOL],
        tool_choice={"type": "tool", "name": "record_order"},
        messages=[{"role": "user", "content": text}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_order":
            return ParsedOrder.model_validate(block.input)

    return ParsedOrder(missing_fields=["не удалось разобрать заявку"])
