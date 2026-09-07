"""Первичная авторизация аккаунта логиста в Telegram.

Двухшаговый запуск (без интерактивного input(), удобно из чата):

    python -m scripts.auth_telegram              # запросить код, Telegram пришлёт его в приложение/SMS
    python -m scripts.auth_telegram --code 12345  # ввести полученный код
    python -m scripts.auth_telegram --code 12345 --password "2fa-пароль"  # если включена 2FA

Скрипт сохраняет файл сессии рядом с проектом. Дальше агент стартует без ввода кода.
"""

import argparse
import asyncio

from telethon.errors import SessionPasswordNeededError

from app.config import settings
from app.telegram.client import build_client


async def main(code: str | None, password: str | None) -> None:
    client = build_client()
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (@{me.username}), id={me.id}")
        await client.disconnect()
        return

    if code is None:
        sent = await client.send_code_request(settings.tg_phone)
        print(f"Код отправлен на {settings.tg_phone} (phone_code_hash={sent.phone_code_hash}).")
        print("Введите его следующим запуском: python -m scripts.auth_telegram --code XXXXX")
        await client.disconnect()
        return

    try:
        await client.sign_in(phone=settings.tg_phone, code=code)
    except SessionPasswordNeededError:
        if not password:
            print("Включена двухфакторная защита. Повторите с --password '<ваш пароль>'.")
            await client.disconnect()
            return
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Авторизован: {me.first_name} (@{me.username}), id={me.id}")
    print(f"Файл сессии: {settings.session_path}")
    print()
    print("Подставьте этот id в .env как LOGIST_USER_ID, если это аккаунт логиста.")

    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.code, args.password))
