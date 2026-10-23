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
    """Две очереди по две группы по ~0.2 с: параллельно это ~0.2 с, а не ~0.8 с."""
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

    assert peak == 4  # группы опрашиваются одновременно (до watch_concurrency на аккаунт)
    assert elapsed < 0.5


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


async def test_group_is_resolved_once_and_then_taken_from_the_cache():
    from tests.test_watcher import FakeClient

    client = FakeClient([])
    group = _group(id=77)
    await watcher._poll_session(client, [group], 1, asyncio.Event())
    await watcher._poll_session(client, [group], 2, asyncio.Event())

    assert len(client.entity_calls) == 1  # имя группы определили один раз, дальше берём из кэша


async def test_flood_wait_pauses_the_whole_account(monkeypatch):
    from telethon.errors import FloodWaitError

    monkeypatch.setattr(watcher, "GROUP_GAP", 0)
    monkeypatch.setattr(watcher.settings, "watch_concurrency", 1)
    started = []

    async def poll(client, group, check_changes=True):
        started.append((group.id, asyncio.get_running_loop().time()))
        if group.id == 0 and len(started) == 1:
            raise FloodWaitError(request=None, capture=0)
        return {"new": 0, "edited": 0, "deleted": 0}

    monkeypatch.setattr(watcher, "poll_group", poll)

    await watcher._poll_session(object(), [_group(id=0), _group(id=1)], 0, asyncio.Event())

    assert [g for g, _ in started] == [0, 1]


# --- Загруженные группы опрашиваются чаще, тихие реже ---------------------------------------------------


def test_poll_every_depends_on_activity(monkeypatch):
    monkeypatch.setattr(watcher.settings, "watch_hot_per_hour", 20)
    monkeypatch.setattr(watcher.settings, "watch_warm_per_hour", 4)
    monkeypatch.setattr(watcher.settings, "watch_warm_every", 2)
    monkeypatch.setattr(watcher.settings, "watch_cold_every", 4)

    assert watcher.poll_every(50) == 1
    assert watcher.poll_every(20) == 1
    assert watcher.poll_every(10) == 2
    assert watcher.poll_every(0.5) == 4


def test_schedule_polls_hot_groups_every_cycle_and_spreads_quiet_ones(monkeypatch):
    monkeypatch.setattr(watcher.settings, "watch_adaptive", True)
    monkeypatch.setattr(watcher.settings, "watch_cold_every", 4)
    hot = _group(id=1, tg_chat_id=-101, last_message_id=5)
    quiet = [_group(id=i, tg_chat_id=-100 - i, last_message_id=5) for i in range(10, 18)]
    activity = {-101: 60.0}

    polled_quiet = {group.id: 0 for group in quiet}
    for cycle in range(8):
        due = watcher.schedule([hot, *quiet], cycle, activity)
        assert 1 in due  # загруженная — каждый круг
        for group in quiet:
            polled_quiet[group.id] += group.id in due
        assert 0 < sum(1 for group in quiet if group.id in due) < len(quiet)  # тихие — не все сразу

    assert set(polled_quiet.values()) == {2}  # каждая тихая — раз в 4 круга, то есть дважды за 8


def test_never_polled_group_is_polled_at_once(monkeypatch):
    monkeypatch.setattr(watcher.settings, "watch_adaptive", True)
    fresh = _group(id=3, last_message_id=None)

    assert 3 in watcher.schedule([fresh], 1, {})


def test_adaptive_polling_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(watcher.settings, "watch_adaptive", False)
    groups = [_group(id=i, last_message_id=5) for i in range(5)]

    assert set(watcher.schedule(groups, 3, {})) == {0, 1, 2, 3, 4}


async def test_rarely_polled_group_always_checks_edits_and_deletions(monkeypatch):
    monkeypatch.setattr(watcher, "GROUP_GAP", 0)
    monkeypatch.setattr(watcher.settings, "watch_slow_every", 5)
    checks = {}

    async def poll(client, group, check_changes=True):
        checks[group.id] = check_changes
        return {"new": 0, "edited": 0, "deleted": 0}

    monkeypatch.setattr(watcher, "poll_group", poll)

    # круг №1 не «медленный» (1 % 5 != 0): загруженная группа правки не проверяет, а тихая — проверяет
    await watcher._poll_session(object(), [_group(id=1), _group(id=2)], 1, asyncio.Event(), {1: 1, 2: 4})

    assert checks == {1: False, 2: True}


async def test_activity_counts_messages_per_hour(session):
    from app.models import ParseOutcome, ParseStat

    now = datetime.now(timezone.utc)
    stamp = now.replace(tzinfo=None)
    for i in range(6):
        session.add(ParseStat(chat_id=-100, message_id=i, outcome=ParseOutcome.ORDER, created_at=stamp - timedelta(minutes=10)))
    session.add(ParseStat(chat_id=-200, message_id=1, outcome=ParseOutcome.NOT_ORDER, created_at=stamp - timedelta(hours=5)))
    await session.commit()

    activity = await watcher.activity_by_chat(2, now=now)

    assert activity == {-100: 3.0}  # 6 сообщений за 2 часа; старое сообщение другой группы не считается


# --- Страховочный опрос приоритетных групп, где аккаунт состоит ----------------------------------------


async def test_priority_member_group_is_polled_as_a_backup(session, monkeypatch):
    from app.models import WorkGroup

    monkeypatch.setattr(watcher.settings, "priority_chat_ids", "-1004413141153")
    monkeypatch.setattr(watcher.settings, "watch_member_backup", True)
    session.add_all([
        WorkGroup(tg_chat_id=-1004413141153, title="PRO", watch_only=False, is_active=True),  # состоит, приоритетная
        WorkGroup(tg_chat_id=-1005555, title="обычная", watch_only=False, is_active=True),  # состоит, не приоритетная
        WorkGroup(tg_chat_id=-1006666, title="опрос", watch_only=True, username="poll", is_active=True),
    ])
    await session.commit()

    groups = await watcher.watch_groups()

    assert {g.title: g.member for g in groups} == {"опрос": False, "PRO": True}


def test_member_backup_group_is_due_every_cycle(monkeypatch):
    monkeypatch.setattr(watcher.settings, "watch_adaptive", True)
    group = _group(id=9, member=True, last_message_id=5)

    assert all(9 in watcher.schedule([group], cycle, {}) for cycle in range(8))


async def test_member_backup_first_poll_takes_only_the_last_minutes():
    old = Msg(1, NOW - timedelta(hours=2))
    fresh = Msg(2, NOW - timedelta(minutes=1))
    recorder = Recorder()

    counts = await watcher.poll_group(
        FakeClient([old, fresh]), _group(member=True), handle=recorder.handle, handle_deleted=recorder.handle_deleted,
        now=NOW, check_changes=True,
    )

    assert recorder.handled == [(2, False)] and counts["new"] == 1  # старое уже разобрано событиями
