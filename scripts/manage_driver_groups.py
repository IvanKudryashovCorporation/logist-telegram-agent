"""Управление списком водительских групп для публикации заказов (вопросы 41-42, 49).

    python -m scripts.manage_driver_groups list
    python -m scripts.manage_driver_groups add <tg_chat_id> "<title>" [--directions "Севастополь,Сочи"] [--interval 0]
    python -m scripts.manage_driver_groups remove <id>

Найти tg_chat_id нужной группы: python -m scripts.list_chats
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import DriverGroup


async def list_groups() -> None:
    async with SessionLocal() as session:
        groups = (await session.execute(select(DriverGroup))).scalars().all()
        if not groups:
            print("Групп пока нет.")
            return
        for g in groups:
            status = "активна" if g.is_active else "выключена"
            print(f"#{g.id:<3} {g.title:<40} directions={g.directions or '(любое)':<30} "
                  f"interval={g.min_interval_seconds}s  {status}")


async def add_group(tg_chat_id: int, title: str, directions: str | None, interval: int) -> None:
    async with SessionLocal() as session:
        group = DriverGroup(
            tg_chat_id=tg_chat_id,
            title=title,
            directions=directions or None,
            min_interval_seconds=interval,
        )
        session.add(group)
        await session.commit()
        print(f"Добавлена группа #{group.id}: {title}")


async def remove_group(group_id: int) -> None:
    async with SessionLocal() as session:
        group = await session.get(DriverGroup, group_id)
        if group is None:
            print("Группа не найдена.")
            return
        await session.delete(group)
        await session.commit()
        print(f"Группа #{group_id} удалена.")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    add_parser = sub.add_parser("add")
    add_parser.add_argument("tg_chat_id", type=int)
    add_parser.add_argument("title")
    add_parser.add_argument("--directions", default=None)
    add_parser.add_argument("--interval", type=int, default=0)

    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("group_id", type=int)

    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(list_groups())
    elif args.command == "add":
        asyncio.run(add_group(args.tg_chat_id, args.title, args.directions, args.interval))
    elif args.command == "remove":
        asyncio.run(remove_group(args.group_id))


if __name__ == "__main__":
    main()
