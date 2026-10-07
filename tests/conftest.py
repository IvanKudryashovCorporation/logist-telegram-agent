"""Общее окружение тестов.

Две вещи здесь критичны:

1. Переменные окружения выставляются ДО первого импорта ``app.*``. И ``settings``,
   и движок БД создаются один раз при импорте модуля, поэтому «подкрутить»
   конфиг внутри теста уже не получится.
2. База — отдельный файл рядом с тестами, а не рабочий ``logist.db``: тесты
   пересоздают схемы и пишут данные, портить боевую dev-базу нельзя.
"""

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

TEST_DB_PATH = Path(__file__).resolve().parent / "_test_logist.db"

os.environ.update(
    {
        # По умолчанию SQLite-файл; чтобы прогнать тесты на PostgreSQL:
        # TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/logist_test
        "DB_NULL_POOL": "true",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL")
        or f"sqlite+aiosqlite:///{TEST_DB_PATH.as_posix()}",
        # Маленький размер страницы — тесты пагинации не должны создавать сотни строк.
        "WEB_PAGE_SIZE": "5",
        # id, под которым тесты входят через Telegram (см. tests/test_telegram_login.py)
        "ADMIN_TELEGRAM_IDS": "555000111",
        "SESSION_SECRET": "test-session-secret",
        "RATE_LIMIT_ENABLED": "false",
        "MASK_CLIENT_CONTACTS": "true",
        "WEB_HOST": "127.0.0.1",
        "TRUST_PROXY_HEADERS": "false",
        "NOTIFY_CHAT_ID": "",
        "WEB_NOTIFY_ENABLED": "false",
        "QUEUE_ENABLED": "true",
        "QUEUE_MAX_ATTEMPTS": "3",
        "QUEUE_BASE_DELAY_SECONDS": "60",
        "PREFILTER_ENABLED": "true",
        "PARSE_STATS_ENABLED": "false",
        "EXPIRE_GRACE_HOURS": "6",
        # Ключа нет — тесты не должны ходить в реальную LLM.
        "LLM_API_KEY": "",
    }
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import geo  # noqa: E402
from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.models import Order, OrderStatus  # noqa: E402
from app.search import refresh_derived  # noqa: E402
from app.timeutil import now_msk_naive, now_utc_naive  # noqa: E402


@pytest.fixture(autouse=True)
def no_extra_geocoders(monkeypatch):
    """Нечёткий поиск (Photon) и поиск районов/областей по умолчанию ничего не находят.

    Иначе любой тест, где место не нашлось в подменённом Nominatim, пошёл бы в настоящую
    сеть. Тесты этих шагов подменяют функции сами."""
    from app import geo

    async def nothing(*args, **kwargs):
        return []

    monkeypatch.setattr(geo, "_photon_search", nothing)
    monkeypatch.setattr(geo, "_nominatim_area", nothing)


@pytest.fixture(autouse=True)
def reset_in_memory_state():
    """Сброс всего, что живёт в памяти процесса между тестами."""
    from app.parsing.llm_parser import clear_parse_cache
    from app.services.notify import notifier
    from app.telegram.dedup import clear_processed
    from app.web.rate_limit import reset_limiters

    def _reset() -> None:
        clear_processed()
        clear_parse_cache()
        reset_limiters()
        # Счётчик «подряд идущих ошибок разбора» живёт в синглтоне уведомлений;
        # публичного сброса у него нет, а накапливаться между тестами он не должен.
        notifier._consecutive_errors = 0
        notifier.detach()

    _reset()
    yield
    _reset()


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    """Чистая схема на каждый тест — порядок тестов не должен влиять на результат."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def session():
    async with SessionLocal() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def client():
    """HTTP-клиент прямо в ASGI-приложение, без поднятия порта."""
    from app.web.server import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as http_client:
        yield http_client


_counter = {"n": 0}

#: Маркер «параметр не передавали» — отличается от явного None, потому что
#: ``pickup_at=None`` означает «время подачи неизвестно», а это осмысленное
#: состояние заявки, которое тесты обязаны уметь создавать.
_UNSET = object()


async def create_order(
    session,
    *,
    from_city: str = "Симферополь",
    to_city: str = "Сочи",
    price: str = "14000",
    pickup_at=_UNSET,
    passengers: int | None = 2,
    client_name: str | None = "Иван",
    client_phone: str | None = "+79990000000",
    dispatcher_username: str = "dispatcher_test",
    status: OrderStatus = OrderStatus.NEW,
    taken_by_token: str | None = None,
    raw_text: str | None = None,
    from_coords: tuple[float, float] | None = None,
    to_coords: tuple[float, float] | None = None,
) -> Order:
    """Создаёт заказ так же, как это делает агент (с пересчётом search-полей).

    ``source_message_id`` каждый раз новый: уникальность источника
    (``uq_orders_source``) не должна мешать тестам.
    """
    _counter["n"] += 1
    message_id = _counter["n"]
    if pickup_at is _UNSET:
        pickup_at = now_msk_naive() + timedelta(days=1, hours=message_id % 12)
    when = f"{pickup_at:%d.%m %H:%M} " if pickup_at is not None else ""
    order = Order(
        source_chat_id=-1001234567890,
        source_message_id=message_id,
        source_sub_index=0,
        dispatcher_tg_id=111,
        dispatcher_username=dispatcher_username,
        raw_text=raw_text
        or f"{when}{from_city} — {to_city}, {passengers} пасс., {price}, "
        f"{client_name or ''} {client_phone or ''}".strip(),
        pickup_at=pickup_at,
        from_city=from_city,
        to_city=to_city,
        from_address=f"{from_city}, ул. Ленина 1",
        to_address=f"{to_city}, аэропорт",
        passengers=passengers,
        client_name=client_name,
        client_phone=client_phone,
        client_price=Decimal(price) if price else None,
        status=status,
        taken_by_token=taken_by_token,
        taken_at=now_msk_naive() if taken_by_token else None,
    )
    # То же самое делает рантайм перед сохранением: без этого search_text пуст
    # и поиск в тестах проверял бы не то, что работает на проде.
    refresh_derived(order)
    if from_coords or to_coords:
        # Строго после refresh_derived: он сбрасывает координаты при смене города.
        order.from_lat, order.from_lon = from_coords or (None, None)
        order.to_lat, order.to_lon = to_coords or (None, None)
        order.geo_checked_at = now_utc_naive()
        order.geo_version = geo.GEO_VERSION
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


@pytest_asyncio.fixture
async def make_order(session):
    """Фабрика заказов для тестов: ``order = await make_order(price="9000")``."""

    async def _factory(**kwargs) -> Order:
        return await create_order(session, **kwargs)

    return _factory
