"""Массовое подключение публичных групп «без вступления» по результатам проверки.

    python -m scripts.add_watch_groups catalog_probe.json --sessions acc_crimea,acc_4077
    python -m scripts.add_watch_groups catalog_probe.json --sessions acc_crimea,acc_4077 --dry-run

Файл — список проверенных групп (``username``, ``id``, ``title``, ``readable``, ``kind``).
Подключаются только те, что читаются без вступления; уже известные группы (по id) пропускаются.
Группы раскладываются по аккаунтам по кругу, чтобы поровну разделить нагрузку и лимиты Telegram.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup


def select_new(rows: list[dict], existing_ids: set[int], sessions: list[str]) -> list[dict]:
    """Какие группы подключить и каким аккаунтом: читаются, ещё не добавлены, без повторов."""
    chosen: list[dict] = []
    seen = set(existing_ids)
    for row in rows:
        peer_id = row.get("id")
        if not row.get("readable") or not row.get("username") or peer_id is None or peer_id in seen:
            continue
        seen.add(peer_id)
        chosen.append({**row, "session": sessions[len(chosen) % len(sessions)]})
    return chosen


async def main(path: Path, sessions: list[str], dry_run: bool) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    async with SessionLocal() as db:
        existing = set((await db.execute(select(WorkGroup.tg_chat_id))).scalars().all())
        chosen = select_new(rows, existing, sessions)
        print(f"в файле {len(rows)}, читаются и новые: {len(chosen)}")
        per_session: dict[Optional[str], int] = {}
        for row in chosen:
            per_session[row["session"]] = per_session.get(row["session"], 0) + 1
            if not dry_run:
                db.add(
                    WorkGroup(
                        tg_chat_id=row["id"], title=(row.get("title") or row["username"])[:250],
                        session_name=row["session"], watch_only=True, username=row["username"],
                    )
                )
        if not dry_run:
            await db.commit()
        print(("(пробный прогон, ничего не записано) " if dry_run else "добавлено: ") + str(per_session))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--sessions", required=True, help="имена сессий через запятую")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.file, [s.strip() for s in args.sessions.split(",") if s.strip()], args.dry_run))
