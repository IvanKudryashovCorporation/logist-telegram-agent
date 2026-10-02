"""Telethon-клиент, работающий от лица личного аккаунта логиста.

Логист продолжает пользоваться Telegram с телефона параллельно (вопросы 155-157),
поэтому агент только читает нужные чаты и пишет сам — ничего не помечает
прочитанным и не трогает остальные диалоги.
"""

from typing import Optional

from telethon import TelegramClient

from app.config import settings


def build_client(session_name: Optional[str] = None) -> TelegramClient:
    """Создаёт клиент на сохранённой сессии. Авторизация — scripts/auth_telegram.py
    (основной аккаунт) или scripts/auth_account.py (дополнительный).

    ``session_name`` — имя файла сессии дополнительного аккаунта рядом с проектом;
    None — основная сессия агента (TG_SESSION_NAME).
    """
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError(
            "Не заданы TG_API_ID / TG_API_HASH. Скопируйте .env.example в .env и заполните."
        )
    path = (
        settings.session_path.with_suffix("")
        if session_name is None
        else settings.session_path.with_name(session_name)
    )
    return TelegramClient(str(path), settings.tg_api_id, settings.tg_api_hash)
