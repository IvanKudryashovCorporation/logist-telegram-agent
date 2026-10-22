"""Осторожное вступление трёх аккаунтов в публичные группы, которые сейчас читаются опросом (``watch_only``).

Участникам Telegram присылает новые сообщения сразу, без опроса: заказ из группы появляется в ленте за 1–2 секунды.
Но массовое вступление — типичный повод для FloodWait и ограничения аккаунта, поэтому скрипт:

* распределяет группы по аккаунтам поровну по трафику (кто уже состоит в группе — остаётся её аккаунтом);
* вступает по одной группе на аккаунт с большой случайной паузой, не больше ``--daily`` вступлений в сутки на аккаунт;
* при FloodWait ждёт указанное время с запасом и замедляется; при длинном ожидании (> часа) останавливает аккаунт на сутки;
* помнит всё в файле состояния и после остановки продолжает с того же места;
* САМ базу не меняет: вступившие группы переводятся на события командой ``--apply`` (и перезапуском агента), пока
  они остаются в опросе — так ни одно сообщение не теряется.

    python -m scripts.join_watch_groups --plan                # посчитать, кто куда (ничего не вступает)
    python -m scripts.join_watch_groups --run                 # вступать (долго, запускать в фоне)
    python -m scripts.join_watch_groups --apply [--restart]   # перевести вступившие группы на события
    python -m scripts.join_watch_groups --status              # сводка

Работает с КОПИЯМИ файлов сессий (``receive_updates=False``), как остальные служебные скрипты: рабочий агент не затрагивается.
"""

import argparse
import asyncio
import json
import random
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest

from app.config import settings
from app.db.base import SessionLocal
from app.models import ParseStat, WorkGroup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "join_watch_state.json"
#: Имена сессий; у основной группы в БД ``session_name`` пустой.
ACCOUNTS = ["logist", "acc_4077", "acc_crimea"]
DB_SESSION = {"logist": None, "acc_4077": "acc_4077", "acc_crimea": "acc_crimea"}
DELAY_MIN, DELAY_MAX = 240.0, 480.0  # пауза между вступлениями одного аккаунта, секунд
MAX_DELAY = 1200.0
LONG_FLOOD = 3600


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def joined_chat_ids(name: str) -> set[int]:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(PROJECT_ROOT / f"{name}.session", f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            return {d.id async for d in client.iter_dialogs() if d.is_group or d.is_channel}
        finally:
            await client.disconnect()


def assign(groups: list[dict], already: dict[str, set[int]], weights: dict[int, int], base_load: dict[str, int]) -> dict[str, str]:
    """``{id группы: аккаунт}``: уже вступивший остаётся; остальные — туда, где меньше суммарный трафик."""
    load = {name: float(base_load.get(name, 0)) for name in ACCOUNTS}
    result: dict[str, str] = {}
    pending = []
    for group in groups:
        owners = [name for name in ACCOUNTS if group["tg_chat_id"] in already.get(name, set())]
        if owners:
            owner = min(owners, key=lambda n: load[n])
            result[str(group["id"])] = owner
            load[owner] += weights.get(group["id"], 1)
        else:
            pending.append(group)
    for group in sorted(pending, key=lambda g: -weights.get(g["id"], 1)):
        owner = min(ACCOUNTS, key=lambda n: load[n])
        result[str(group["id"])] = owner
        load[owner] += weights.get(group["id"], 1)
    return result


async def make_plan() -> dict:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(WorkGroup).where(WorkGroup.is_active.is_(True), WorkGroup.watch_only.is_(True))
            )
        ).scalars().all()
        groups = [{"id": g.id, "tg_chat_id": g.tg_chat_id, "title": g.title, "username": g.username} for g in rows]
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        counts = dict(
            (
                await session.execute(
                    select(ParseStat.chat_id, func.count()).where(ParseStat.created_at >= since).group_by(ParseStat.chat_id)
                )
            ).all()
        )
        members = (
            await session.execute(
                select(WorkGroup.session_name, func.count()).where(
                    WorkGroup.is_active.is_(True), WorkGroup.watch_only.is_(False)
                ).group_by(WorkGroup.session_name)
            )
        ).all()
    base_load = {"logist": 0, "acc_4077": 0, "acc_crimea": 0}
    weights = {g["id"]: counts.get(g["tg_chat_id"], 1) + 1 for g in groups}
    for session_name, count in members:
        base_load["logist" if not session_name else session_name] = base_load.get("logist" if not session_name else session_name, 0) + count * 500
    already = {name: await joined_chat_ids(name) for name in ACCOUNTS}
    mapping = assign(groups, already, weights, base_load)
    state = {
        "created": now_iso(),
        "groups": {
            str(g["id"]): {
                "title": g["title"], "username": g["username"], "tg_chat_id": g["tg_chat_id"],
                "account": mapping[str(g["id"])],
                "status": "joined" if g["tg_chat_id"] in already.get(mapping[str(g["id"])], set()) else "pending",
                "weight": weights[g["id"]],
            }
            for g in groups
        },
        "accounts": {name: {"blocked_until": None, "joined_today": 0, "day": None} for name in ACCOUNTS},
    }
    return state


def summary(state: dict) -> str:
    lines = []
    for name in ACCOUNTS:
        mine = [g for g in state["groups"].values() if g["account"] == name]
        by = {}
        for g in mine:
            by[g["status"]] = by.get(g["status"], 0) + 1
        lines.append(f"{name:<11} групп {len(mine):>3}  вес {sum(g['weight'] for g in mine):>6}  {by}")
    return "\n".join(lines)


async def run_account(name: str, state: dict, daily: int, stop: asyncio.Event) -> None:
    acc = state["accounts"][name]
    delay_min, delay_max = DELAY_MIN, DELAY_MAX
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(PROJECT_ROOT / f"{name}.session", f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            while not stop.is_set():
                today = datetime.now(timezone.utc).date().isoformat()
                if acc.get("day") != today:
                    acc["day"], acc["joined_today"] = today, 0
                if acc.get("blocked_until") and time.time() < acc["blocked_until"]:
                    await asyncio.sleep(min(600, acc["blocked_until"] - time.time()))
                    continue
                if acc["joined_today"] >= daily:
                    tomorrow = datetime.now(timezone.utc).replace(hour=0, minute=5, second=0, microsecond=0) + timedelta(days=1)
                    await asyncio.sleep(max(60, (tomorrow - datetime.now(timezone.utc)).total_seconds()))
                    continue
                todo = [
                    (gid, g) for gid, g in state["groups"].items()
                    if g["account"] == name and g["status"] == "pending" and g.get("username")
                ]
                if not todo:
                    print(f"[{name}] очередь пуста", flush=True)
                    return
                gid, group = max(todo, key=lambda item: item[1]["weight"])  # сначала самые загруженные
                try:
                    entity = await client.get_entity(group["username"])
                    await client(JoinChannelRequest(entity))
                    group["status"], group["joined_at"] = "joined", now_iso()
                    acc["joined_today"] += 1
                    print(f"[{name}] вступил: {group['title'][:40]} (сегодня {acc['joined_today']}/{daily})", flush=True)
                except UserAlreadyParticipantError:
                    group["status"] = "joined"
                    print(f"[{name}] уже состоит: {group['title'][:40]}", flush=True)
                except InviteRequestSentError:
                    group["status"], group["note"] = "requested", "нужна заявка админу"
                    print(f"[{name}] отправлена заявка: {group['title'][:40]}", flush=True)
                except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError) as exc:
                    group["status"], group["note"] = "failed", type(exc).__name__
                    print(f"[{name}] не удалось ({type(exc).__name__}): {group['title'][:40]}", flush=True)
                except ChannelsTooMuchError:
                    acc["blocked_until"] = time.time() + 86400
                    print(f"[{name}] лимит чатов на аккаунте — остановлен на сутки", flush=True)
                except FloodWaitError as exc:
                    wait = exc.seconds
                    print(f"[{name}] FloodWait {wait} с — жду и замедляюсь", flush=True)
                    if wait > LONG_FLOOD:
                        acc["blocked_until"] = time.time() + wait + 600
                    else:
                        acc["blocked_until"] = time.time() + wait * 1.5 + 120
                    delay_min, delay_max = min(MAX_DELAY, delay_min * 2), min(MAX_DELAY * 1.5, delay_max * 2)
                except Exception as exc:  # noqa: BLE001
                    group["status"], group["note"] = "failed", f"{type(exc).__name__}: {exc}"[:120]
                    print(f"[{name}] ошибка {type(exc).__name__}: {exc}", flush=True)
                save_state(state)
                await asyncio.sleep(random.uniform(delay_min, delay_max))
        finally:
            await client.disconnect()


async def run(daily: int) -> None:
    state = load_state()
    if not state:
        raise SystemExit("Сначала --plan")
    stop = asyncio.Event()
    await asyncio.gather(*(run_account(name, state, daily, stop) for name in ACCOUNTS))
    save_state(state)
    print("Готово.\n" + summary(state))


async def apply(restart: bool = False) -> int:
    state = load_state()
    done = [(int(gid), g) for gid, g in state.get("groups", {}).items() if g["status"] == "joined" and not g.get("applied")]
    async with SessionLocal() as session:
        for gid, group in done:
            await session.execute(
                update(WorkGroup).where(WorkGroup.id == gid).values(
                    watch_only=False, session_name=DB_SESSION[group["account"]]
                )
            )
            group["applied"] = True
        await session.commit()
    save_state(state)
    print(f"Переведено на события: {len(done)} групп. Перезапустите logist-agent, чтобы он начал слушать их.")
    if restart and done:
        import subprocess

        subprocess.run(["systemctl", "restart", "logist-agent"], check=False)
        print("logist-agent перезапущен: группы слушаются по событиям.")
    return len(done)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--restart", action="store_true", help="с --apply: перезапустить агента, если что-то переведено")
    parser.add_argument("--daily", type=int, default=15, help="не больше вступлений в сутки на аккаунт")
    args = parser.parse_args()
    if args.plan:
        state = asyncio.run(make_plan())
        save_state(state)
        print(summary(state))
    elif args.run:
        asyncio.run(run(args.daily))
    elif args.apply:
        asyncio.run(apply(args.restart))
    elif args.status:
        print(summary(load_state()))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
