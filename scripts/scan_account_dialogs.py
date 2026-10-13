"""Ищет среди групп аккаунта те, что дают заявки, но ещё не подключены к агенту.

    python -m scripts.scan_account_dialogs acc_crimea --out /root/scan_acc_crimea.json

Работает с КОПИЕЙ файла сессии и без получения обновлений: рабочий агент не затрагивается. Только чтение:
ничего не вступает и не пишет. Для каждой неподключённой группы читает последние сообщения и оценивает, сколько
заявок диспетчеров в ней публикуется в сутки (те же правила, что в ``scripts.discover_groups``).
"""

import argparse
import asyncio
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from app.config import settings
from app.db.base import SessionLocal
from app.models import WorkGroup
from scripts.discover_groups import estimate_orders_per_day, looks_like_order

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def scan(session_name: str, per_chat: int, delay: float) -> list[dict]:
    source = PROJECT_ROOT / f"{session_name}.session"
    if not source.exists():
        raise SystemExit(f"Нет файла сессии {source.name}")
    async with SessionLocal() as db:
        connected = {row.tg_chat_id for row in (await db.execute(select(WorkGroup))).scalars().all()}
    now = datetime.now(timezone.utc)
    result: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(source, f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise SystemExit(f"Сессия {session_name} не авторизована")
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not (dialog.is_group or getattr(entity, "megagroup", False)) or dialog.id in connected:
                    continue
                try:
                    messages = await client.get_messages(entity, limit=per_chat)
                except FloodWaitError as exc:
                    print(f"FloodWait {exc.seconds}s")
                    if exc.seconds > 120:
                        break
                    await asyncio.sleep(exc.seconds + 2)
                    continue
                except RPCError:
                    continue
                if not messages:
                    continue
                dates = [m.date for m in messages]
                texts = [m.raw_text for m in messages]
                per_day, in_sample = estimate_orders_per_day(dates, texts, now)
                example = next((m.raw_text for m in messages if looks_like_order(m.raw_text)), "")
                result.append(
                    {
                        "session": session_name, "id": dialog.id, "title": dialog.name,
                        "username": getattr(entity, "username", None), "orders_per_day": per_day,
                        "orders_in_sample": in_sample, "last_message": max(dates).isoformat(),
                        "example": re.sub(r"\s+", " ", example)[:140],
                    }
                )
                await asyncio.sleep(delay)
        finally:
            await client.disconnect()
    return sorted(result, key=lambda row: row["orders_per_day"], reverse=True)


async def main(args: argparse.Namespace) -> None:
    rows = await scan(args.session, args.per_chat, args.delay)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    good = [row for row in rows if row["orders_per_day"] >= args.min_per_day]
    print(f"{args.session}: неподключённых групп {len(rows)}, с потоком от {args.min_per_day}/сут: {len(good)}")
    for row in good[:40]:
        print(f"  {row['orders_per_day']:6.1f}/сут  {row['id']}  {row['title'][:40]!r}  {row['example'][:70]!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-chat", type=int, default=100)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--min-per-day", type=float, default=5.0)
    asyncio.run(main(parser.parse_args()))
