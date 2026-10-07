"""Сторож: что проверяет и когда пишет владельцу (без реальных сетевых вызовов)."""

import os
import time
from datetime import datetime, timedelta

import httpx
import pytest

from app.config import settings
from app.models import ParseOutcome, ParseStat, PendingMessage, PendingStatus
from app.services import watchdog
from app.services.watchdog import Check, decide, run_once

NOW = datetime(2026, 10, 2, 12, 0)


def _bad(name="сайт", detail="не отвечает"):
    return Check(name, False, detail)


def _ok(name="сайт", detail="ок"):
    return Check(name, True, detail)


# --- Когда и что сообщать -----------------------------------------------------


def test_single_failure_does_not_alert_yet():
    """Деплой с перезапуском на 15 секунд не должен поднимать тревогу."""
    messages, state = decide({}, [_bad()], NOW)

    assert messages == []
    assert state["сайт"]["fails"] == 1 and state["сайт"]["alerted"] is False


def test_second_consecutive_failure_alerts_once():
    _, state = decide({}, [_bad()], NOW)

    messages, state = decide(state, [_bad()], NOW + timedelta(minutes=5))

    assert len(messages) == 1
    name, text = messages[0]
    assert name == "сайт" and text.startswith("🔴 сайт: проблема") and "не отвечает" in text
    assert state["сайт"]["alerted"] is True


def test_no_repeat_before_reminder_interval_then_reminder():
    state = {}
    for step in (0, 5):  # две проверки подряд -> алерт
        _, state = decide(state, [_bad()], NOW + timedelta(minutes=step))

    quiet, state = decide(state, [_bad()], NOW + timedelta(hours=3))
    assert quiet == []

    reminder, state = decide(state, [_bad()], NOW + timedelta(hours=7))
    assert len(reminder) == 1 and "всё ещё не работает" in reminder[0][1]


def test_recovery_is_reported_once_and_state_is_cleared():
    state = {}
    for step in (0, 5):
        _, state = decide(state, [_bad()], NOW + timedelta(minutes=step))

    messages, state = decide(state, [_ok()], NOW + timedelta(minutes=35))

    assert len(messages) == 1 and "восстановлено" in messages[0][1] and "35 мин" in messages[0][1]
    assert state == {}
    again, _ = decide(state, [_ok()], NOW + timedelta(minutes=40))
    assert again == []


def test_short_blip_never_reported_even_on_recovery():
    _, state = decide({}, [_bad()], NOW)

    messages, state = decide(state, [_ok()], NOW + timedelta(minutes=5))

    assert messages == [] and state == {}


def test_checks_are_tracked_independently():
    _, state = decide({}, [_bad("сайт"), _ok("диск")], NOW)

    messages, _ = decide(state, [_bad("сайт"), _bad("диск")], NOW + timedelta(minutes=5))

    assert [name for name, _ in messages] == ["сайт"]  # у диска это только первый сбой


# --- Отправка и повтор при сбое ------------------------------------------------


async def test_run_once_sends_and_persists_state(tmp_path):
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    state_path = tmp_path / "state.json"
    for step in (0, 5):
        await run_once(state_path=state_path, send=fake_send, now=NOW + timedelta(minutes=step), checks=[_bad()])

    assert len(sent) == 1 and sent[0].startswith("🔴 сайт: проблема")
    assert state_path.exists()


async def test_failed_delivery_is_retried_on_the_next_run(tmp_path):
    attempts = []

    async def flaky_send(text):
        attempts.append(text)
        return len(attempts) > 1  # первая отправка не дошла

    state_path = tmp_path / "state.json"
    run = lambda minutes: run_once(  # noqa: E731
        state_path=state_path, send=flaky_send, now=NOW + timedelta(minutes=minutes), checks=[_bad()]
    )
    await run(0)
    await run(5)   # здесь алерт уходит впервые, но отправка падает
    delivered = await run(10)  # и повторяется

    assert len(attempts) == 2 and len(delivered) == 1


async def test_send_telegram_without_credentials_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "alert_chat_id", "")
    monkeypatch.setattr(settings, "alert_bot_token", "")
    monkeypatch.setattr(settings, "telegram_login_bot_token", "")

    assert await watchdog.send_telegram("текст") is False


async def test_send_telegram_posts_to_bot_api(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json):
            captured.update(url=url, json=json)
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(watchdog.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "alert_chat_id", "42")
    monkeypatch.setattr(settings, "alert_bot_token", "")
    monkeypatch.setattr(settings, "telegram_login_bot_token", "123:abc")

    assert await watchdog.send_telegram("привет") is True
    assert captured["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert captured["json"]["chat_id"] == "42" and captured["json"]["text"] == "привет"


# --- Сами проверки --------------------------------------------------------------


async def _add_stat(session, *, minutes_ago, outcome=ParseOutcome.NOT_ORDER):
    session.add(ParseStat(outcome=outcome, created_at=NOW - timedelta(minutes=minutes_ago)))
    await session.commit()


async def test_agent_alive_when_recent_messages(session):
    await _add_stat(session, minutes_ago=20)

    check = await watchdog.check_agent_alive(now=NOW)

    assert check.ok


async def test_agent_silent_for_hours_is_a_problem(session):
    await _add_stat(session, minutes_ago=5 * 60)

    check = await watchdog.check_agent_alive(now=NOW)

    assert not check.ok and "300 мин" in check.detail


async def test_agent_check_fails_on_empty_database(session):
    assert not (await watchdog.check_agent_alive(now=NOW)).ok


async def test_many_llm_errors_in_the_last_hour(session):
    for _ in range(10):
        await _add_stat(session, minutes_ago=10, outcome=ParseOutcome.ERROR)
    await _add_stat(session, minutes_ago=120, outcome=ParseOutcome.ERROR)  # старая не в счёт

    assert not (await watchdog.check_llm_errors(now=NOW)).ok


async def test_few_llm_errors_are_fine(session):
    for _ in range(3):
        await _add_stat(session, minutes_ago=10, outcome=ParseOutcome.ERROR)

    assert (await watchdog.check_llm_errors(now=NOW)).ok


async def test_failed_queue_items_raise_an_alarm(session):
    session.add(
        PendingMessage(chat_id=1, message_id=1, text="x", status=PendingStatus.FAILED,
                       next_attempt_at=NOW)
    )
    await session.commit()

    check = await watchdog.check_queue()

    assert not check.ok and "1" in check.detail


async def test_empty_queue_is_fine(session):
    assert (await watchdog.check_queue()).ok


def test_backup_check(tmp_path):
    assert not watchdog.check_backup(directory=tmp_path).ok  # дампов нет

    fresh = tmp_path / "logist-20261002-031500.sql.gz"
    fresh.write_bytes(b"x")
    assert watchdog.check_backup(directory=tmp_path).ok

    old = time.time() - 30 * 3600
    os.utime(fresh, (old, old))
    assert not watchdog.check_backup(directory=tmp_path).ok  # дамп старше 26 часов


def test_disk_check_uses_threshold(monkeypatch):
    monkeypatch.setattr(settings, "watchdog_disk_percent", 1)
    assert not watchdog.check_disk().ok

    monkeypatch.setattr(settings, "watchdog_disk_percent", 101)
    assert watchdog.check_disk().ok


async def test_site_check_handles_connection_errors(monkeypatch):
    class Boom:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def get(self, url): raise httpx.ConnectError("refused")

    monkeypatch.setattr(watchdog.httpx, "AsyncClient", Boom)

    check = await watchdog.check_site()

    assert not check.ok and "не отвечает" in check.detail


async def test_services_check_reports_inactive_units(monkeypatch):
    async def fake_state(unit):
        return "failed" if unit == "logist-agent" else "active"

    monkeypatch.setattr(watchdog, "_unit_state", fake_state)

    checks = await watchdog.check_services()

    bad = [c for c in checks if not c.ok]
    assert [c.name for c in bad] == ["служба logist-agent"]


async def test_services_check_is_skipped_without_systemd(monkeypatch):
    async def no_systemd(unit):
        return "unknown"

    monkeypatch.setattr(watchdog, "_unit_state", no_systemd)

    assert await watchdog.check_services() == []


@pytest.mark.parametrize("failing", [0, 1])
async def test_database_failure_becomes_a_check(monkeypatch, failing):
    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(watchdog, "check_agent_alive", broken)
    monkeypatch.setattr(watchdog, "check_services", lambda: _empty())
    monkeypatch.setattr(watchdog, "check_site", lambda: _ok_async())

    checks = await watchdog.run_checks()

    assert any(c.name == "база данных" and not c.ok for c in checks)


async def _empty():
    return []


async def _ok_async():
    return _ok()


def test_httpx_request_log_cannot_leak_the_bot_token():
    import logging

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- Аккаунты по отдельности и поток заявок -------------------------------------


async def _group(session, chat_id, session_name, *, created_hours_ago=48, active=True):
    from app.models import WorkGroup

    session.add(
        WorkGroup(
            tg_chat_id=chat_id, title=f"g{chat_id}", session_name=session_name, is_active=active,
            created_at=NOW - timedelta(hours=created_hours_ago),
        )
    )
    await session.commit()


async def _group_stat(session, chat_id, minutes_ago):
    session.add(ParseStat(outcome=ParseOutcome.NOT_ORDER, chat_id=chat_id,
                          created_at=NOW - timedelta(minutes=minutes_ago)))
    await session.commit()


async def test_dead_account_is_found_even_when_others_work(session):
    """Общая проверка «агент жив» зелёная, пока читает хоть один аккаунт, — отдельная ловит второго."""
    await _group(session, -1001, "acc_crimea")
    await _group(session, -1002, "acc_4077")
    await _group_stat(session, -1001, minutes_ago=10)
    await _group_stat(session, -1002, minutes_ago=8 * 60)

    checks = {c.name: c for c in await watchdog.check_account_activity(now=NOW)}

    assert checks["аккаунт acc_crimea читает группы"].ok
    assert not checks["аккаунт acc_4077 читает группы"].ok
    assert "480 мин" in checks["аккаунт acc_4077 читает группы"].detail
    assert (await watchdog.check_agent_alive(now=NOW)).ok  # так общая проверка ничего не замечала


async def test_account_without_messages_is_a_problem_but_new_account_gets_grace(session):
    await _group(session, -1001, "old_account", created_hours_ago=48)
    await _group(session, -1002, "new_account", created_hours_ago=1)

    checks = {c.name: c for c in await watchdog.check_account_activity(now=NOW)}

    assert not checks["аккаунт old_account читает группы"].ok
    assert checks["аккаунт new_account читает группы"].ok


async def test_main_session_groups_are_counted_under_its_name(session, monkeypatch):
    monkeypatch.setattr(settings, "tg_session_name", "logist")
    await _group(session, -1001, None)
    await _group_stat(session, -1001, minutes_ago=30)

    (check,) = await watchdog.check_account_activity(now=NOW)

    assert check.name == "аккаунт logist читает группы" and check.ok


async def test_inactive_groups_are_not_watched(session):
    await _group(session, -1001, "acc_old", active=False)

    assert await watchdog.check_account_activity(now=NOW) == []


async def test_no_new_orders_for_hours_is_a_problem(session, make_order):
    order = await make_order()
    order.created_at = NOW - timedelta(hours=7)
    session.add(order)
    await session.commit()

    check = await watchdog.check_orders_flow(now=NOW)

    assert not check.ok and "420 мин" in check.detail


async def test_recent_order_means_flow_is_fine(session, make_order):
    order = await make_order()
    order.created_at = NOW - timedelta(minutes=40)
    session.add(order)
    await session.commit()

    assert (await watchdog.check_orders_flow(now=NOW)).ok


async def test_orders_flow_is_quiet_on_a_fresh_database(session):
    assert (await watchdog.check_orders_flow(now=NOW)).ok


async def test_run_checks_includes_the_new_checks(session, monkeypatch):
    async def no_services():
        return []

    async def site_ok():
        return Check("сайт", True)

    monkeypatch.setattr(watchdog, "check_services", no_services)
    monkeypatch.setattr(watchdog, "check_site", site_ok)
    await _group(session, -1001, "acc_x")

    names = [c.name for c in await watchdog.run_checks()]

    assert "аккаунт acc_x читает группы" in names
    assert any(name.startswith("новые заявки") for name in names)
