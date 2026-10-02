"""Зависимости веб-слоя: кто такой «текущий водитель» и «админ».

Водителя различаем анонимной cookie, а после входа через Telegram — подписанной
сессией; админ — это вошедший владелец из ``ADMIN_TELEGRAM_IDS``. Всё это живёт
здесь, чтобы роуты не занимались криптографией и куками самостоятельно.

Подписи сравниваются через :func:`hmac.compare_digest` — обычное ``==``
уязвимо к timing-атаке.
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


# --- Подпись ----------------------------------------------------------------


def _is_https() -> bool:
    return settings.web_host not in {"127.0.0.1", "localhost", "::1"}


def _sign(payload: str) -> str:
    return hmac.new(
        settings.signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def is_admin(request: Request) -> bool:
    """Админ — вошедший через Telegram владелец из ``ADMIN_TELEGRAM_IDS``.

    Id берётся из подписанной cookie входа (её нельзя подделать без секрета),
    а не из чего-либо, что присылает браузер.
    """
    if not settings.admin_enabled:
        return False
    telegram_id = logged_in_telegram_id(request)
    return telegram_id is not None and telegram_id in settings.admin_ids


# --- Вход через Telegram Login Widget ----------------------------------------
#
# driver_id (анонимная cookie выше) НЕ участвует во входе: она не подписана,
# и если бы мы просто клали в неё "tg:<id>" после логина, любой посетитель мог
# бы вручную выставить себе driver_id=tg:<чужой_id> в браузере и открыть чужой
# профиль/статистику или "взять" заказ от чужого имени. Поэтому личность
# вошедшего водителя всегда читается ТОЛЬКО из подписанной driver_session —
# driver_id при этом продолжает жить как раньше, для анонимного листания ленты.

DRIVER_SESSION_COOKIE = "driver_session"
DRIVER_SESSION_MAX_AGE = 60 * 60 * 24 * 365  # год


def driver_session_cookie_value(
    telegram_id: int, *, now: Optional[int] = None, ttl: int = DRIVER_SESSION_MAX_AGE
) -> str:
    """``<telegram_id>.<unix_ts_expires>.<hmac>``."""
    expires = int(now if now is not None else time.time()) + ttl
    payload = f"{telegram_id}.{expires}"
    return f"{payload}.{_sign(payload)}"


def verify_driver_session(value: Optional[str], *, now: Optional[int] = None) -> Optional[int]:
    """Возвращает telegram_id, если подпись верна и срок не истёк, иначе None."""
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    telegram_id_raw, expires_raw, signature = parts
    if not hmac.compare_digest(_sign(f"{telegram_id_raw}.{expires_raw}"), signature):
        return None
    try:
        telegram_id = int(telegram_id_raw)
        expires = int(expires_raw)
    except ValueError:
        return None
    if expires <= int(now if now is not None else time.time()):
        return None
    return telegram_id


def set_driver_session_cookie(response: Response, telegram_id: int) -> None:
    response.set_cookie(
        DRIVER_SESSION_COOKIE,
        driver_session_cookie_value(telegram_id),
        max_age=DRIVER_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_https(),
    )


def clear_driver_session_cookie(response: Response) -> None:
    response.delete_cookie(DRIVER_SESSION_COOKIE)


def logged_in_telegram_id(request: Request) -> Optional[int]:
    """telegram_id вошедшего водителя, или None (не вошёл / вход выключен)."""
    if not settings.telegram_login_enabled:
        return None
    return verify_driver_session(request.cookies.get(DRIVER_SESSION_COOKIE))


def resolve_driver(request: Request) -> tuple[Optional[str], Optional[str], bool]:
    """Identity для страниц, которым нужен "хозяин" (карточка заказа, "Мои заказы").

    Возвращает ``(token, новый_анонимный_токен_если_надо_поставить, нужен_вход)``.

    Пока вход через Telegram не настроен (``TELEGRAM_LOGIN_*`` пусты в .env) —
    поведение точно как раньше: анонимная cookie, вход никогда не требуется.
    Как только настроен — на privileged-страницы без действительной
    driver_session не пускаем вовсе (см. предупреждение выше про подделку).
    """
    if not settings.telegram_login_enabled:
        token, new_token = driver_token(request)
        return token, new_token, False

    telegram_id = logged_in_telegram_id(request)
    if telegram_id is None:
        return None, None, True
    return f"tg:{telegram_id}", None, False


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
