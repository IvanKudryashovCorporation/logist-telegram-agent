"""Точка входа агента.

Слушает рабочую группу диспетчеров (Этап 1), переписку с логистом в его же
«Избранном» для подтверждения публикаций (Этап 2) и отклики водителей —
в группах и в личке (Этап 3).
"""

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import DriverGroup
from app.telegram.client import build_client
from app.telegram.driver_dm import register_driver_dm_handlers
from app.telegram.driver_groups import register_driver_group_handlers
from app.telegram.logist_dm import register_logist_dm_handlers
from app.telegram.work_group import register_work_group_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("agent")


async def main() -> None:
    client = build_client()
    await client.start()

    me = await client.get_me()
    log.info("Агент запущен от лица %s (id=%s)", me.first_name, me.id)

    if not settings.work_group_chat_id:
        log.warning(
            "WORK_GROUP_CHAT_ID не задан — заявки читать неоткуда. "
            "Найдите id группы: python -m scripts.list_chats"
        )
    else:
        entity = await client.get_entity(settings.work_group_chat_id)
        log.info("Рабочая группа диспетчеров: %s", getattr(entity, "title", entity))
        register_work_group_handlers(client)

    if not settings.logist_user_id:
        log.warning("LOGIST_USER_ID не задан — подтверждения публикаций отправлять некуда.")
    else:
        register_logist_dm_handlers(client)

    async with SessionLocal() as session:
        driver_group_ids = (
            await session.execute(select(DriverGroup.tg_chat_id).where(DriverGroup.is_active.is_(True)))
        ).scalars().all()
    if not driver_group_ids:
        log.warning("Нет ни одной активной водительской группы — отклики читать неоткуда.")
    else:
        register_driver_group_handlers(client, list(driver_group_ids))
        log.info("Слушаю отклики в %s водительских группах.", len(driver_group_ids))

    register_driver_dm_handlers(client)

    log.info("Ожидание событий. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
