"""Классификация сообщений водителя в ответ на объявление (вопросы 68, 71, 75)."""

from decimal import Decimal
from typing import Optional

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.config import settings
from app.models import ResponseKind

_client = AsyncAnthropic(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url or None,
    http_client=httpx.AsyncClient(trust_env=False),
)

CLASSIFY_TOOL = {
    "name": "classify_message",
    "description": "Разобрать сообщение водителя в ответ на объявление о заказе.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["accept", "bargain", "question", "hold", "unclear"],
                "description": (
                    "accept — согласен ехать за предложенную оплату ('я', 'беру', 'еду'); "
                    "bargain — просит другую оплату ('за 15 поеду', 'торг?'); "
                    "question — задаёт вопрос ('актуален?', 'куда именно?'); "
                    "hold — просит придержать заказ за собой; "
                    "unclear — непонятно без уточнения (например просто '+')."
                ),
            },
            "requested_payment": {
                "type": "number",
                "description": "Сумма, которую просит водитель, если это торг. Иначе не указывать.",
            },
            "car_model": {"type": "string", "description": "Марка/модель авто, если упомянута."},
            "car_plate": {"type": "string", "description": "Госномер, если упомянут."},
            "phone": {"type": "string", "description": "Телефон водителя, если указан."},
            "name": {"type": "string", "description": "Имя водителя, если представился."},
        },
        "required": ["kind"],
    },
}


class ClassifiedMessage(BaseModel):
    kind: ResponseKind
    requested_payment: Optional[Decimal] = None
    car_model: Optional[str] = None
    car_plate: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None


async def classify_driver_message(text: str) -> ClassifiedMessage:
    response = await _client.messages.create(
        model=settings.llm_model,
        max_tokens=512,
        system="Ты помогаешь диспетчеру разбирать ответы водителей такси на объявления о заказах.",
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_message"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "classify_message":
            return ClassifiedMessage.model_validate(block.input)
    return ClassifiedMessage(kind=ResponseKind.UNCLEAR)
