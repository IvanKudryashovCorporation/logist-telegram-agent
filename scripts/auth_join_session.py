"""Отдельная авторизация ВТОРОЙ Telegram-сессии — только для scripts.join_work_groups.

Никогда не переиспользует logist.session (боевой файл агента на VPS): при
одновременном использовании ОДНОГО файла сессии с разных IP Telegram убивает
ключ авторизации целиком (AuthKeyDuplicatedError), что уронит боевой агент.
Эта сессия — как ещё одно залогиненное устройство на том же аккаунте
(аналог Telegram Desktop + Telegram Mobile одновременно) — Telegram это
поддерживает штатно и никак не мешает основной сессии.

    python -m scripts.auth_join_session              # запросить код
    python -m scripts.auth_join_session --code 12345  # ввести код
    python -m scripts.auth_join_session --code 12345 --password "2fa"
"""

import argparse
import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings

SESSION_NAME = "logist_join"


def build_join_client() -> TelegramClient:
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError("Не заданы TG_API_ID / TG_API_HASH в .env.")
    session_path = Path(__file__).resolve().parent.parent / SESSION_NAME
    return TelegramClient(str(session_path), settings.tg_api_id, settings.tg_api_hash)


async def main(code: str | None, password: str | None, phone_code_hash: str | None) -> None:
    client = build_join_client()
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (@{me.username}), id={me.id}")
        await client.disconnect()
        return

    if code is None:
        sent = await client.send_code_request(settings.tg_phone)
        print(f"Код отправлен на {settings.tg_phone} (phone_code_hash={sent.phone_code_hash}).")
        print("Введите его следующим запуском: python -m scripts.auth_join_session --code XXXXX")
        await client.disconnect()
        return

    try:
        await client.sign_in(phone=settings.tg_phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            print("Включена двухфакторная защита. Повторите с --password '<ваш пароль>'.")
            await client.disconnect()
            return
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Авторизован: {me.first_name} (@{me.username}), id={me.id}")
    print(f"Файл сессии: {SESSION_NAME}.session (отдельно от боевого logist.session)")

    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--phone-code-hash", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.code, args.password, args.phone_code_hash))
