"""Подключение сотни групп без вступления: распределение, параллельный опрос, редкие проверки."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.telegram import watcher
from scripts.add_watch_groups import select_new
from tests.test_watcher import CHAT, FakeClient, Msg, Recorder, _group

NOW = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)


# --- Какие группы подключаются и чьим аккаунтом ------------------------------------------------------


def _row(i, readable=True, username=True):
    return {"id": -1000000000000 - i, "username": f"grp{i}" if username else None, "title": f"G{i}", "readable": readable}


def test_only_readable_new_groups_are_chosen_and_spread_over_accounts():
    rows = [_row(1), _row(2), _row(3, readable=False), _row(4), _row(5, username=False), _row(6)]
    existing = {-1000000000006}

    chosen = select_new(rows, existing, ["acc_a", "acc_b"])

    assert [r["username"] for r in chosen] == ["grp1", "grp2", "grp4"]
    assert [r["session"] for r in chosen] == ["acc_a", "acc_b", "acc_a"]


def test_the_same_group_listed_twice_is_added_once():
    chosen = select_new([_row(1), _row(1)], set(), ["acc_a"])

    assert len(chosen) == 1


def test_split_by_session_keeps_each_account_in_its_own_queue():
    groups = [_group(id=i, session_name=name) for i, name in enumerate(["a", "b", "a", None, "b"])]

    split = watcher.split_by_session(groups)

    assert {k: [g.id for g in v] for k, v in split.items()} == {"a": [0, 2], "b": [1, 4], None: [3]}


# --- Правки и удаления — только на «медленных» кругах -------------------------------------------------


async def test_fast_cycle_reads_only_new_messages(session):
    edited = Msg(2, NOW - timedelta(hours=1), edit_date=NOW - timedelta(seconds=30))
    client = FakeClient([edited, Msg(3, NOW - timedelta(minutes=5))])
    recorder = Recorder()

    counts = await watcher.poll_group(
        client, _group(id=1, last_message_id=2), handle=recorder.handle,
        handle_deleted=recorder.handle_deleted, now=NOW, check_changes=False,
    )

    assert recorder.handled == [(3, False)]  # правка №2 на быстром круге не разбирается
    assert counts == {"new": 1, "edited": 0, "deleted": 0}


async def test_slow_cycle_also_checks_edits(session):
    edited = Msg(2, NOW - timedelta(hours=1), edit_date=NOW - timedelta(seconds=30))
    client = FakeClient([edited])
    recorder = Recorder()

    counts = await watcher.poll_group(
        client, _group(id=1, last_message_id=2), handle=recorder.handle,
        handle_deleted=recorder.handle_deleted, now=NOW, check_changes=True,
    )

    assert recorder.handled == [(2, True)] and counts["edited"] == 1


# --- Аккаунты опрашиваются параллельно ------------------------------------------------------------------


async def test_accounts_are_polled_concurrently(monkeypatch):
    """Две очереди по две группы по ~0.2 с: параллельно это ~0.4 с, а не ~0.8 с."""
    monkeypatch.setattr(watcher, "GROUP_GAP", 0)
    active = 0
    peak = 0

    async def slow_poll(client, group, check_changes=True):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.2)
        active -= 1
        return {"new": 0, "edited": 0, "deleted": 0}

    monkeypatch.setattr(watcher, "poll_group", slow_poll)
    groups = [_group(id=i, session_name="a" if i < 2 else "b") for i in range(4)]
    queues = [
        watcher._poll_session(object(), session_groups, 0, asyncio.Event())
        for session_groups in watcher.split_by_session(groups).values()
    ]

    started = asyncio.get_running_loop().time()
    await asyncio.gather(*queues)
    elapsed = asyncio.get_running_loop().time() - started

    assert peak == 2  # по одной группе на аккаунт одновременно
    assert elapsed < 0.7


async def test_one_failing_group_does_not_stop_the_queue(monkeypatch):
    monkeypatch.setattr(watcher, "GROUP_GAP", 0)
    seen = []

    async def flaky_poll(client, group, check_changes=True):
        seen.append(group.id)
        if group.id == 0:
            raise RuntimeError("сбой")
        return {"new": 0, "edited": 0, "deleted": 0}

    monkeypatch.setattr(watcher, "poll_group", flaky_poll)

    await watcher._poll_session(object(), [_group(id=0), _group(id=1), _group(id=2)], 0, asyncio.Event())

    assert seen == [0, 1, 2]


def test_chat_constant_is_the_probe_group():
    assert CHAT == -1003362257200
