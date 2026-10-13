"""Собирает все ссылки на группы из истории чата (например, раздел General в Transfer 24 VIP).

    python -m scripts.harvest_group_links logist --chat 2515035207 --topic 1 --out /root/harvest.json

Работает с КОПИЕЙ файла сессии и без получения обновлений: рабочий агент не затрагивается. Только чтение.
Идёт от новых сообщений к старым, каждые ``--checkpoint`` сообщений сохраняет результат, поэтому после остановки
продолжает с того места (``--out`` тот же). Для каждой ссылки считает, сколько раз и когда она встречалась:

* ``username`` — публичное имя группы или канала (ссылка на пост ``t.me/имя/123`` тоже считается упоминанием группы);
* ``+хеш`` — приглашение в закрытую группу;
* ``c/число`` — ссылка на пост закрытой группы.
"""

import argparse
import asyncio
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
URL_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([^\s)\]>,]+)", re.IGNORECASE)
#: Служебные адреса Telegram, которые группами не являются.
IGNORED = {"joinchat", "addstickers", "share", "proxy", "socks", "login", "addtheme", "iv", "setlanguage"}


def link_key(raw: str) -> Optional[str]:
    """Ключ ссылки: ``имя``, ``+хеш`` или ``c/число``. ``None`` — не похоже на группу."""
    raw = raw.strip().rstrip("/.,;)").split("?")[0]
    parts = [part for part in raw.split("/") if part]
    if not parts:
        return None
    head = parts[0]
    if head.lower() == "joinchat" and len(parts) > 1:
        return "+" + parts[1]
    if head == "c" and len(parts) > 1:
        return "c/" + parts[1]
    if head.lower() in IGNORED or not re.fullmatch(r"\+?[A-Za-z0-9_-]{4,}", head):
        return None
    return head


def urls_of(message) -> list[str]:
    urls: list[str] = []
    if message.entities:
        for entity, text in message.get_entities_text():
            url = getattr(entity, "url", None) or text
            if "t.me" in url or "telegram.me" in url:
                urls.append(url)
    urls += [m.group(0) for m in URL_RE.finditer(message.raw_text or "")]
    return urls


def merge(links: dict, key: str, message_id: int, when: datetime, kind: str) -> None:
    stamp = when.isoformat()
    item = links.setdefault(key, {"count": 0, "first": stamp, "last": stamp, "kinds": [], "sample": message_id})
    item["count"] += 1
    if when.isoformat() < item["first"]:
        item["first"] = when.isoformat()
    if when.isoformat() > item["last"]:
        item["last"] = when.isoformat()
    if kind not in item["kinds"]:
        item["kinds"].append(kind)


async def main(args: argparse.Namespace) -> None:
    source = PROJECT_ROOT / f"{args.session}.session"
    state = {"offset_id": 0, "messages": 0, "links": {}}
    if args.out.exists():
        state = json.loads(args.out.read_text(encoding="utf-8"))
        print(f"Продолжаю: {state['messages']} сообщений, offset_id={state['offset_id']}, ссылок {len(state['links'])}")
    links = state["links"]

    def save() -> None:
        state["links"] = links
        args.out.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(source, f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            entity = await client.get_entity(PeerChannel(args.chat))
            print("Чат:", entity.title)
            done = False
            while not done:
                try:
                    async for message in client.iter_messages(
                        entity, reply_to=args.topic, offset_id=state["offset_id"], wait_time=args.wait
                    ):
                        state["messages"] += 1
                        state["offset_id"] = message.id
                        for url in urls_of(message):
                            match = URL_RE.search(url)
                            key = link_key(match.group(1)) if match else None
                            if key:
                                path = [p for p in match.group(1).split("?")[0].split("/") if p]
                                is_post = len(path) > 1
                                merge(links, key, message.id, message.date, "post" if is_post else "link")
                        if state["messages"] % args.checkpoint == 0:
                            save()
                            print(
                                f"{state['messages']} сообщений, id {message.id}, {message.date:%Y-%m-%d}, "
                                f"ссылок {len(links)}", flush=True,
                            )
                    done = True
                except FloodWaitError as exc:
                    print(f"FloodWait {exc.seconds}s", flush=True)
                    save()
                    await asyncio.sleep(exc.seconds + 5)
        finally:
            save()
            await client.disconnect()
    print(f"Готово: {state['messages']} сообщений, уникальных ссылок {len(links)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session")
    parser.add_argument("--chat", type=int, required=True, help="id канала без -100")
    parser.add_argument("--topic", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--checkpoint", type=int, default=5000)
    parser.add_argument("--wait", type=float, default=0.6)
    asyncio.run(main(parser.parse_args()))
