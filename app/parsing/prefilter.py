"""Дешёвый предфильтр сообщений перед отправкой в LLM.

Зачем: в рабочих чатах 70–90% сообщений — переписка («ок», «принял», «кто
свободен»), а не заявки. Каждое из них раньше уходило в LLM: это деньги,
латентность и риск упереться в rate-limit провайдера (из-за которого терялись
уже НАСТОЯЩИЕ заявки).

Фильтр намеренно КОНСЕРВАТИВЕН: сообщение отсекается, только если в нём нет
ни одной цифры, ни одного известного города и ни одного маркера поездки.
Ложное «отсечь настоящую заявку» стоит намного дороже, чем лишний вызов LLM,
поэтому всё сомнительное уходит в модель.

Отключается целиком настройкой ``PREFILTER_ENABLED=false``.
"""

import re
from dataclasses import dataclass
from typing import Final

from app.city_aliases import CITY_ALIASES

#: Короче этого текста заявки не бывает физически («Керчь→Краснодар 8000» = 24).
_MIN_LENGTH: Final[int] = 12

#: Сообщения, которые целиком совпадают с типовой перепиской в чате.
_CHATTER_EXACT: Final[frozenset[str]] = frozenset(
    {
        "ок", "оk", "ok", "okay", "oke", "good", "+", "++", "+++", "да", "нет",
        "принял", "приняла", "принято", "хорошо", "спасибо", "благодарю",
        "добрый день", "доброе утро", "добрый вечер", "здравствуйте", "привет",
        "скинул", "скинула", "отправил", "отправила", "передал", "готов",
        "готова", "на месте", "ждём", "ждем", "жду", "в пути", "выехал",
        "отбой", "минус", "свободен", "свободна", "занят", "не актуально",
        "неактуально", "актуально", "в работе", "сделали", "договорились",
    }
)

#: Признаки того, что текст вообще про перевозку. Совпадение по подстроке.
_ORDER_MARKERS: Final[tuple[str, ...]] = (
    # структура заявки
    "откуда", "куда", "из ", "→", "->", "=>", "маршрут", "направление",
    # время и дата
    "подача", "подачу", "выезд", "встреча", "встретить", "завтра", "сегодня",
    "послезавтра", "утром", "вечером", "ночью", "числа", "время",
    # транспорт и пассажиры
    "такси", "трансфер", "перевозк", "пассажир", "человек", "чел.", "мест",
    "ребенок", "ребёнок", "детск", "кресло", "бустер", "животн", "багаж",
    "чемодан", "комфорт", "бизнес", "эконом", "минивэн", "минивен", "универсал",
    "седан", "хэтчбек", "авто", "машин", "водитель", "свободн",
    # точки
    "аэропорт", "аэро", "вокзал", "жд ", "ржд", "терминал", "отель", "гостиниц",
    "порт", "границ",
    # деньги
    "сумма", "цена", "стоимость", "оплата", "руб", "руб.", "₽", "грн", "тенге",
    "предоплата", "нал", "карта", "перевод",
)

#: Цифры — почти всегда цена или время подачи, то есть заявка.
_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\d")

#: Телефон в любом виде. Наличие телефона — железный признак заявки.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d)|(?:(?:8|7|380|99)[\s.-]?\(?\d{2,5}\)?[\s.-]?\d{2,3}[\s.-]?\d{2}[\s.-]?\d{2})"
)

#: Слова, которые точно не являются названиями городов, но содержат цифры/маркеры.
_LETTERS_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]", re.UNICODE)

#: Все известные написания городов (алиасы + канонические), в нижнем регистре.
_CITY_TOKENS: Final[frozenset[str]] = frozenset(
    {name.lower() for name in CITY_ALIASES} | {canon.lower() for canon in CITY_ALIASES.values()}
)


@dataclass(frozen=True)
class PrefilterDecision:
    """Вердикт предфильтра: отправлять ли текст в LLM и почему."""

    send_to_llm: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - удобство в if-ах
        return self.send_to_llm


def normalize(text: str | None) -> str:
    """Текст к сравнению: нижний регистр, схлопнутые пробелы."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def mentions_known_city(text: str) -> bool:
    """Есть ли в тексте хотя бы одно известное название города."""
    if not text:
        return False
    # Границы слов для кириллицы: \b в Python для str работает по \w, куда
    # входят и кириллические буквы, так что граница определяется корректно.
    for token in _CITY_TOKENS:
        if len(token) < 3:
            # «мв», «мск», «крд», «спб», «рнд» — слишком короткие для substring,
            # ищем только как отдельное слово.
            if re.search(rf"(?:^|[\s,;:()/-]){re.escape(token)}(?:$|[\s,;:.()/-])", text):
                return True
            continue
        if token in text:
            return True
    return False


def prefilter(text: str | None) -> PrefilterDecision:
    """Главная функция: стоит ли тратить вызов LLM на это сообщение."""
    normalized = normalize(text)

    if not normalized:
        return PrefilterDecision(False, "empty")
    if len(normalized) < 3:
        return PrefilterDecision(False, "too_short")
    if normalized.strip(" .,!?:;") in _CHATTER_EXACT:
        return PrefilterDecision(False, "chatter")
    if not _LETTERS_RE.search(normalized):
        return PrefilterDecision(False, "no_letters")
    # Совсем короткое и без единого признака поездки — переписка.
    if len(normalized) < _MIN_LENGTH and not _DIGITS_RE.search(normalized):
        return PrefilterDecision(False, "too_short_no_signal")

    if _PHONE_RE.search(normalized):
        return PrefilterDecision(True, "has_phone")
    if _DIGITS_RE.search(normalized):
        return PrefilterDecision(True, "has_digits")
    if mentions_known_city(normalized):
        return PrefilterDecision(True, "has_city")
    if any(marker in normalized for marker in _ORDER_MARKERS):
        return PrefilterDecision(True, "has_marker")

    return PrefilterDecision(False, "no_signal")

