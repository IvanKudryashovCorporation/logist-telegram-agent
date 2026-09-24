"""Управление списком рабочих групп диспетчеров, откуда агент парсит заявки (Этап 1).

    python -m scripts.manage_work_groups list
    python -m scripts.manage_work_groups add <chat_id_или_@username_или_ссылка> ["<title>"]
    python -m scripts.manage_work_groups remove <id>
    python -m scripts.manage_work_groups deactivate <id>
    python -m scripts.manage_work_groups activate <id>

Если title не указан при add — возьмём название чата из Telegram (нужна
рабочая сессия, см. scripts.auth_telegram). Для чата по числовому id без
доступа агента (ещё не состоит в чате) title обязателен.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup


async def list_groups() -> None:
    async with SessionLocal() as session:
        groups = (await session.execute(select(WorkGroup))).scalars().all()
        if not groups:
            print("Групп пока нет.")
            return
        for g in groups:
            status = "активна" if g.is_active else "выключена"
            print(f"#{g.id:<3} {g.tg_chat_id:<16} {g.title:<40} {status}")


async def add_group(chat_ref: str, title: str | None) -> None:
    tg_chat_id: int
    resolved_title = title

    try:
        tg_chat_id = int(chat_ref)
        if resolved_title is None:
            print("Для числового id укажите title вторым аргументом (не могу резолвить имя без него).")
            return
    except ValueError:
        # @username или ссылка на чат — резолвим через живую Telethon-сессию.
        from app.telegram.client import build_client

        client = build_client()
        await client.start()
        entity = await client.get_entity(chat_ref)
        from telethon.utils import get_peer_id

        tg_chat_id = get_peer_id(entity)
        resolved_title = resolved_title or getattr(entity, "title", chat_ref)
        await client.disconnect()

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(WorkGroup).where(WorkGroup.tg_chat_id == tg_chat_id))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Уже добавлена как #{existing.id}: {existing.title}")
            return
        group = WorkGroup(tg_chat_id=tg_chat_id, title=resolved_title or str(tg_chat_id))
        session.add(group)
        await session.commit()
        print(f"Добавлена группа #{group.id}: {group.title} (tg_chat_id={tg_chat_id})")


async def remove_group(group_id: int) -> None:
    async with SessionLocal() as session:
        group = await session.get(WorkGroup, group_id)
        if group is None:
            print("Группа не найдена.")
            return
        await session.delete(group)
        await session.commit()
        print(f"Группа #{group_id} удалена.")


async def set_active(group_id: int, active: bool) -> None:
    async with SessionLocal() as session:
        group = await session.get(WorkGroup, group_id)
        if group is None:
            print("Группа не найдена.")
            return
        group.is_active = active
        await session.commit()
        print(f"Группа #{group_id} ({group.title}): {'активна' if active else 'выключена'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    add_parser = sub.add_parser("add")
    add_parser.add_argument("chat_ref", help="tg_chat_id (число) или @username / ссылка на чат")
    add_parser.add_argument("title", nargs="?", default=None)

    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("group_id", type=int)

    deactivate_parser = sub.add_parser("deactivate")
    deactivate_parser.add_argument("group_id", type=int)

    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("group_id", type=int)

    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(list_groups())
    elif args.command == "add":
        asyncio.run(add_group(args.chat_ref, args.title))
    elif args.command == "remove":
        asyncio.run(remove_group(args.group_id))
    elif args.command == "deactivate":
        asyncio.run(set_active(args.group_id, False))
    elif args.command == "activate":
        asyncio.run(set_active(args.group_id, True))


if __name__ == "__main__":
    main()
