"""Вступление личного Telegram-аккаунта в новые рабочие группы (Этап 1.5).

Массовое вступление в десятки-сотни чатов подряд с одного аккаунта — типичный
триггер антиспам-защиты Telegram (FloodWaitError, PEER_FLOOD, вплоть до
временного ограничения аккаунта). Поэтому скрипт:
  - обрабатывает за один запуск не больше --limit чатов (по умолчанию 15);
  - делает случайную паузу между вступлениями (--delay-min..--delay-max сек);
  - запоминает результат каждой ссылки в state-файле (join_state.json), чтобы
    повторный запуск продолжал с того же места, а не дублировал попытки;
  - при FloodWaitError сразу останавливается и печатает, сколько ждать —
    продолжать раньше этого времени бессмысленно, Telegram всё равно откажет.

Источник списка — текстовый файл, по строке на чат:
    INVITE\t<https://t.me/+hash>\t<название>
    PUBLIC\t<username>\t<название>

Использование:
    python -m scripts.join_work_groups <файл_со_списком> [--limit 15] [--state join_state.json]
    python -m scripts.join_work_groups <файл_со_списком> --dry-run   # без реальных действий
"""

import argparse
import asyncio
import json
import random
from pathlib import Path

from sqlalchemy import select
from telethon.errors import (
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.utils import get_peer_id

from app.db.base import SessionLocal
from app.models import WorkGroup
from scripts.auth_join_session import build_join_client


def load_targets(path: Path) -> list[tuple[str, str, str]]:
    targets = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        kind, ref, name = line.split("\t", 2)
        targets.append((kind, ref, name))
    return targets


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def register_work_group(entity, title: str) -> str:
    tg_chat_id = get_peer_id(entity)
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(WorkGroup).where(WorkGroup.tg_chat_id == tg_chat_id))
        ).scalar_one_or_none()
        if existing is not None:
            return f"уже была в WorkGroup #{existing.id}"
        group = WorkGroup(tg_chat_id=tg_chat_id, title=getattr(entity, "title", title) or title)
        session.add(group)
        await session.commit()
        return f"добавлена в WorkGroup #{group.id}"


async def run(
    targets_path: Path, state_path: Path, limit: int, delay_min: float, delay_max: float, dry_run: bool
) -> None:
    targets = load_targets(targets_path)
    state = load_state(state_path)

    DONE_STATUSES = ("joined", "already", "skipped", "failed_permanent", "requested")
    pending = [
        (kind, ref, name)
        for kind, ref, name in targets
        if state.get(ref, {}).get("status") not in DONE_STATUSES
    ]
    print(f"Всего в списке: {len(targets)}. Осталось необработанных: {len(pending)}.")

    if dry_run:
        for kind, ref, name in pending[:limit]:
            print(f"[DRY-RUN] {kind}\t{ref}\t{name}")
        return

    client = build_join_client()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Сессия logist_join не авторизована. Сначала выполните:\n"
            "  python -m scripts.auth_join_session\n"
            "  python -m scripts.auth_join_session --code XXXXX"
        )

    processed = 0
    try:
        for kind, ref, name in pending:
            if processed >= limit:
                break

            print(f"-> {name} ({ref}) ...", end=" ")
            try:
                if kind == "INVITE":
                    invite_hash = ref.split("t.me/+")[1] if "t.me/+" in ref else ref
                    result = await client(ImportChatInviteRequest(invite_hash))
                    entity = result.chats[0]
                else:
                    entity = await client.get_entity(ref)
                    await client(JoinChannelRequest(entity))

                note = await register_work_group(entity, name)
                state[ref] = {"status": "joined", "name": name, "note": note}
                print(f"OK — {note}")

            except UserAlreadyParticipantError:
                try:
                    entity = await client.get_entity(ref)
                    note = await register_work_group(entity, name)
                except Exception:  # noqa: BLE001
                    note = "уже состоим, но не зарегистрирована — добавьте через manage_work_groups"
                state[ref] = {"status": "already", "name": name, "note": note}
                print(f"уже состоим — {note}")

            except (
                InviteHashExpiredError,
                InviteHashInvalidError,
                UsernameInvalidError,
                UsernameNotOccupiedError,
            ) as exc:
                state[ref] = {"status": "failed_permanent", "name": name, "note": str(exc)}
                print(f"ссылка недействительна ({exc}) — пропуск навсегда")

            except InviteRequestSentError:
                # Заявка реально отправлена, просто чат с ручным одобрением админом.
                # Не наша ошибка и не тупик: когда админ одобрит, аккаунт станет
                # участником сам по себе — тогда чат добавляем через manage_work_groups.
                state[ref] = {
                    "status": "requested",
                    "name": name,
                    "note": "заявка отправлена, ждёт одобрения админом",
                }
                print("заявка отправлена, ждёт одобрения админом")

            except ChatAdminRequiredError as exc:
                state[ref] = {"status": "failed_permanent", "name": name, "note": str(exc)}
                print(f"нужна заявка/одобрение админа ({exc}) — пропуск, вступить руками")

            except ChannelsTooMuchError:
                print("аккаунт уже состоит в максимально возможном числе каналов/групп — останавливаюсь")
                break

            except FloodWaitError as exc:
                print(f"Telegram просит подождать {exc.seconds} сек. — останавливаюсь, запустите скрипт снова позже")
                save_state(state_path, state)
                return

            except Exception as exc:  # noqa: BLE001 - логируем и идём дальше, чтобы не терять весь батч
                state[ref] = {"status": "failed", "name": name, "note": f"{type(exc).__name__}: {exc}"}
                print(f"ошибка: {type(exc).__name__}: {exc}")

            processed += 1
            save_state(state_path, state)

            if processed < limit:
                pause = random.uniform(delay_min, delay_max)
                await asyncio.sleep(pause)

    finally:
        await client.disconnect()

    print(f"\nОбработано за этот запуск: {processed}.")
    remaining = len(targets) - sum(1 for v in state.values() if v.get("status") in DONE_STATUSES)
    print(f"Осталось необработанных: {max(remaining, 0)}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets_file", type=Path)
    parser.add_argument("--state", type=Path, default=Path("join_state.json"))
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--delay-min", type=float, default=25.0)
    parser.add_argument("--delay-max", type=float, default=70.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(run(args.targets_file, args.state, args.limit, args.delay_min, args.delay_max, args.dry_run))


if __name__ == "__main__":
    main()
