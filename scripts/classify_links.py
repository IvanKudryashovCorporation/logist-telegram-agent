"""Определяет, что за группа стоит за каждой собранной ссылкой (результат ``scripts.harvest_group_links``).

    python -m scripts.classify_links acc_4077 /root/harvest.json --out /root/classified.json

Работает с КОПИЕЙ файла сессии, без получения обновлений. Только чтение: ничего не вступает и не пишет.
Результат сохраняется по ходу, повторный запуск продолжает с того места. Категории:

* ``readable``      — публичная группа или канал, читается без вступления;
* ``public_join``   — публичная, но читать можно только вступив (или вход по заявке);
* ``closed_free``   — закрытая, вступление по приглашению сразу;
* ``closed_request``— закрытая, вступление по заявке админа;
* ``member``        — аккаунт уже состоит в этой группе;
* ``post_private``  — ссылка на пост закрытой группы (``t.me/c/...``), группу не определить;
* ``dead``          — имя не существует или приглашение просрочено/отозвано;
* ``bot_user``      — бот или пользователь, не группа.
"""

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, ChatInvitePeek, User

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def classify_username(client, name: str) -> dict:
    try:
        entity = await client.get_entity(name)
    except FloodWaitError:
        raise  # лимит Telegram — это не «группы нет»: вызывающий код подождёт и повторит
    except (ValueError, RPCError) as exc:
        return {"category": "dead", "detail": type(exc).__name__}
    if isinstance(entity, User):
        return {"category": "bot_user", "detail": "бот" if entity.bot else "пользователь"}
    if not isinstance(entity, (Channel, Chat)):
        return {"category": "dead", "detail": type(entity).__name__}
    info = {
        "title": getattr(entity, "title", name),
        "kind": "канал" if getattr(entity, "broadcast", False) else "группа",
        "id": getattr(entity, "id", None),
    }
    try:
        messages = await client.get_messages(entity, limit=3)
    except FloodWaitError:
        raise
    except RPCError as exc:
        return {"category": "public_join", "detail": type(exc).__name__, **info}
    if not messages and getattr(entity, "join_request", False):
        return {"category": "public_join", "detail": "по заявке", **info}
    return {"category": "readable", "detail": "читается", **info}


async def classify_invite(client, hash_: str) -> dict:
    try:
        result = await client(CheckChatInviteRequest(hash_))
    except FloodWaitError:
        raise
    except RPCError as exc:
        return {"category": "dead", "detail": type(exc).__name__}
    if isinstance(result, (ChatInviteAlready, ChatInvitePeek)):
        return {"category": "member", "title": getattr(result.chat, "title", "")}
    if isinstance(result, ChatInvite):
        return {
            "category": "closed_request" if getattr(result, "request_needed", False) else "closed_free",
            "title": result.title, "members": getattr(result, "participants_count", None),
            "kind": "канал" if getattr(result, "broadcast", False) else "группа",
        }
    return {"category": "dead", "detail": type(result).__name__}


async def main(args: argparse.Namespace) -> None:
    links = json.loads(args.harvest.read_text(encoding="utf-8"))["links"]
    done: dict = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    for key in links:
        if key.startswith("c/") and key not in done:
            done[key] = {"category": "post_private"}
    todo = [key for key in links if key not in done]
    print(f"Ссылок: {len(links)}, уже проверено: {len(done)}, осталось: {len(todo)}", flush=True)

    source = PROJECT_ROOT / f"{args.session}.session"
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(source, f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise SystemExit("Сессия не авторизована")
            for number, key in enumerate(todo, 1):
                while True:
                    try:
                        if key.startswith("+"):
                            done[key] = await classify_invite(client, key[1:])
                        else:
                            done[key] = await classify_username(client, key)
                        break
                    except FloodWaitError as exc:
                        print(f"FloodWait {exc.seconds}s на {key}", flush=True)
                        args.out.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                        if exc.seconds > args.max_wait:
                            print("Слишком долгое ожидание: останавливаюсь, запустите ещё раз позже", flush=True)
                            return
                        await asyncio.sleep(exc.seconds + 3)
                if number % 25 == 0:
                    args.out.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                    print(f"{number}/{len(todo)} проверено", flush=True)
                await asyncio.sleep(args.delay)
        finally:
            args.out.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            await client.disconnect()
    print("Готово")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session")
    parser.add_argument("harvest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--max-wait", type=int, default=900)
    asyncio.run(main(parser.parse_args()))
