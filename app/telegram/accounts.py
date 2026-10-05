"""Какими Telegram-аккаунтами читаются рабочие группы.

Основная сессия агента (``WorkGroup.session_name IS NULL``) и любое число
дополнительных: у каждой группы свой файл сессии. Один клиент может слушать
много групп, но группы разных аккаунтов нужны разным клиентам.
"""

from typing import Optional

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup


async def active_groups_by_session() -> dict[Optional[str], list[int]]:
    """``{имя сессии | None: [tg_chat_id, ...]}`` по активным группам."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(WorkGroup.session_name, WorkGroup.tg_chat_id)
                .where(WorkGroup.is_active.is_(True), WorkGroup.watch_only.is_(False))
                .order_by(WorkGroup.id)
            )
        ).all()
    grouped: dict[Optional[str], list[int]] = {}
    for session_name, chat_id in rows:
        grouped.setdefault(session_name or None, []).append(chat_id)
    return grouped
