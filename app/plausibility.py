"""Здравый смысл для цены за километр: подсказка, что место найдено не там.

Цена в заявке и расстояние независимы, но вместе они выдают ошибку геокодера: 30 800 ₽
за 26 км или 4 000 ₽ за 800 км так не бывает. Если у названий есть другие варианты
(одноимённые населённые пункты), берём пару, при которой цена за километр обычная.
"""

import math
from typing import Optional

from app.geo import Candidate, haversine_km

#: Вне этого коридора цена за км считается подозрительной, ₽/км.
MIN_RATE = 12.0
MAX_RATE = 200.0
#: Альтернативная пара принимается, только если цена за км в этом коридоре.
ALT_MIN_RATE = 18.0
ALT_MAX_RATE = 120.0
#: Обычная цена за километр — к ней тянется выбор среди подходящих пар.
TYPICAL_RATE = 45.0
#: Дорога длиннее прямой в среднем на четверть.
_DETOUR = 1.25


def is_plausible(rate: float) -> bool:
    return MIN_RATE <= rate <= MAX_RATE


def choose_pair(
    price: float, from_candidates: list[Candidate], to_candidates: list[Candidate]
) -> Optional[tuple[Candidate, Candidate]]:
    """Пара кандидатов с самой правдоподобной ценой за км, ``None`` — выбирать не из чего."""
    if len(from_candidates) * len(to_candidates) < 2:
        return None
    best: Optional[tuple[Candidate, Candidate]] = None
    best_score = math.inf
    for origin in from_candidates:
        for destination in to_candidates:
            km = haversine_km(origin.coords, destination.coords) * _DETOUR
            if km < 3:
                continue
            rate = price / km
            if not ALT_MIN_RATE <= rate <= ALT_MAX_RATE:
                continue
            score = abs(math.log(rate / TYPICAL_RATE))
            if score < best_score:
                best, best_score = (origin, destination), score
    return best
