"""Список групп дополнительного аккаунта (по его файлу сессии) и что из них уже подключено.

    python -m scripts.list_account_chats acc_4077
    python -m scripts.list_account_chats acc_4077 --groups-only

Только читает: ничего не вступает и не пишет, в БД ничего не меняет. Для основной
сессии агента (TG_SESSION_NAME) не используйте — см. scripts/list_chats.py.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup
from scripts.auth_account import build_account_client


async def main(session_name: str, groups_only: bool) -> None:
    client = build_account_client(session_name)
    await client.connect()
    if not await client.is_user_authorized():
        print(f"Сессия {session_name} не авторизована: сначала scripts.auth_account.")
        await client.disconnect()
        return

    async with SessionLocal() as db:
        connected = {
            row.tg_chat_id: row.session_name
            for row in (await db.execute(select(WorkGroup))).scalars().all()
        }

    me = await client.get_me()
    print(f"# {me.first_name} (@{me.username}) id={me.id}")
    print("# id | вид | подключена (сессия) | название")
    async for dialog in client.iter_dialogs():
        if groups_only and not (dialog.is_group or dialog.is_channel):
            continue
        kind = "группа" if dialog.is_group else "канал" if dialog.is_channel else "личка"
        owner = connected.get(dialog.id, "-")
        print(f"{dialog.id} | {kind} | {owner or 'main'} | {dialog.name}")
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("--groups-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.session, args.groups_only))
