"""Точка входа агента.

Пока (Этап 0) только поднимает Telethon-клиент и проверяет доступ к чатам.
Обработчики заявок появятся на Этапе 1.
"""

import asyncio
import logging

from app.config import settings
from app.telegram.client import build_client

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

    log.info("Ожидание событий. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
