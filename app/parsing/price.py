"""Запасной разбор цены прямо из текста заявки, без LLM.

Цену из текста достаёт модель, но она недетерминирована: на «30000 водителю» она иногда
отвечала null (в схеме цена — «стоимость для клиента», а тут написано «водителю»), и заказ
уходил на сайт без цены. Здесь — узкие правила на самые частые формулировки диспетчеров;
срабатывают только когда модель цену не вернула, поэтому ложно её не перебивают.
"""

import re
from decimal import Decimal
from typing import Optional

from app.parsing.schema import normalize_price

_AMOUNT = r"(?<![\d+()\-])(\d{1,3}(?:[  ]\d{3})+|\d{1,6})(?:[.,](\d))?"
_CURRENCY = r"(?:₽|руб(?:\.|лей|ля|ль)?|р\.?|т\.?\s?р\.?|тыс(?:\.|яч\w*)?|к|k|т)"
_WHO = r"(?:водител\w*|вод\b|водиле|исполнител\w*|на\s+руки|клиент\w*|с\s+клиента|за\s+рейс|за\s+поездку)"
_LABEL = r"(?:цена|сумма|стоимость|оплата|оплатим|плачу|платим|заплатим|гонорар|тариф|ставка|за\s+рейс|за\s+поездку)"

# Порядок важен: сначала самые однозначные формулировки.
_PATTERNS = [
    # «30000 водителю», «25к вод», «30 000 р на руки»
    re.compile(rf"{_AMOUNT}\s*{_CURRENCY}?\s*{_WHO}", re.IGNORECASE),
    # «цена 30000», «сумма: 25к», «водителю - 30000»
    re.compile(rf"(?:{_LABEL}|{_WHO})\s*[:=\-–—]?\s*{_AMOUNT}", re.IGNORECASE),
    # «30000₽», «30000 руб», «30000р»
    re.compile(rf"{_AMOUNT}\s*(?:₽|руб(?:\.|лей|ля|ль)?|р\b\.?)", re.IGNORECASE),
]


def _to_decimal(whole: str, frac: Optional[str]) -> Decimal:
    digits = re.sub(r"[  ]", "", whole)
    return Decimal(f"{digits}.{frac}") if frac else Decimal(digits)


def extract_price(text: Optional[str]) -> Optional[Decimal]:
    """Цена в рублях из текста заявки или ``None``, если однозначно её найти нельзя."""
    if not text:
        return None
    for pattern in _PATTERNS:
        found = {
            price
            for match in pattern.finditer(text)
            if (price := normalize_price(_to_decimal(match.group(1), match.group(2)))) is not None
            and 1000 <= price <= 300_000
        }
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            return None  # несколько разных сумм — не гадаем (возможно, пачка заявок)
    return None


#: Цену предлагает сам водитель: «запрос цены», «цена по запросу», «предлагайте цену», «договорная».
_PRICE_REQUEST_RE = re.compile(
    r"запрос\s+цен|цен\w*\s+(?:по\s+запросу|договорн|обсужда|свободн|уточн)|по\s+запросу"
    r"|договорн\w*|предлага\w*\s+(?:свою\s+)?(?:цен|стоимост)|предложите\s+(?:свою\s+)?(?:цен|стоимост)"
    r"|(?:назов|озвуч|пиш)\w*\s+(?:свою\s+|вашу\s+)?(?:цен|стоимост)|ваш[ау]\s+(?:цен|стоимост)|ваши\s+цен"
    r"|цен\w*\s+(?:за\s+вами|ваш)|торг\w*|какая\s+цена|сколько\s+(?:возьм|стоит|будет)",
    re.IGNORECASE,
)


def has_price_request(text: Optional[str]) -> bool:
    """В заявке прямо сказано, что цену называет водитель («запрос цены», «договорная»)."""
    return bool(text) and bool(_PRICE_REQUEST_RE.search(text))
