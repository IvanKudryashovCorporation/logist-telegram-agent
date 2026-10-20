"""Проверка ответа модели: город в заявке должен быть «виден» в самом тексте.

Модель иногда подменяет неизвестное или написанное с опечаткой название другим городом («валнаваха» → «Волгоград»):
заказ получается с чужим маршрутом и расстоянием. Город считается подтверждённым, если в тексте есть слово, похожее
на него, его сокращение из справочника (``симф`` → Симферополь) или начало названия. Названия из аэропортов и вокзалов
(«Внуково» → Москва) в тексте не повторяются, поэтому при таких словах проверка не применяется.
"""

import difflib
import re
from typing import Iterable

from app.city_aliases import CITY_ALIASES, city_key

#: Слова, при которых город может быть выведен из места, а не назван («аэропорт Домодедово» → Москва).
_PLACE_HINT_RE = re.compile(
    r"аэропорт|аэро\b|метро|вокзал|\bжд\b|ж/д|шереметьев|домодедов|внуков|пулков|жуковск|толмач|кольцов|курумоч|платов|"
    r"витязев|гелендж|минвод|мин\.?\s?воды|\bсво\b|граница|кпп|новые\s+территори|новорос",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-zа-яё]{3,}", re.IGNORECASE)
_MIN_RATIO = 0.62


def _alias_keys(city: str) -> set[str]:
    key = city_key(city)
    keys = {key}
    for alias, canonical in CITY_ALIASES.items():
        if city_key(canonical) == key:
            keys.add(city_key(alias))
    return {k for k in keys if k}


def city_is_supported(city: str | None, text: str) -> bool:
    """Есть ли в тексте заявки то, из чего получился этот город."""
    if not city or not city.strip():
        return True
    base = re.sub(r"\s*\(.*$", "", city).strip()  # «Белоглинка (Симферополь)» → «Белоглинка»
    keys = _alias_keys(base) | _alias_keys(city)
    words = [city_key(word) for word in _WORD_RE.findall(text.lower().replace("ё", "е"))]
    if not words:
        return True
    pairs = list(zip(words, words[1:]))
    haystack = [*words, *(f"{a}{b}" for a, b in pairs)]
    for key in keys:
        compact = key.replace(" ", "").replace("-", "")
        if not compact:
            continue
        for token in haystack:
            if len(token) < 3:
                continue
            # сокращение или начало названия: «симф», «ростов» в «Ростов-на-Дону»
            if len(token) >= 3 and (compact.startswith(token) or token.startswith(compact)):
                return True
            # общее начало из четырёх букв: «Волн…»
            if len(token) >= 4 and len(compact) >= 4 and token[:4] == compact[:4]:
                return True
            if difflib.SequenceMatcher(None, token, compact).ratio() >= _MIN_RATIO:
                return True
    return False


def unsupported_cities(cities: Iterable[str | None], text: str) -> list[str]:
    """Города из ответа модели, которых в тексте не видно. Пусто, если в тексте названия мест вместо городов."""
    if _PLACE_HINT_RE.search(text):
        return []
    return [city for city in cities if city and not city_is_supported(city, text)]
