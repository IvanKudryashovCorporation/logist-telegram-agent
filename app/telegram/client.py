"""Telethon-клиент, работающий от лица личного аккаунта логиста.

Логист продолжает пользоваться Telegram с телефона параллельно (вопросы 155-157),
поэтому агент только читает нужные чаты и пишет сам — ничего не помечает
прочитанным и не трогает остальные диалоги.
"""

from telethon import TelegramClient

from app.config import settings


def build_client() -> TelegramClient:
    """Создаёт клиент на сохранённой сессии. Авторизация — scripts/auth_telegram.py."""
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError(
            "Не заданы TG_API_ID / TG_API_HASH. Скопируйте .env.example в .env и заполните."
        )
    return TelegramClient(
        str(settings.session_path.with_suffix("")),
        settings.tg_api_id,
        settings.tg_api_hash,
    )
