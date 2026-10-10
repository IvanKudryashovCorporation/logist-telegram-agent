"""Очередь повторного разбора: отсрочки, идемпотентность, переход в FAILED.

Telethon не переигрывает уже доставленные события, поэтому без очереди заявка,
которую не удалось разобрать (лимит LLM, таймаут, сеть), терялась навсегда.
Тесты проверяют то, что делает очередь надёжной: экспоненциальную отсрочку с
потолком, отсутствие дублей и гарантию, что сообщение не крутится вечно.
"""

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import PendingMessage, PendingStatus
from app.telegram import pending as queue
from app.telegram import queue_worker
from app.timeutil import now_utc_naive

CHAT_ID = -1001234567890


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.raw_text = text

    async def get_sender(self):
        return SimpleNamespace(id=111, username="dispatcher_test")


class FakeClient:
    """Замена Telethon-клиента: воркер ходит в Telegram только за текстом."""

    def __init__(self, messages=None) -> None:
        self.messages = messages or {}
        self.requests: list[int] = []

    async def get_messages(self, chat_id, ids=None):
        self.requests.append(ids)
        return self.messages.get(ids)


async def _enqueue(session, message_id: int, text: str = "заявка на перевозку") -> PendingMessage:
    return await queue.enqueue(chat_id=CHAT_ID, message_id=message_id, text=text, session=session)


async def _reload_pending(pending_id: int) -> PendingMessage:
    """Читает запись очереди в НОВОЙ сессии.

    Воркер и ``drop_pending`` работают со своими сессиями и коммитят в них,
    поэтому кэш объектов фикстуры показывает устаревшие значения. А
    ``expire_all()`` + ``session.get()`` в асинхронном коде натыкается на
    ленивую загрузку вне greenlet — отсюда отдельная сессия.
    """
    async with SessionLocal() as fresh:
        return (
            await fresh.execute(select(PendingMessage).where(PendingMessage.id == pending_id))
        ).scalar_one()


async def _due_message_ids(limit: int = 10) -> list[int]:
    async with SessionLocal() as fresh:
        return [item.message_id for item in await queue.fetch_due(fresh, limit=limit)]


# --- Отсрочки ----------------------------------------------------------------


def test_backoff_grows_exponentially():
    base = 60  # QUEUE_BASE_DELAY_SECONDS в тестах
    now = now_utc_naive()

    delays = [
        (queue.next_attempt_at(attempts, now=now) - now).total_seconds()
        for attempts in (1, 2, 3, 4)
    ]

    assert delays == [base, base * 2, base * 4, base * 8]


def test_backoff_is_capped_at_six_hours():
    now = now_utc_naive()

    delay = (queue.next_attempt_at(50, now=now) - now).total_seconds()

    assert delay == 6 * 3600, "неограниченный рост отсрочки сломал бы повторный разбор"


# --- Постановка в очередь ----------------------------------------------------


async def test_enqueue_creates_pending_record(session):
    pending = await _enqueue(session, message_id=1001, text="Симферополь — Сочи 14000")
    await session.commit()

    stored = await session.get(PendingMessage, pending.id)
    assert stored.status == PendingStatus.PENDING
    assert stored.attempts == 0
    assert stored.text == "Симферополь — Сочи 14000"
    assert stored.next_attempt_at > now_utc_naive()


async def test_enqueue_is_idempotent(session):
    """Одно сообщение не должно плодить строки — иначе разбор повторится дважды."""
    await _enqueue(session, message_id=1002, text="первый текст")
    await session.commit()
    await _enqueue(session, message_id=1002, text="исправленный текст")
    await session.commit()

    rows = list(
        (
            await session.execute(select(PendingMessage).where(PendingMessage.message_id == 1002))
        ).scalars().all()
    )

    assert len(rows) == 1
    assert rows[0].text == "исправленный текст", "текст должен освежаться — диспетчер мог править"


async def test_enqueue_revives_failed_message(session):
    pending = await _enqueue(session, message_id=1003)
    pending.status = PendingStatus.FAILED
    pending.attempts = 9
    session.add(pending)
    await session.commit()

    await _enqueue(session, message_id=1003)
    await session.commit()

    revived = await session.get(PendingMessage, pending.id)
    assert revived.status == PendingStatus.PENDING


async def test_enqueue_commits_without_external_session():
    """Постановка в очередь обязана пережить откат транзакции разбора."""
    pending = await queue.enqueue(
        chat_id=CHAT_ID, message_id=2001, text="самостоятельная постановка"
    )

    async with SessionLocal() as fresh:
        stored = await fresh.get(PendingMessage, pending.id)
        assert stored is not None
        assert stored.status == PendingStatus.PENDING


# --- Выборка и попытки -------------------------------------------------------


async def test_fetch_due_returns_only_ready_pending(session):
    ready = await _enqueue(session, message_id=3001)
    ready.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
    session.add(ready)

    future = await _enqueue(session, message_id=3002)
    future.next_attempt_at = now_utc_naive() + timedelta(hours=1)
    session.add(future)

    done = await _enqueue(session, message_id=3003)
    done.status = PendingStatus.DONE
    done.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
    session.add(done)

    failed = await _enqueue(session, message_id=3004)
    failed.status = PendingStatus.FAILED
    failed.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
    session.add(failed)
    await session.commit()

    due = await queue.fetch_due(session, limit=10)

    assert [item.message_id for item in due] == [3001]


async def test_fetch_due_respects_limit(session):
    for index in range(5):
        item = await _enqueue(session, message_id=4000 + index)
        item.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
        session.add(item)
    await session.commit()

    assert len(await queue.fetch_due(session, limit=2)) == 2


async def test_mark_retry_increments_attempts_and_gives_up(session, monkeypatch):
    """После исчерпания QUEUE_MAX_ATTEMPTS сообщение уходит в FAILED — нужен человек."""
    monkeypatch.setattr(queue.settings, "queue_max_attempts", 3)
    pending = await _enqueue(session, message_id=5001)
    await session.commit()

    for attempt in (1, 2):
        await queue.mark_retry(session, pending, f"сбой {attempt}")
        await session.commit()
        assert pending.attempts == attempt
        assert pending.status == PendingStatus.PENDING

    await queue.mark_retry(session, pending, "сбой 3")
    await session.commit()

    assert pending.attempts == 3
    assert pending.status == PendingStatus.FAILED
    assert "сбой 3" in pending.last_error


async def test_mark_done_clears_error(session):
    pending = await _enqueue(session, message_id=6001)
    pending.last_error = "старая ошибка"
    session.add(pending)
    await session.commit()

    await queue.mark_done(session, pending)
    await session.commit()

    assert pending.status == PendingStatus.DONE
    assert pending.last_error is None


async def test_drop_pending_closes_deleted_messages(session):
    """Удалённое диспетчером сообщение больше не должно разбираться.

    Строка не удаляется, а закрывается (DONE + причина): история очереди нужна
    для диагностики, а воркер берёт только PENDING.
    """
    dropped = await _enqueue(session, message_id=7001)
    kept = await _enqueue(session, message_id=7002)
    await session.commit()

    removed = await queue.drop_pending(CHAT_ID, [7001])

    assert removed == 1
    assert (await _reload_pending(dropped.id)).status == PendingStatus.DONE
    assert (await _reload_pending(kept.id)).status == PendingStatus.PENDING
    # И главное: закрытое сообщение не вернётся в выборку воркера.
    assert 7001 not in await _due_message_ids()


async def test_drop_pending_ignores_empty_list(session):
    assert await queue.drop_pending(CHAT_ID, []) == 0


async def test_queue_depth_counts_by_status(session):
    await _enqueue(session, message_id=8001)
    failed = await _enqueue(session, message_id=8002)
    failed.status = PendingStatus.FAILED
    session.add(failed)
    await session.commit()

    depth = await queue.queue_depth()

    assert depth["pending"] == 1
    assert depth["failed"] == 1
    assert depth["done"] == 0


async def test_queue_depth_is_complete_when_empty():
    """Пустая очередь не должна ломать обращение к ключам."""
    depth = await queue.queue_depth()

    assert depth == {"pending": 0, "done": 0, "failed": 0}


# --- Воркер ------------------------------------------------------------------


async def _ready(session, message_id: int, text: str) -> PendingMessage:
    """Запись очереди, у которой уже наступил срок попытки."""
    pending = await _enqueue(session, message_id=message_id, text=text)
    pending.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
    session.add(pending)
    await session.commit()
    return pending


async def test_worker_parses_and_marks_done(session, monkeypatch):
    text = "Симферополь — Сочи 14000"
    pending = await _ready(session, 9001, text)
    calls: list[dict] = []

    async def fake_upsert(**kwargs):
        calls.append(kwargs)
        return [42]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    client = FakeClient({9001: FakeMessage(text)})

    processed = await queue_worker.process_due_once(client, limit=5)

    assert processed == 1
    assert calls[0]["chat_id"] == CHAT_ID
    assert calls[0]["message_id"] == 9001
    assert calls[0]["text"] == text
    assert (await _reload_pending(pending.id)).status == PendingStatus.DONE


async def test_worker_rereads_message_from_telegram(session, monkeypatch):
    """Воркер обязан брать свежий текст: за время ожидания диспетчер мог править заявку."""
    await _ready(session, 9002, "старый текст заявки")
    captured: dict = {}

    async def fake_upsert(**kwargs):
        captured.update(kwargs)
        return [1]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    client = FakeClient({9002: FakeMessage("обновлённый текст заявки")})

    await queue_worker.process_due_once(client, limit=5)

    assert captured["text"] == "обновлённый текст заявки"


async def test_worker_marks_done_when_message_deleted(session, monkeypatch):
    pending = await _ready(session, 9003, "текст")
    called = False

    async def fake_upsert(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)

    assert await queue_worker.process_one(FakeClient({9003: None}), pending.id) is True
    assert called is False, "разбирать удалённое сообщение бессмысленно"
    assert (await _reload_pending(pending.id)).status == PendingStatus.DONE


async def test_worker_schedules_retry_on_parse_failure(session, monkeypatch):
    pending = await _ready(session, 9004, "текст")

    async def failing_upsert(**kwargs):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(queue_worker, "upsert_order_text", failing_upsert)

    assert await queue_worker.process_one(FakeClient({9004: FakeMessage("текст")}), pending.id) is False

    stored = await _reload_pending(pending.id)
    assert stored.status == PendingStatus.PENDING
    assert stored.attempts == 1
    assert "LLM недоступна" in stored.last_error


async def test_worker_retries_when_telegram_unavailable(session, monkeypatch):
    """Сбой сети — не причина терять заявку: попытка просто переносится."""
    pending = await _ready(session, 9005, "текст")

    class BrokenClient(FakeClient):
        async def get_messages(self, chat_id, ids=None):
            raise OSError("сеть недоступна")

    assert await queue_worker.process_one(BrokenClient(), pending.id) is False

    stored = await _reload_pending(pending.id)
    assert stored.status == PendingStatus.PENDING
    assert "сеть недоступна" in stored.last_error


async def test_worker_skips_records_that_are_not_pending(session, monkeypatch):
    """FAILED разбирает только человек (scripts.retry_queue или кнопка в админке)."""
    pending = await _enqueue(session, message_id=9006)
    pending.status = PendingStatus.FAILED
    pending.next_attempt_at = now_utc_naive() - timedelta(seconds=1)
    session.add(pending)
    await session.commit()

    async def forbidden_upsert(**kwargs):
        raise AssertionError("воркер не должен трогать FAILED")

    monkeypatch.setattr(queue_worker, "upsert_order_text", forbidden_upsert)

    assert await queue_worker.process_one(FakeClient(), pending.id) is True


# --- Группы «без вступления»: клиент не знает группу по номеру ---------------------------------


class EntityLessClient:
    """Клиент, у которого группы нет в кэше: по номеру не находит, по username — да."""

    def __init__(self, message) -> None:
        self.message = message
        self.resolved: list[str] = []

    async def get_messages(self, target, ids=None):
        if isinstance(target, int):
            raise ValueError("Could not find the input entity for PeerChannel(channel_id=123)")
        return self.message

    async def get_entity(self, username):
        self.resolved.append(username)
        return SimpleNamespace(id=123, username=username)


async def test_watch_only_group_is_found_by_username_when_the_client_does_not_know_it(session, monkeypatch):
    from app.models import WorkGroup

    session.add(WorkGroup(tg_chat_id=CHAT_ID, title="Публичная", username="public_dispatch", watch_only=True))
    await session.commit()
    pending = await _ready(session, 9100, "Симферополь — Сочи 14000")

    async def fake_upsert(**kwargs):
        return [7]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    client = EntityLessClient(FakeMessage("Симферополь — Сочи 14000"))

    assert await queue_worker.process_one(client, pending.id) is True

    assert client.resolved == ["public_dispatch"]
    assert (await _reload_pending(pending.id)).status == PendingStatus.DONE


async def test_unknown_group_without_username_still_fails_with_the_real_reason(session):
    pending = await _ready(session, 9101, "заявка")
    client = EntityLessClient(FakeMessage("заявка"))

    assert await queue_worker.process_one(client, pending.id) is False

    assert "Could not find the input entity" in (await _reload_pending(pending.id)).last_error


async def test_worker_processes_a_batch_concurrently_and_survives_one_failure(session, monkeypatch):
    import asyncio

    texts = {9100 + i: f"Курск — Тольятти {30000 + i}" for i in range(6)}
    pendings = {mid: await _ready(session, mid, text) for mid, text in texts.items()}
    running = {"now": 0, "peak": 0}

    async def fake_upsert(**kwargs):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        if kwargs["message_id"] == 9103:
            raise RuntimeError("boom")
        return [1]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    client = FakeClient({mid: FakeMessage(text) for mid, text in texts.items()})

    processed = await queue_worker.process_due_once(client, limit=10, concurrency=3)

    assert processed == 6
    assert 1 < running["peak"] <= 3  # параллельно, но не больше заданного
    statuses = {mid: (await _reload_pending(p.id)).status for mid, p in pendings.items()}
    assert statuses[9103] != PendingStatus.DONE  # упавшее осталось в очереди на повтор
    assert all(s == PendingStatus.DONE for mid, s in statuses.items() if mid != 9103)


async def test_worker_skips_messages_of_a_disabled_group(session, monkeypatch):
    from app.models import WorkGroup

    session.add(WorkGroup(tg_chat_id=CHAT_ID, title="Отключённая", is_active=False))
    await session.commit()
    pending = await _ready(session, 9200, "Курск — Тольятти 30000")
    called = []

    async def fake_upsert(**kwargs):
        called.append(kwargs)
        return [1]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    client = FakeClient({9200: FakeMessage("Курск — Тольятти 30000")})

    await queue_worker.process_due_once(client, limit=5)

    assert called == []  # заказ из убранной группы не создаётся
    reloaded = await _reload_pending(pending.id)
    assert reloaded.status == PendingStatus.DONE and reloaded.last_error == "group_disabled"
