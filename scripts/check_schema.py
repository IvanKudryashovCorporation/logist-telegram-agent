"""Проверка схемы БД после миграций — что реально создалось и заполнилось.

    python -m scripts.check_schema
    python -m scripts.check_schema --fix   # починить повреждённые статусы

Зачем отдельный скрипт: ``alembic upgrade head`` на SQLite выполняется через
batch-режим (пересоздание таблицы), и «успешный» выход не гарантирует, что
уникальное ограничение и все индексы действительно на месте. Перед выкладкой
на прод стоит прогнать эту проверку — она отвечает на вопросы:

* есть ли новые колонки ``orders`` (search_text и нормализованные ключи);
* есть ли ``uq_orders_source``, индексы ленты и таблицы queue/stats;
* заполнены ли производные поля у существующих заказов (бэкфилл);
* нет ли в ``orders.status`` значений, которые ORM не сможет прочитать.

Код возврата 1, если что-то не так — можно вставлять в деплой-скрипт.
"""

import argparse
import asyncio
import sys

from sqlalchemy import func, inspect, select, text

from app.db.base import SessionLocal, engine
from app.models import Order, OrderStatus

#: Колонки, которые добавляет миграция f3c1a9d47b02.
EXPECTED_ORDER_COLUMNS = (
    "search_text",
    "from_city_key",
    "to_city_key",
    "pickup_time_key",
)
#: Колонка, которую та же миграция удаляет (дубль client_price).
REMOVED_ORDER_COLUMNS = ("driver_payment",)

EXPECTED_INDEXES = (
    "ix_orders_feed",
    "ix_orders_dedup",
    "ix_orders_from_city_key",
    "ix_orders_to_city_key",
    "ix_orders_pickup_time_key",
    "ix_parse_stats_created",
    "ix_parse_stats_outcome",
    "ix_pending_messages_due",
)

EXPECTED_TABLES = ("orders", "pending_messages", "parse_stats", "work_groups", "action_logs", "drivers", "geo_places",
                   "order_subscriptions", "subscription_notifications")


async def _inspect_schema() -> tuple[list[str], set[str], set[str], set[str]]:
    """Колонки orders, все таблицы, все индексы, все unique-ограничения."""

    def _collect(sync_conn):
        inspector = inspect(sync_conn)
        columns = [column["name"] for column in inspector.get_columns("orders")]
        tables = set(inspector.get_table_names())
        indexes: set[str] = set()
        uniques: set[str] = set()
        for table in tables:
            for index in inspector.get_indexes(table):
                if not index.get("name"):
                    continue
                indexes.add(index["name"])
                # SQLite выражает UNIQUE из CREATE TABLE автоиндексом.
                if index.get("unique"):
                    uniques.add(index["name"])
            for unique in inspector.get_unique_constraints(table):
                if unique.get("name"):
                    uniques.add(unique["name"])
        return columns, tables, indexes, uniques

    async with engine.connect() as conn:
        return await conn.run_sync(_collect)


async def _backfill_coverage() -> dict[str, int]:
    """Сколько заказов реально получили производные поля при миграции."""
    async with SessionLocal() as session:
        total = (await session.execute(select(func.count(Order.id)))).scalar_one()
        if not total:
            return {"total": 0, "search_text": 0, "city_keys": 0}
        with_search = (
            await session.execute(
                select(func.count(Order.id)).where(
                    Order.search_text.is_not(None), Order.search_text != ""
                )
            )
        ).scalar_one()
        with_keys = (
            await session.execute(
                select(func.count(Order.id)).where(
                    Order.from_city_key.is_not(None), Order.from_city_key != ""
                )
            )
        ).scalar_one()
    return {"total": total, "search_text": with_search, "city_keys": with_keys}


async def _unique_source_ok(uniques: set[str]) -> bool:
    """Есть ли уникальность источника. Часть диалектов не отдаёт имя ограничения
    через инспектор, поэтому дополнительно смотрим DDL (для SQLite)."""
    if "uq_orders_source" in uniques:
        return True
    try:
        async with SessionLocal() as session:
            raw = (
                await session.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'")
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — не SQLite, такого каталога просто нет
        return False
    return bool(raw and "uq_orders_source" in raw)


#: SQLAlchemy хранит enum по ИМЕНИ, поэтому корректны только имена.
_VALID_STATUS_NAMES = {status.name for status in OrderStatus}


async def _invalid_statuses() -> list[tuple[str, int]]:
    """Значения ``orders.status``, которые ORM не сможет прочитать.

    Откат взятия когда-то был написан через ``case()``, и в БД попадало
    ЗНАЧЕНИЕ enum (``'new'``) вместо ИМЕНИ (``'NEW'``). Такие строки падают
    с ``LookupError`` при любой попытке их загрузить — то есть ломают и
    карточку заказа, и всю ленту.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(text("SELECT status, COUNT(*) FROM orders GROUP BY status"))
        ).all()
    return [(str(value), int(count)) for value, count in rows if value not in _VALID_STATUS_NAMES]


async def _fix_statuses() -> int:
    """Приводит повреждённые статусы к именам enum.

    У ``OrderStatus`` каждое имя — это значение в верхнем регистре
    (``NEW``/``new``, ``EXPIRED``/``expired``), поэтому ``UPPER()`` — точное
    и безопасное исправление.
    """
    placeholders = ", ".join(f"'{name}'" for name in sorted(_VALID_STATUS_NAMES))
    async with SessionLocal() as session:
        result = await session.execute(
            text(f"UPDATE orders SET status = UPPER(status) WHERE status NOT IN ({placeholders})")
        )
        await session.commit()
    return result.rowcount or 0


async def main(fix: bool = False) -> int:
    problems: list[str] = []
    columns, tables, indexes, uniques = await _inspect_schema()

    print("=== Таблицы ===")
    for table in EXPECTED_TABLES:
        present = table in tables
        print(f"  {'OK ' if present else 'НЕТ'} {table}")
        if not present:
            problems.append(f"нет таблицы {table}")

    print("\n=== Колонки orders ===")
    for name in EXPECTED_ORDER_COLUMNS:
        present = name in columns
        print(f"  {'OK ' if present else 'НЕТ'} {name}")
        if not present:
            problems.append(f"нет колонки orders.{name}")
    for name in REMOVED_ORDER_COLUMNS:
        absent = name not in columns
        print(f"  {'OK ' if absent else 'НЕТ'} {name} удалён")
        if not absent:
            problems.append(f"колонка orders.{name} должна быть удалена миграцией")

    print("\n=== Индексы ===")
    for name in EXPECTED_INDEXES:
        present = name in indexes
        print(f"  {'OK ' if present else 'НЕТ'} {name}")
        if not present:
            problems.append(f"нет индекса {name}")

    print("\n=== Ограничения уникальности ===")
    if await _unique_source_ok(uniques):
        print("  OK  uq_orders_source")
    else:
        print("  НЕТ uq_orders_source")
        problems.append("нет uq_orders_source — при повторном разборе возможны дубли заказов")

    print("\n=== Бэкфилл производных полей ===")
    coverage = await _backfill_coverage()
    if not coverage["total"]:
        print("  Заказов в базе нет — проверять нечего.")
    else:
        print(f"  всего заказов:   {coverage['total']}")
        print(f"  с search_text:   {coverage['search_text']}")
        print(f"  с from_city_key: {coverage['city_keys']}")
        if coverage["search_text"] < coverage["total"]:
            missing = coverage["total"] - coverage["search_text"]
            problems.append(
                f"у {missing} заказов не заполнен search_text — поиск по ним не найдёт ничего"
            )

    print("\n=== Целостность данных ===")
    invalid = await _invalid_statuses()
    if not invalid:
        print("  OK  все значения orders.status читаются ORM")
    elif fix:
        repaired = await _fix_statuses()
        print(f"  ПОЧИНЕНО {repaired} строк с некорректным статусом: {invalid}")
        remaining = await _invalid_statuses()
        if remaining:
            problems.append(f"не удалось исправить статусы: {remaining}")
    else:
        for value, count in invalid:
            print(f"  НЕТ status={value!r} у {count} заказов — строки не читаются (LookupError)")
        problems.append(
            "в orders.status есть значения вместо имён enum — "
            "почините: python -m scripts.check_schema --fix"
        )

    if problems:
        print("\n=== ПРОБЛЕМЫ ===")
        for problem in problems:
            print(f"  ! {problem}")
        print("\nВыполните: python -m alembic upgrade head")
        return 1

    print("\nСхема соответствует моделям.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="исправить значения orders.status, которые ORM не может прочитать",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(fix=args.fix)))
