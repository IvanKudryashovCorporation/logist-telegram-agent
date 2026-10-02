"""Авторизация ДОПОЛНИТЕЛЬНОГО Telegram-аккаунта, группы которого тоже читает агент.

Сессия кладётся рядом с проектом как ``<имя>.session``. Это отдельный файл —
основную сессию агента (TG_SESSION_NAME) скрипт не трогает.

    python -m scripts.auth_account --phone +79782784009 --session acc_crimea
    python -m scripts.auth_account --phone +79782784009 --session acc_crimea \\
        --code 12345 --phone-code-hash <hash из первого запуска>
    ... --password "2fa-пароль"       # если включена двухфакторная защита

Запускать тем же Python, что и агент (.venv): файл сессии зависит от версии Telethon.
"""

import argparse
import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_account_client(session_name: str) -> TelegramClient:
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError("Не заданы TG_API_ID / TG_API_HASH в .env.")
    return TelegramClient(str(PROJECT_ROOT / session_name), settings.tg_api_id, settings.tg_api_hash)


async def main(args: argparse.Namespace) -> None:
    client = build_account_client(args.session)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (@{me.username}), id={me.id}")
        await client.disconnect()
        return

    if args.code is None:
        sent = await client.send_code_request(args.phone)
        print(f"Код отправлен на {args.phone}. phone_code_hash={sent.phone_code_hash}")
        await client.disconnect()
        return

    try:
        await client.sign_in(args.phone, args.code, phone_code_hash=args.phone_code_hash)
    except SessionPasswordNeededError:
        if not args.password:
            print("Включена двухфакторная защита. Повторите с --password.")
            await client.disconnect()
            return
        await client.sign_in(password=args.password)

    me = await client.get_me()
    print(f"Авторизован: {me.first_name} (@{me.username}), id={me.id}")
    print(f"Файл сессии: {args.session}.session")
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--code", default=None)
    parser.add_argument("--phone-code-hash", default=None)
    parser.add_argument("--password", default=None)
    asyncio.run(main(parser.parse_args()))
