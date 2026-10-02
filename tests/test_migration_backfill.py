"""Бэкфилл производных полей из миграции f3c1a9d47b02.

На пустой dev-базе бэкфилл проверить нельзя, а на проде он выполняется один раз
и молча: если он заполнит ``search_text`` не так, как это делает рантайм, поиск
по старым заявкам перестанет что-либо находить — и никто не увидит ошибки.
Тесты ниже сверяют результат миграции с :func:`app.search.refresh_derived`.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text

from app.config import settings
from app.db.base import SessionLocal
from app.models import Order
from app.search import (
    SEARCH_FIELDS,
    order_search_text,
    pickup_time_key,
    refresh_derived,
)

# Миграция f3c1a9d47b02 — часть SQLite-истории: на PostgreSQL схема строится из
# моделей (scripts/init_postgres), и проверять тут нечего.
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("sqlite"),
    reason="историческая SQLite-миграция, на PostgreSQL не применяется",
)

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "f3c1a9d47b02_reliability_indexes_queue_stats.py"
)


def _load_migration():
    """Загружает модуль миграции: имя файла не является валидным идентификатором."""
    spec = importlib.util.spec_from_file_location("reliability_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_url() -> str:
    """Миграция работает через синхронный bind — убираем драйвер aiosqlite."""
    return settings.database_url.replace("+aiosqlite", "")


async def _clear_derived(session, order_id: int) -> None:
    """Превращает строку в «старую»: производных колонок тогда ещё не было."""
    await session.execute(
        text(
            "UPDATE orders SET search_text = NULL, from_city_key = NULL, "
            "to_city_key = NULL, pickup_time_key = NULL WHERE id = :order_id"
        ),
        {"order_id": order_id},
    )
    await session.commit()


async def _reload_order(order_id: int) -> Order:
    """Читает заказ в новой сессии.

    Бэкфилл выполняется синхронным движком поверх того же файла, и кэш объектов
    основной сессии после этого показывает устаревшие/протухшие значения;
    обращение к ним в async-коде даёт ленивую загрузку вне greenlet.
    """
    async with SessionLocal() as fresh:
        return (await fresh.execute(select(Order).where(Order.id == order_id))).scalar_one()


def _run_backfill():
    """Прогоняет бэкфилл миграции на синхронном движке поверх тестовой БД."""
    migration = _load_migration()
    engine = create_engine(_sync_url())
    try:
        with engine.begin() as bind:
            return migration._backfill_derived(bind)
    finally:
        engine.dispose()


def test_migration_file_exists():
    assert MIGRATION_PATH.exists(), "миграция переименована — обновите путь в тесте"


def test_migration_uses_the_same_search_fields_as_runtime():
    """Порядок полей критичен: бэкфилл режет строку по индексу ``offset``."""
    assert tuple(_load_migration()._SEARCH_FIELDS) == tuple(SEARCH_FIELDS)


async def test_backfill_matches_runtime_values(session, make_order):
    order = await make_order(
        from_city="г. Симферополь", to_city="СОЧИ", client_name="Иван",
        client_phone="+79990000000", dispatcher_username="disp",
    )
    expected = {
        "search_text": order_search_text(order),
        "from_city_key": order.from_city_key,
        "to_city_key": order.to_city_key,
        "pickup_time_key": order.pickup_time_key,
    }
    await _clear_derived(session, order.id)

    filled = _run_backfill()

    assert filled >= 1
    restored = await _reload_order(order.id)
    assert restored.search_text == expected["search_text"]
    assert restored.from_city_key == expected["from_city_key"]
    assert restored.to_city_key == expected["to_city_key"]
    assert restored.pickup_time_key == expected["pickup_time_key"]


async def test_backfill_normalizes_city_keys(session, make_order):
    """«г. Симферополь» и «СИМФЕРОПОЛЬ» должны дать один ключ — иначе фильтр
    «откуда» работает только для одного из написаний."""
    first = await make_order(from_city="г. Симферополь", to_city="Сочи")
    second = await make_order(from_city="СИМФЕРОПОЛЬ", to_city="сочи")
    await _clear_derived(session, first.id)
    await _clear_derived(session, second.id)

    _run_backfill()

    session.expire_all()
    rows = list((await session.execute(select(Order))).scalars().all())
    assert {(row.from_city_key, row.to_city_key) for row in rows} == {("симферополь", "сочи")}


async def test_backfill_keeps_unknown_values_as_null(session, make_order):
    """Пустые поля не должны превращаться в пустые строки."""
    order = await make_order(
        from_city="", to_city="", pickup_at=None, client_name=None, client_phone=None
    )
    await _clear_derived(session, order.id)

    _run_backfill()

    restored = await _reload_order(order.id)
    assert restored.from_city_key is None
    assert restored.to_city_key is None
    assert restored.pickup_time_key is None


async def test_dedupe_step_is_safe_on_clean_table(session, make_order):
    """Шаг удаления дублей источника не должен трогать уникальные строки."""
    migration = _load_migration()
    await make_order()
    await make_order()

    engine = create_engine(_sync_url())
    try:
        with engine.begin() as bind:
            removed = migration._dedupe_orders(bind)
            total = bind.execute(text("SELECT COUNT(*) FROM orders")).scalar_one()
    finally:
        engine.dispose()

    assert removed == 0
    assert total == 2


# --- Сами функции поиска -----------------------------------------------------


def test_search_text_is_lowercase_and_contains_fields():
    order = Order(
        id=42, client_phone="+79990000000", client_name="Иван",
        from_city="Симферополь", to_city="Сочи", from_address="ул. Ленина 1",
        to_address="аэропорт", dispatcher_username="Dispatcher", contact_username=None,
    )

    value = order_search_text(order)

    assert value == value.lower()
    for fragment in ("42", "+79990000000", "иван", "симферополь", "сочи",
                     "ул. ленина 1", "аэропорт", "dispatcher"):
        assert fragment in value, fragment


def test_search_text_skips_empty_values():
    order = Order(
        id=7, client_phone=None, client_name="", from_city="Сочи", to_city=None,
        from_address=None, to_address=None, dispatcher_username=None, contact_username=None,
    )

    assert order_search_text(order) == "7 сочи"


def test_refresh_derived_fills_all_columns():
    order = Order(
        id=1, from_city="г. Ялта", to_city="Сочи", client_name="Анна",
        client_phone="+79001234567", from_address=None, to_address=None,
        dispatcher_username="disp", contact_username=None,
    )

    refresh_derived(order)

    assert order.from_city_key == "ялта"
    assert order.to_city_key == "сочи"
    assert order.search_text.startswith("1 ")
    assert order.pickup_time_key is None


def test_pickup_time_key_format():
    from datetime import datetime

    assert pickup_time_key(datetime(2026, 5, 24, 8, 5)) == "08:05"
    assert pickup_time_key(datetime(2026, 5, 24, 23, 0)) == "23:00"
    assert pickup_time_key(None) is None


def test_pickup_time_key_accepts_sqlite_text():
    """Регрессия: бэкфилл миграции падал на непустой базе.

    Сырой SQL в SQLite отдаёт DATETIME строкой, а ``pickup_time_key`` ждал
    только ``datetime`` — ``alembic upgrade head`` обрывался с AttributeError
    ровно там, где бэкфилл и был нужен.
    """
    assert pickup_time_key("2026-05-24 08:05:00.000000") == "08:05"
    assert pickup_time_key("2026-05-24T23:00:00") == "23:00"
    assert pickup_time_key("2026-05-24 7:05") == "07:05"
    assert pickup_time_key("") is None
    assert pickup_time_key("   ") is None
    assert pickup_time_key("дата неизвестна") is None
