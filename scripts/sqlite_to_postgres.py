"""Копирует все данные из SQLite-файла в PostgreSQL (схема уже создана init_postgres).

    DATABASE_URL=postgresql+asyncpg://user:pass@localhost/logist \
        python -m scripts.sqlite_to_postgres --sqlite ./logist.db [--truncate]

* Идентификаторы сохраняются как есть (на них ссылаются action_logs и заказы
  водителей), после копирования счётчики id (sequence) выставляются на максимум.
* Таблицы копируются в порядке зависимостей по внешним ключам.
* ``--truncate`` очищает целевые таблицы перед копированием — так репетицию
  можно повторять сколько угодно раз.
* В конце сверяются количества строк по каждой таблице; код возврата 1 при
  расхождении — скрипт можно вставлять в процедуру переезда.

Агента и сайт на время копирования надо остановить: иначе заказы, пришедшие
после чтения, не попадут в PostgreSQL.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  регистрирует модели в метаданных
from app.config import settings
from app.db.base import Base

BATCH = 500


async def copy(sqlite_path: Path, truncate: bool) -> int:
    if not settings.database_url.startswith("postgresql"):
        print("DATABASE_URL указывает не на PostgreSQL — копировать некуда.")
        return 1
    if not sqlite_path.exists():
        print(f"Нет файла {sqlite_path}")
        return 1

    source = create_async_engine(f"sqlite+aiosqlite:///{sqlite_path.as_posix()}")
    target = create_async_engine(settings.database_url)
    tables = Base.metadata.sorted_tables
    problems = 0

    async with source.connect() as src, target.begin() as dst:
        if truncate:
            names = ", ".join(f'"{table.name}"' for table in tables)
            await dst.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

        for table in tables:
            rows = (await src.execute(select(table))).mappings().all()
            for start in range(0, len(rows), BATCH):
                await dst.execute(table.insert(), [dict(row) for row in rows[start:start + BATCH]])
            print(f"  {table.name:<18} скопировано {len(rows)}")

            pk = list(table.primary_key.columns)
            if len(pk) == 1 and pk[0].autoincrement is not False and pk[0].type.python_type is int:
                await dst.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', '{pk[0].name}'), "
                        f"COALESCE((SELECT MAX(\"{pk[0].name}\") FROM \"{table.name}\"), 1), "
                        f"(SELECT COUNT(*) > 0 FROM \"{table.name}\"))"
                    )
                )

    print("\nСверка количества строк:")
    async with source.connect() as src, target.connect() as dst:
        for table in tables:
            before = (await src.execute(select(func.count()).select_from(table))).scalar_one()
            after = (await dst.execute(select(func.count()).select_from(table))).scalar_one()
            mark = "OK " if before == after else "ОШИБКА"
            problems += before != after
            print(f"  {mark} {table.name:<18} SQLite={before} PostgreSQL={after}")

    await source.dispose()
    await target.dispose()
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", type=Path, default=Path("logist.db"))
    parser.add_argument("--truncate", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(copy(args.sqlite, args.truncate)))
