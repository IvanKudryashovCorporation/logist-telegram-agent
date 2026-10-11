"""Удаляет из базы группы-«пустышки»: отключённые и те, что за период ничего не дали.

    python -m scripts.prune_groups            # только показать, что будет удалено
    python -m scripts.prune_groups --apply    # удалить (перед этим сохраняется JSON-копия в --backup-dir)

Под удаление попадают:
* отключённые группы;
* активные группы, из которых за ``--days`` дней пришло 0 сообщений либо одно, и оно спам.

Заказы, уже созданные из этих групп, остаются в ленте. Запускать раз в месяц.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup
from app.services import reporting


def weak_chat_ids(rows: list[dict]) -> set[int]:
    """Группы без толку: 0 сообщений либо ровно одно, и оно — спам."""
    return {r["chat_id"] for r in rows if r["messages"] == 0 or (r["messages"] == 1 and r["spam_share"] >= 1.0)}


def select_doomed(groups: list[WorkGroup], rows: list[dict]) -> list[WorkGroup]:
    weak = weak_chat_ids(rows)
    quiet = {g.tg_chat_id for g in groups if g.is_active} - {r["chat_id"] for r in rows}  # в статистике их нет вовсе
    return [g for g in groups if not g.is_active or g.tg_chat_id in weak or g.tg_chat_id in quiet]


async def main(days: int, apply: bool, backup_dir: Path) -> None:
    stats = await reporting.group_stats(days=days)
    async with SessionLocal() as session:
        groups = list((await session.execute(select(WorkGroup))).scalars().all())
        doomed = select_doomed(groups, stats["rows"])
        print(f"Всего групп {len(groups)}; к удалению {len(doomed)}")
        for group in doomed:
            reason = "отключена" if not group.is_active else "нет сообщений или один спам"
            print(f"  id={group.id} {group.tg_chat_id} @{group.username} {group.title[:40]!r} [{reason}]")
        if not apply:
            print("Это просмотр. Чтобы удалить, запустите с --apply.")
            return
        backup_dir.mkdir(parents=True, exist_ok=True)
        dump = [{c.name: getattr(g, c.name) for c in WorkGroup.__table__.columns} for g in doomed]
        path = backup_dir / f"deleted_groups_{int(time.time())}.json"
        path.write_text(json.dumps(dump, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
        for group in doomed:
            await session.delete(group)
        await session.commit()
        print(f"Удалено. Копия: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="окно статистики (до 30)")
    parser.add_argument("--apply", action="store_true", help="действительно удалить")
    parser.add_argument("--backup-dir", type=Path, default=Path("/root/backups"))
    args = parser.parse_args()
    asyncio.run(main(min(max(args.days, 1), 30), args.apply, args.backup_dir))
