"""Создаёт две отдельные тестовые группы вместо одной общей "Такси":
  - "Парсинг заказов" — рабочая группа диспетчеров (WORK_GROUP_CHAT_ID)
  - "Публикация заказов" — водительская группа

    python -m scripts.create_test_groups
"""

import asyncio

from telethon.tl.functions.channels import CreateChannelRequest
from telethon.utils import get_peer_id

from app.telegram.client import build_client


async def main() -> None:
    client = build_client()
    await client.start()

    for title in ["Парсинг заказов", "Публикация заказов"]:
        result = await client(CreateChannelRequest(title=title, about="", megagroup=True))
        chat_id = get_peer_id(result.chats[0])
        print(f"{chat_id}  {title}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
