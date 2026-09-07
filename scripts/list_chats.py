"""Печатает список диалогов аккаунта с их id.

Нужен, чтобы заполнить в .env id рабочей группы диспетчеров и завести
водительские группы в БД:

    python -m scripts.list_chats
    python -m scripts.list_chats поиск-по-названию
"""

import asyncio
import sys

from app.telegram.client import build_client


async def main(query: str = "") -> None:
    client = build_client()
    await client.start()

    query = query.lower()
    async for dialog in client.iter_dialogs():
        if query and query not in (dialog.name or "").lower():
            continue
        kind = "группа" if dialog.is_group else "канал" if dialog.is_channel else "личка"
        print(f"{dialog.id:>16}  {kind:<7}  {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
