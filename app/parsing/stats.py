"""Запись метрик качества разбора в ``parse_stats``.

Отдельный модуль, чтобы не раздувать обработчики и чтобы сбой записи метрики
никогда не ронял сам разбор заявок.
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ParseOutcome, ParseStat
from app.parsing.llm_parser import ParseResult

log = logging.getLogger("agent.stats")


def build_stat(
    outcome: ParseOutcome,
    *,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    text_hash: Optional[str] = None,
    result: Optional[ParseResult] = None,
    orders_found: int = 0,
    missing_fields: int = 0,
    error: Optional[str] = None,
) -> ParseStat:
    """Собирает строку метрики из результата разбора."""
    stat = ParseStat(
        chat_id=chat_id,
        message_id=message_id,
        text_hash=text_hash,
        outcome=outcome,
        orders_found=orders_found,
        missing_fields=missing_fields,
        error=error,
    )
    if result is not None:
        stat.model = result.model
        stat.prompt_tokens = result.prompt_tokens
        stat.completion_tokens = result.completion_tokens
        stat.latency_ms = result.latency_ms
        stat.attempts = result.attempts
        stat.text_hash = stat.text_hash or result.text_hash
    return stat


async def record(
    session: Optional[AsyncSession],
    outcome: ParseOutcome,
    **kwargs,
) -> None:
    """Пишет метрику.

    Если передана сессия — добавляет строку в неё (коммитит вызывающий код,
    метрика уезжает в БД одной транзакцией с заказом). Если сессии нет —
    открывает свою. Любой сбой здесь логируется и ПРОГЛАТЫВАЕТСЯ: метрика не
    стоит потерянной заявки.
    """
    if not settings.parse_stats_enabled:
        return
    try:
        stat = build_stat(outcome, **kwargs)
        if session is not None:
            session.add(stat)
            return
        from app.db.base import SessionLocal

        async with SessionLocal() as own:
            own.add(stat)
            await own.commit()
    except Exception:
        log.warning("Не удалось записать parse_stat (%s)", outcome.value, exc_info=True)
