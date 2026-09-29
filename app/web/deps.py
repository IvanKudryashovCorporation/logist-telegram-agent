"""Зависимости веб-слоя: кто такой «текущий водитель» и «админ».

Регистрации на сайте нет по продуктовой задумке: водителя различаем анонимной
cookie, админку — подписанной cookie с паролем из .env. Обе живут здесь, чтобы
роуты не занимались криптографией и куками самостоятельно.

Секреты сравниваются через :func:`hmac.compare_digest` — обычное ``==``
уязвимо к timing-атаке, а для пароля/подписи это критично.
"""

import hashlib
import hmac
import time
import uuid
from typing import Optional
from urllib.parse import quote, unquote

from fastapi import Request, Response

from app.config import settings

# --- Анонимный водитель -----------------------------------------------------

DRIVER_COOKIE = "driver_id"
DRIVER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 года

# --- Админка ----------------------------------------------------------------

ADMIN_COOKIE = "admin_session"
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 12  # 12 часов


def driver_token(request: Request) -> tuple[str, Optional[str]]:
    """Возвращает ``(текущий_токен, новый_токен_если_надо_поставить_cookie)``."""
    existing = request.cookies.get(DRIVER_COOKIE)
    if existing and len(existing) <= 64:
        return existing, None
    new_token = uuid.uuid4().hex
    return new_token, new_token


def attach_driver_cookie(response: Response, new_token: Optional[str]) -> None:
    """Ставит cookie водителя, если она ещё не выдана.

    ``secure`` включается только когда сайт реально отдаётся по HTTPS: на
    http://localhost браузер secure-cookie не сохранит, и «Мои заказы»
    перестанут работать у разработчика.
    """
    if not new_token:
        return
    response.set_cookie(
        DRIVER_COOKIE,
        new_token,
        max_age=DRIVER_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_https(),
    )


def client_ip(request: Request) -> str:
    """IP клиента — с учётом обратного прокси, если он доверенный.

    За nginx/Caddy ``request.client.host`` — это всегда 127.0.0.1, и rate-limit
    превращается в один общий лимит на всех посетителей. Реальный адрес лежит
    в ``X-Forwarded-For`` (первый элемент списка).
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            if candidate:
                return candidate
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else "unknown"


# --- Подписанная cookie админки ---------------------------------------------


def _is_https() -> bool:
    return settings.web_host not in {"127.0.0.1", "localhost", "::1"}


def admin_cookie_value(*, now: Optional[int] = None, ttl: int = ADMIN_COOKIE_MAX_AGE) -> str:
    """``<unix_ts>.<hmac>`` — значение cookie админки."""
    expires = int(now if now is not None else time.time()) + ttl
    signature = _sign(str(expires))
    return f"{expires}.{signature}"


def _sign(payload: str) -> str:
    return hmac.new(
        settings.signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def is_valid_admin_cookie(value: Optional[str], *, now: Optional[int] = None) -> bool:
    """Проверяет подпись и срок действия cookie админки."""
    if not value or "." not in value:
        return False
    expires_raw, _, signature = value.partition(".")
    expected = _sign(expires_raw)
    if not hmac.compare_digest(expected, signature):
        return False
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    return expires > int(now if now is not None else time.time())


def is_admin(request: Request) -> bool:
    return settings.admin_enabled and is_valid_admin_cookie(
        request.cookies.get(ADMIN_COOKIE)
    )


def check_admin_password(candidate: str) -> bool:
    """Сверка пароля админки без утечки длины/префикса через время ответа."""
    if not settings.admin_enabled:
        return False
    return hmac.compare_digest(
        settings.admin_password.encode("utf-8"), (candidate or "").encode("utf-8")
    )


def set_admin_cookie(response: Response) -> None:
    response.set_cookie(
        ADMIN_COOKIE,
        admin_cookie_value(),
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_is_https(),
    )


def clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE)


# --- Одноразовые сообщения (flash) ------------------------------------------

FLASH_COOKIE = "flash_notice"


def set_flash(response: Response, text: str) -> None:
    """Кладёт одноразовое сообщение для пользователя.

    Нужно, потому что все действия водителя — это POST с редиректом: показать
    «заказ уже взял другой водитель» негде, кроме как на следующей странице.
    Cookie живёт 60 секунд и вычищается при первом же чтении.

    Значение URL-кодируется обязательно: заголовки HTTP кодируются в latin-1,
    а сообщения русские — без ``quote()`` любой ответ с уведомлением падал бы
    с ``UnicodeEncodeError``.

    Пустой текст удаляет cookie, а не оставляет старое значение: иначе
    уведомление от предыдущего действия «всплывало» бы на следующей странице.
    """
    if not text:
        clear_flash(response)
        return
    response.set_cookie(
        FLASH_COOKIE, quote(text[:300], safe=""), max_age=60, httponly=True,
        samesite="lax", secure=_is_https(),
    )


def pop_flash(request: Request) -> Optional[str]:
    """Достаёт сообщение (и помечает cookie на удаление)."""
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    # unquote безопасен и для незакодированной строки (старые cookie).
    return unquote(raw)


def clear_flash(response: Response) -> None:
    response.delete_cookie(FLASH_COOKIE)
