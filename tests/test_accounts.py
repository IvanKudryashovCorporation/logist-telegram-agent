"""Несколько Telegram-аккаунтов: какая сессия читает какую группу."""

from datetime import timedelta
from pathlib import Path

from app.config import settings
from app.models import WorkGroup
from app.telegram import client as client_module
from app.telegram import pending as queue
from app.telegram import queue_worker
from app.telegram.accounts import active_groups_by_session
from app.timeutil import now_utc_naive


async def _add_group(session, chat_id: int, *, session_name=None, active=True) -> None:
    session.add(
        WorkGroup(tg_chat_id=chat_id, title=f"g{chat_id}", is_active=active, session_name=session_name)
    )
    await session.commit()


async def test_active_groups_are_split_by_session(session):
    await _add_group(session, -1001)
    await _add_group(session, -1002, session_name="acc_crimea")
    await _add_group(session, -1003, session_name="acc_crimea")
    await _add_group(session, -1004, session_name="acc_crimea", active=False)
    await _add_group(session, -1005, session_name="")  # пустое имя = основная сессия

    grouped = await active_groups_by_session()

    assert grouped == {None: [-1001, -1005], "acc_crimea": [-1002, -1003]}


async def test_group_without_session_defaults_to_main_account(session):
    await _add_group(session, -2001)

    assert (await active_groups_by_session()) == {None: [-2001]}


class _FakeTelegramClient:
    def __init__(self, path, api_id, api_hash):
        self.path = path


def test_build_client_uses_main_session_by_default_and_named_file_for_extra(monkeypatch):
    monkeypatch.setattr(settings, "tg_api_id", 1)
    monkeypatch.setattr(settings, "tg_api_hash", "hash")
    monkeypatch.setattr(client_module, "TelegramClient", _FakeTelegramClient)

    main = client_module.build_client()
    extra = client_module.build_client("acc_crimea")

    assert Path(main.path).name == settings.session_path.with_suffix("").name
    assert Path(extra.path).name == "acc_crimea"
    assert Path(extra.path).parent == settings.session_path.parent


class _Msg:
    def __init__(self, text):
        self.raw_text = text

    async def get_sender(self):
        return None


class _Client:
    def __init__(self, name):
        self.name = name
        self.asked: list[tuple[int, int]] = []

    async def get_messages(self, chat_id, ids=None):
        self.asked.append((chat_id, ids))
        return _Msg("заявка на перевозку")


async def test_queue_rereads_message_with_the_client_that_owns_the_chat(session, monkeypatch):
    main, extra = _Client("main"), _Client("extra")
    for chat_id, message_id in ((-1001, 11), (-1002, 22)):
        pending = await queue.enqueue(
            chat_id=chat_id, message_id=message_id, text="текст", session=session
        )
        pending.next_attempt_at = now_utc_naive() - timedelta(minutes=1)
    await session.commit()

    async def fake_upsert(**kwargs):
        return [1]

    monkeypatch.setattr(queue_worker, "upsert_order_text", fake_upsert)
    owners = {-1001: main, -1002: extra}

    processed = await queue_worker.process_due_once(main, limit=5, client_for=owners.__getitem__)

    assert processed == 2
    assert main.asked == [(-1001, 11)]
    assert extra.asked == [(-1002, 22)]
