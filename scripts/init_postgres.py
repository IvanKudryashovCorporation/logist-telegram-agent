"""Создаёт схему на ПУСТОЙ базе PostgreSQL и помечает её последней ревизией Alembic.

    DATABASE_URL=postgresql+asyncpg://user:pass@localhost/logist \
        python -m scripts.init_postgres

Почему не ``alembic upgrade head``: цепочка миграций писалась под SQLite
(batch-режим, ``= 1`` для булевых и т.п.) и на PostgreSQL не воспроизводится.
Схема здесь строится прямо из моделей, а ``alembic stamp head`` говорит Alembic,
что она уже на последней ревизии. Новые миграции дальше пишутся переносимо.
"""

import asyncio
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import app.models  # noqa: F401  регистрирует модели в метаданных
from app.config import settings
from app.db.base import Base, engine


async def create_schema() -> bool:
    async with engine.begin() as conn:
        existing = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        if existing:
            print(f"В базе уже есть таблицы ({', '.join(sorted(existing))}) — схему не трогаю.")
            return False
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return True


def main() -> int:
    if not settings.database_url.startswith("postgresql"):
        print("DATABASE_URL указывает не на PostgreSQL — ничего не делаю.")
        return 1

    if not asyncio.run(create_schema()):
        return 1
    # Alembic сам запускает event loop (env.py), поэтому вызываем его вне нашего.
    command.stamp(Config("alembic.ini"), "head")
    print(f"Схема создана ({len(Base.metadata.tables)} таблиц), Alembic помечен как head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
