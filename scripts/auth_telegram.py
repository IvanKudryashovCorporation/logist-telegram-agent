"""Первичная авторизация аккаунта логиста в Telegram.

Запускается один раз вручную:

    python -m scripts.auth_telegram

Скрипт запросит код из Telegram (и пароль 2FA, если он включён) и сохранит
файл сессии рядом с проектом. Дальше агент стартует уже без ввода кода.
"""

import asyncio

from app.config import settings
from app.telegram.client import build_client


async def main() -> None:
    client = build_client()
    await client.start(phone=settings.tg_phone or None)

    me = await client.get_me()
    print(f"Авторизован: {me.first_name} (@{me.username}), id={me.id}")
    print(f"Файл сессии: {settings.session_path}")
    print()
    print("Подставьте этот id в .env как LOGIST_USER_ID, если это аккаунт логиста.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
