"""Точка входа агента.

Этап 1: поднимает Telethon-клиент и слушает рабочую группу диспетчеров,
разбирая заявки в БД.
"""

import asyncio
import logging

from app.config import settings
from app.telegram.client import build_client
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

    log.info("Ожидание событий. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
