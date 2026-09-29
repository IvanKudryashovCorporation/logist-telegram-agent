"""Проверка данных от Telegram Login Widget.

Алгоритм ровно тот, что описан в официальной документации виджета
(https://core.telegram.org/widgets/login#checking-authorization):
HMAC-SHA256 от отсортированных пар "ключ=значение" (кроме самого hash),
ключ подписи — SHA256 от токена БОТА (не userbot-агента, обычного бота
из @BotFather, привязанного к домену сайта через /setdomain).

Намеренно работает со СТРОКАМИ как есть (то, что реально приходит в
query-параметрах), а не с типизированной моделью: подпись считается по
исходному текстовому представлению, и лишний int()/float() до проверки
хэша тихо сломал бы её на граничных значениях.
"""

import hashlib
import hmac
import time
from typing import Mapping, Optional

#: auth_date — unix-время выдачи виджетом. Старше суток — подозрительно
#: (переиспользование когда-то скопированной ссылки), отклоняем.
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60


def verify_telegram_login(params: Mapping[str, str], bot_token: str, *, now: Optional[int] = None) -> bool:
    """True, если подпись данных виджета верна и они не протухли."""
    if not bot_token:
        return False
    raw_hash = params.get("hash")
    if not raw_hash:
        return False

    check_fields = {key: value for key, value in params.items() if key != "hash"}
    if "id" not in check_fields or "auth_date" not in check_fields:
        return False

    data_check_string = "\n".join(f"{key}={check_fields[key]}" for key in sorted(check_fields))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, raw_hash):
        return False

    try:
        auth_date = int(check_fields["auth_date"])
    except ValueError:
        return False

    current = now if now is not None else int(time.time())
    return current - auth_date <= MAX_AUTH_AGE_SECONDS


def parse_telegram_id(params: Mapping[str, str]) -> Optional[int]:
    """id пользователя из уже ПРОВЕРЕННЫХ данных виджета."""
    try:
        return int(params["id"])
    except (KeyError, ValueError):
        return None
