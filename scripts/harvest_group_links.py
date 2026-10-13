"""Собирает все ссылки на группы из истории чата (например, раздел General в Transfer 24 VIP или «PRO заказы»).

    python -m scripts.harvest_group_links logist --chat 2515035207 --topic 1 --out /root/harvest.json
    python -m scripts.harvest_group_links acc_2939 --chat 123456789 --topic 0 --buttons Источник \\
        --follow-hours 48 --out /root/harvest_pro.json

Работает с КОПИЕЙ файла сессии и без получения обновлений: рабочий агент не затрагивается. Только чтение.
Идёт от новых сообщений к старым, каждые ``--checkpoint`` сообщений сохраняет результат, поэтому после остановки
продолжает с того места (``--out`` тот же). Для каждой ссылки считает, сколько раз и когда она встречалась:

* ``username`` — публичное имя группы или канала (ссылка на пост ``t.me/имя/123`` тоже считается упоминанием группы);
* ``+хеш`` — приглашение в закрытую группу;
* ``c/число`` — ссылка на пост закрытой группы.

``--topic 0`` — читать весь чат, а не тему форума. ``--buttons Источник`` — брать ссылки ещё и из кнопок под
сообщением, у которых в подписи есть это слово (так публикует заказы бот: «Источник» ведёт на исходное сообщение).
``--follow-hours N`` — после чтения истории N часов следить за новыми сообщениями и добавлять их ссылки.
"""

import argparse
import asyncio
import json
import re
import shutil
import tempfile
import time
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


def urls_of(message, button_text: Optional[str] = None) -> list[str]:
    urls: list[str] = []
    if message.entities:
        for entity, text in message.get_entities_text():
            url = getattr(entity, "url", None) or text
            if "t.me" in url or "telegram.me" in url:
                urls.append(url)
    urls += [m.group(0) for m in URL_RE.finditer(message.raw_text or "")]
    if button_text:
        for row in message.buttons or []:
            for button in row:
                url = getattr(button, "url", None)
                if not url or button_text.lower() not in (button.text or "").lower():
                    continue
                if "t.me" in url or "telegram.me" in url:
                    urls.append(url)
    return urls


def merge(links: dict, key: str, message_id: int, when: datetime, kind: str) -> None:
    stamp = when.isoformat()
    item = links.setdefault(key, {"count": 0, "first": stamp, "last": stamp, "kinds": [], "sample": message_id})
    item["count"] += 1
    if stamp < item["first"]:
        item["first"] = stamp
    if stamp > item["last"]:
        item["last"] = stamp
    if kind not in item["kinds"]:
        item["kinds"].append(kind)


def record(links: dict, message, button_text: Optional[str]) -> None:
    for url in urls_of(message, button_text):
        match = URL_RE.search(url)
        key = link_key(match.group(1)) if match else None
        if key:
            path = [p for p in match.group(1).split("?")[0].split("/") if p]
            merge(links, key, message.id, message.date, "post" if len(path) > 1 else "link")


async def main(args: argparse.Namespace) -> None:
    source = PROJECT_ROOT / f"{args.session}.session"
    state = {"offset_id": 0, "messages": 0, "newest_id": 0, "history_done": False, "links": {}}
    if args.out.exists():
        state.update(json.loads(args.out.read_text(encoding="utf-8")))
        print(f"Продолжаю: {state['messages']} сообщений, offset_id={state['offset_id']}, ссылок {len(state['links'])}")
    links = state["links"]
    reply_to = args.topic or None

    def save() -> None:
        state["links"] = links
        args.out.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(source, f"{tmp}/s.session")
        client = TelegramClient(f"{tmp}/s", settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            entity = await client.get_entity(PeerChannel(args.chat))
            print("Чат:", entity.title, flush=True)
            while not state["history_done"]:
                try:
                    async for message in client.iter_messages(
                        entity, reply_to=reply_to, offset_id=state["offset_id"], wait_time=args.wait
                    ):
                        state["messages"] += 1
                        state["offset_id"] = message.id
                        state["newest_id"] = max(state["newest_id"], message.id)
                        record(links, message, args.buttons)
                        if state["messages"] % args.checkpoint == 0:
                            save()
                            print(
                                f"{state['messages']} сообщений, id {message.id}, {message.date:%Y-%m-%d}, "
                                f"ссылок {len(links)}", flush=True,
                            )
                    state["history_done"] = True
                except FloodWaitError as exc:
                    print(f"FloodWait {exc.seconds}s", flush=True)
                    save()
                    await asyncio.sleep(exc.seconds + 5)
            save()
            print(f"История прочитана: {state['messages']} сообщений, уникальных ссылок {len(links)}", flush=True)

            deadline = time.time() + args.follow_hours * 3600
            while time.time() < deadline:
                await asyncio.sleep(args.poll)
                try:
                    fresh = [
                        m async for m in client.iter_messages(entity, reply_to=reply_to, min_id=state["newest_id"])
                    ]
                except FloodWaitError as exc:
                    print(f"FloodWait {exc.seconds}s", flush=True)
                    await asyncio.sleep(exc.seconds + 5)
                    continue
                for message in fresh:
                    state["messages"] += 1
                    state["newest_id"] = max(state["newest_id"], message.id)
                    record(links, message, args.buttons)
                if fresh:
                    save()
                    print(f"{datetime.now():%d.%m %H:%M} новых сообщений {len(fresh)}, ссылок {len(links)}", flush=True)
        finally:
            save()
            await client.disconnect()
    print(f"Готово: {state['messages']} сообщений, уникальных ссылок {len(links)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session")
    parser.add_argument("--chat", type=int, required=True, help="id канала без -100")
    parser.add_argument("--topic", type=int, default=1, help="тема форума; 0 — весь чат")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--checkpoint", type=int, default=5000)
    parser.add_argument("--wait", type=float, default=0.6)
    parser.add_argument("--buttons", default=None, help="брать ссылки из кнопок с этим словом в подписи")
    parser.add_argument("--follow-hours", type=float, default=0.0, help="сколько часов следить за новыми сообщениями")
    parser.add_argument("--poll", type=int, default=300, help="как часто искать новые сообщения, секунд")
    asyncio.run(main(parser.parse_args()))
