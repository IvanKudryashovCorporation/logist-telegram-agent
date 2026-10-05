"""Безопасная проверка публичной группы: можно ли читать её, не вступая.

Файл рабочей сессии (``acc_4077.session`` и т.п.) принадлежит работающему агенту.
Открывать его ещё раз нельзя: два клиента на одном ключе мешают друг другу. Поэтому
проверка работает с КОПИЕЙ файла сессии и с ``receive_updates=False`` (не забирает
обновления у агента). Ничего не пишет и никуда не вступает.
"""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.utils import get_peer_id

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_USERNAME_RE = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?$")


def parse_username(chat_ref: str) -> Optional[str]:
    """``https://t.me/VipTAXIVIKARS`` / ``@VipTAXIVIKARS`` / ``VipTAXIVIKARS`` -> ``VipTAXIVIKARS``.

    ``None`` для ссылок-приглашений (``t.me/+…``, ``t.me/joinchat/…``): они закрытые.
    """
    ref = chat_ref.strip()
    if "/+" in ref or "joinchat" in ref or ref.startswith("+"):
        return None
    match = _USERNAME_RE.match(ref)
    return match.group(1) if match else None


@dataclass(frozen=True)
class PublicGroup:
    peer_id: int
    title: str
    username: str
    is_member: bool
    readable: bool
    sample: int  # сколько сообщений удалось прочитать пробным запросом
    reason: str = ""


async def inspect_public_group(username: str, session_name: Optional[str]) -> PublicGroup:
    """Находит группу по username и пробует прочитать её историю без вступления."""
    from telethon import functions

    source = PROJECT_ROOT / f"{session_name or settings.tg_session_name}.session"
    if not source.exists():
        raise FileNotFoundError(f"Нет файла сессии {source.name}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "probe"
        shutil.copy(source, f"{copy}.session")
        client = TelegramClient(str(copy), settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            entity = await client.get_entity(username)
            title = getattr(entity, "title", username)
            try:
                await client(functions.channels.GetParticipantRequest(entity, "me"))
                member = True
            except RPCError:
                member = False
            try:
                messages = await client.get_messages(entity, limit=5)
            except RPCError as exc:
                return PublicGroup(get_peer_id(entity), title, username, member, False, 0, type(exc).__name__)
            return PublicGroup(
                get_peer_id(entity), title, username, member, bool(messages), len(messages),
                "" if messages else "история пуста или скрыта",
            )
        finally:
            await client.disconnect()
