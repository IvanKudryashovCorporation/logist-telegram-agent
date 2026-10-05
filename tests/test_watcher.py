"""Чтение публичных групп без вступления: опрос истории вместо событий."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import WorkGroup
from app.telegram import accounts, watcher
from app.telegram.probe import parse_username

NOW = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
CHAT = -1003362257200


@dataclass
class Msg:
    id: int
    date: datetime
    raw_text: str = "заявка"
    edit_date: Optional[datetime] = None


class FakeClient:
    """Минимум от Telethon: get_entity и get_messages (limit / min_id / ids)."""

    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.entity_calls = []

    async def get_entity(self, ref):
        self.entity_calls.append(ref)
        return "entity"

    async def get_messages(self, entity, limit=None, min_id=0, ids=None):
        if ids is not None:
            return [self.messages.get(i) for i in ids]
        found = sorted((m for m in self.messages.values() if m.id > min_id), key=lambda m: -m.id)
        return found[:limit]


class Recorder:
    def __init__(self):
        self.handled = []
        self.deleted = []

    async def handle(self, message, is_edit):
        self.handled.append((message.id, is_edit))

    async def handle_deleted(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))


def _group(**kwargs):
    base = dict(id=1, tg_chat_id=CHAT, title="VIP", username="VipTAXIVIKARS", session_name="acc_4077",
                last_message_id=None)
    base.update(kwargs)
    return watcher.WatchGroup(**base)


async def _poll(client, group, recorder, **kwargs):
    return await watcher.poll_group(
        client, group, handle=recorder.handle, handle_deleted=recorder.handle_deleted, now=NOW, **kwargs
    )


@pytest.fixture
async def stored_group(session):
    group = WorkGroup(tg_chat_id=CHAT, title="VIP", session_name="acc_4077", watch_only=True,
                      username="VipTAXIVIKARS")
    session.add(group)
    await session.commit()
    return group.id


async def test_first_poll_reads_only_recent_messages_and_remembers_the_newest(stored_group):
    client = FakeClient([
        Msg(1, NOW - timedelta(hours=30)),  # старше 12 часов — не читаем
        Msg(2, NOW - timedelta(hours=5)),
        Msg(3, NOW - timedelta(minutes=10)),
    ])
    recorder = Recorder()

    counts = await _poll(client, _group(id=stored_group), recorder)

    assert recorder.handled == [(2, False), (3, False)]  # от старых к новым
    assert counts["new"] == 2
    assert client.entity_calls == ["VipTAXIVIKARS"]  # находим по username, а не по id
    async with SessionLocal() as db:
        assert (await db.get(WorkGroup, stored_group)).last_message_id == 3


async def test_next_poll_reads_only_what_is_new(stored_group):
    client = FakeClient([Msg(i, NOW - timedelta(minutes=30 - i)) for i in range(1, 6)])
    recorder = Recorder()

    await _poll(client, _group(id=stored_group, last_message_id=3), recorder)

    assert recorder.handled == [(4, False), (5, False)]
    async with SessionLocal() as db:
        assert (await db.get(WorkGroup, stored_group)).last_message_id == 5


async def test_quiet_poll_changes_nothing(stored_group):
    client = FakeClient([Msg(1, NOW - timedelta(hours=1))])
    recorder = Recorder()

    counts = await _poll(client, _group(id=stored_group, last_message_id=1), recorder)

    assert counts == {"new": 0, "edited": 0, "deleted": 0}
    assert recorder.handled == [] and recorder.deleted == []


async def test_recent_edit_is_reprocessed_as_an_edit(stored_group):
    edited = Msg(2, NOW - timedelta(hours=1), edit_date=NOW - timedelta(seconds=30))
    stale_edit = Msg(1, NOW - timedelta(hours=3), edit_date=NOW - timedelta(hours=2))  # давняя правка
    client = FakeClient([stale_edit, edited])
    recorder = Recorder()

    counts = await _poll(client, _group(id=stored_group, last_message_id=2), recorder)

    assert recorder.handled == [(2, True)]
    assert counts["edited"] == 1


async def test_deleted_message_cancels_its_orders(stored_group, make_order, session):
    order = await make_order()
    order.source_chat_id = CHAT
    order.source_message_id = 7
    await session.commit()
    client = FakeClient([Msg(8, NOW - timedelta(minutes=5))])  # сообщения №7 в истории больше нет
    recorder = Recorder()

    counts = await _poll(client, _group(id=stored_group, last_message_id=8), recorder)

    assert recorder.deleted == [(CHAT, [7])]
    assert counts["deleted"] == 1


async def test_present_message_is_not_reported_as_deleted(stored_group, make_order, session):
    order = await make_order()
    order.source_chat_id = CHAT
    order.source_message_id = 7
    await session.commit()
    client = FakeClient([Msg(7, NOW - timedelta(minutes=5))])
    recorder = Recorder()

    await _poll(client, _group(id=stored_group, last_message_id=7), recorder)

    assert recorder.deleted == []


# --- Какие группы читаются событиями, а какие опросом --------------------------------------------


async def test_watch_only_groups_are_not_in_the_event_listener_list(session):
    session.add_all([
        WorkGroup(tg_chat_id=-1001, title="обычная", session_name=None),
        WorkGroup(tg_chat_id=-1002, title="без вступления", session_name="acc_4077", watch_only=True,
                  username="x_group"),
    ])
    await session.commit()

    assert await accounts.active_groups_by_session() == {None: [-1001]}
    watching = await watcher.watch_groups()
    assert [(g.tg_chat_id, g.username, g.session_name) for g in watching] == [(-1002, "x_group", "acc_4077")]


async def test_inactive_watch_group_is_skipped(session):
    session.add(WorkGroup(tg_chat_id=-1002, title="выключена", watch_only=True, username="x_group", is_active=False))
    await session.commit()

    assert await watcher.watch_groups() == []


async def test_existing_groups_default_to_event_mode(session):
    session.add(WorkGroup(tg_chat_id=-1005, title="старая группа"))
    await session.commit()

    group = (await session.execute(select(WorkGroup))).scalar_one()
    assert group.watch_only is False and group.last_message_id is None


# --- Ссылки ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("https://t.me/VipTAXIVIKARS", "VipTAXIVIKARS"),
        ("http://t.me/VipTAXIVIKARS/", "VipTAXIVIKARS"),
        ("t.me/VipTAXIVIKARS", "VipTAXIVIKARS"),
        ("@VipTAXIVIKARS", "VipTAXIVIKARS"),
        ("VipTAXIVIKARS", "VipTAXIVIKARS"),
        ("https://t.me/+AbCdEf123", None),  # приглашение — группа закрытая
        ("https://t.me/joinchat/AAAA", None),
        ("https://t.me/c/123/5", None),  # ссылка на сообщение закрытой группы
        ("+79991234567", None),
    ],
)
def test_parse_username(ref, expected):
    assert parse_username(ref) == expected
