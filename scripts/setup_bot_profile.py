"""Оформляет профиль Telegram-бота: описание, короткое описание, команды и кнопка меню «Открыть сайт».

    python -m scripts.setup_bot_profile

Запускать на сервере (там токен бота в .env) один раз и после смены текстов. Тексты берутся из
``app.web.bot_login`` — тот же текст бот присылает на /start.
"""

import asyncio

import httpx

from app.config import settings
from app.web.bot_login import BOT_COMMANDS, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, MENU_BUTTON_TEXT


async def main() -> None:
    token = settings.telegram_login_bot_token.strip()
    if not token:
        raise SystemExit("Не задан TELEGRAM_LOGIN_BOT_TOKEN в .env.")
    site = settings.public_base_url.rstrip("/")
    calls = (
        ("setMyDescription", {"description": BOT_DESCRIPTION}),
        ("setMyShortDescription", {"short_description": BOT_SHORT_DESCRIPTION}),
        ("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]}),
        (
            "setChatMenuButton",
            {"menu_button": {"type": "web_app", "text": MENU_BUTTON_TEXT, "web_app": {"url": site}}},
        ),
    )
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        for method, payload in calls:
            response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            data = response.json()
            print(f"{method}: {'ok' if data.get('ok') else data.get('description')}")


if __name__ == "__main__":
    asyncio.run(main())
