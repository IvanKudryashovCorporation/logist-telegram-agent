"""Что считается повторной публикацией той же заявки.

Единое правило для двух мест: проверка при приёме сообщения
(:mod:`app.telegram.work_group`) и фоновая уборка живых заказов
(:mod:`app.services.dedupe`), которая ловит всё, что проскочило мимо первой.

Заявка B — повтор более ранней заявки A, если совпадают маршрут (с поправкой на
написание города) и время подачи, клиенты не разные, и при этом:

* её опубликовал тот же диспетчер (цена может быть другой — он её меняет), ИЛИ
* текст сообщения тот же самый (диспетчер не определился, кросс-пост в другую группу).
"""

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from app.city_aliases import city_key, expand_city_term

#: Подача у двух повторов расходится не больше чем на это.
TIME_TOLERANCE = timedelta(hours=3)
#: Заявки без времени подачи считаются повтором, если опубликованы в течение этого срока.
ASAP_WINDOW = timedelta(hours=24)
#: Короче этого текст слишком общий, чтобы по совпадению текста считать заявки одной.
_MIN_TEXT_LENGTH = 15
#: Насколько похожи тексты (0..1), чтобы считать их одним сообщением.
_SAME_TEXT_RATIO = 0.9


@dataclass(frozen=True)
class LiveOrder:
    id: Optional[int]
    tg_id: Optional[int]
    username: Optional[str]
    from_city: Optional[str]
    to_city: Optional[str]
    pickup_at: Optional[datetime]
    phone: Optional[str]
    passengers: Optional[int]
    created_at: Optional[datetime]
    raw_text: Optional[str]
    from_address: Optional[str] = None
    to_address: Optional[str] = None


def stem(city: Optional[str]) -> str:
    """Первые шесть букв названия без типа населённого пункта, регистра и знаков.

    «деревня Родионцева (М.О.)» и «Родионцево (Московская обл.)» — одно место, просто
    по-разному склонено и дописано."""
    key = city_key(expand_city_term(city or ""))
    return re.sub(r"[^0-9a-zа-я]+", "", key)[:6]


def same_place(first: Optional[str], second: Optional[str]) -> bool:
    key_a, key_b = city_key(expand_city_term(first or "")), city_key(expand_city_term(second or ""))
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    stem_a, stem_b = stem(first), stem(second)
    return len(stem_a) >= 5 and stem_a == stem_b


def normalized_text(text: Optional[str]) -> str:
    """Текст сообщения без регистра, пробелов и знаков — для сравнения «то же сообщение»."""
    return re.sub(r"[^0-9a-zа-я]+", "", (text or "").lower().replace("ё", "е"))


def clearly_different_clients(first: LiveOrder, second: LiveOrder) -> bool:
    """Один маршрут и время, но разные клиенты (телефон или число пассажиров) — не повтор."""
    if first.phone and second.phone and first.phone != second.phone:
        return True
    return bool(first.passengers and second.passengers and first.passengers != second.passengers)


def _same_addresses_inside_one_city(first: LiveOrder, second: LiveOrder) -> bool:
    """Поездка внутри города («ЖД → аэропорт»): названия городов не отличают её от обратной
    («аэропорт → ЖД»), поэтому адреса обязаны совпасть."""
    if not same_place(first.from_city, first.to_city):
        return True
    for a, b in ((first.from_address, second.from_address), (first.to_address, second.to_address)):
        if a and b and normalized_text(a) != normalized_text(b):
            return False
    return True


def same_dispatcher(first: LiveOrder, second: LiveOrder) -> bool:
    if first.tg_id and first.tg_id == second.tg_id:
        return True
    return bool(first.username and second.username and first.username.lower() == second.username.lower())


def same_time(first: LiveOrder, second: LiveOrder) -> bool:
    if first.pickup_at is None and second.pickup_at is None:
        if not (first.created_at and second.created_at):
            return False
        return abs(second.created_at - first.created_at) <= ASAP_WINDOW
    if first.pickup_at is None or second.pickup_at is None:
        return False
    return abs(second.pickup_at - first.pickup_at) <= TIME_TOLERANCE


def is_repost(older: LiveOrder, newer: LiveOrder) -> bool:
    """``newer`` — повторная публикация ``older``."""
    if not (same_place(older.from_city, newer.from_city) and same_place(older.to_city, newer.to_city)):
        return False
    if not same_time(older, newer) or clearly_different_clients(older, newer):
        return False
    if not _same_addresses_inside_one_city(older, newer):
        return False
    if same_dispatcher(older, newer):
        return True
    text_a, text_b = normalized_text(older.raw_text), normalized_text(newer.raw_text)
    if len(text_a) < _MIN_TEXT_LENGTH or len(text_b) < _MIN_TEXT_LENGTH:
        return False
    if text_a == text_b:
        return True
    # Почти то же сообщение: в одной группе дописали строку («Нет машины»), в другой — нет.
    # Но все числа (цена, время, телефон, расстояние) обязаны совпасть: сообщения, где
    # отличается цена, — разные предложения.
    if sorted(re.findall(r"\d+", older.raw_text or "")) != sorted(re.findall(r"\d+", newer.raw_text or "")):
        return False
    return difflib.SequenceMatcher(None, text_a, text_b).ratio() >= _SAME_TEXT_RATIO


def duplicate_ids(orders: Iterable[LiveOrder]) -> list[int]:
    """id заявок, которые надо отменить как повторы более новых (остаётся самая новая)."""
    ordered = sorted(orders, key=lambda order: order.id or 0)
    cancelled: set[int] = set()
    for index, older in enumerate(ordered):
        for newer in ordered[index + 1:]:
            if is_repost(older, newer):
                cancelled.add(older.id)
                break
    return sorted(cancelled)
