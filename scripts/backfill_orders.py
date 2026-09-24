"""Разбирает последние N сообщений из активных рабочих групп задним числом
(агент в норме реагирует только на новые сообщения через события Telethon).

    python -m scripts.backfill_orders            # по 5 последних сообщений на группу
    python -m scripts.backfill_orders --limit 20
    python -m scripts.backfill_orders --group <id_из_manage_work_groups>

Не запускать одновременно с работающим main.py — конфликт за файл сессии,
сначала остановите агента (systemctl stop logist-agent).
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup
from app.telegram.client import build_client
from app.telegram.work_group import _upsert_order


async def backfill(limit: int, only_group_id: int | None) -> None:
    client = build_client()
    await client.start()

    async with SessionLocal() as session:
        stmt = select(WorkGroup).where(WorkGroup.is_active.is_(True))
        if only_group_id is not None:
            stmt = stmt.where(WorkGroup.id == only_group_id)
        groups = (await session.execute(stmt)).scalars().all()

    if not groups:
        print("Нет активных рабочих групп.")
        await client.disconnect()
        return

    for group in groups:
        print(f"\n--- {group.title} ---")
        messages = await client.get_messages(group.tg_chat_id, limit=limit)
        for message in reversed(messages):  # от старых к новым, как приходили бы в реале
            text = (message.raw_text or "").strip()
            if not text or message.out or message.reply_to_msg_id is not None:
                print(f"  [{message.id}] пропущено (пусто/реплай/своё)")
                continue
            try:
                order_id = await _upsert_order(message, is_edit=False)
            except Exception as exc:
                print(f"  [{message.id}] ОШИБКА: {exc}")
                continue
            if order_id is None:
                print(f"  [{message.id}] не заявка: {text[:60]!r}")
            else:
                print(f"  [{message.id}] заказ #{order_id}")

    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--group", type=int, default=None, help="id группы из scripts.manage_work_groups list")
    args = parser.parse_args()
    asyncio.run(backfill(args.limit, args.group))
