"""Шаблонный (без LLM) разбор заявок бота «ProPoisk_zakazov» / «PRO все заказы».

Бот публикует заявки всегда в одном формате::

    ✅ Заказ · ⚡ СРОЧНО · сегодня          (или «18.10 в 01:30», «завтра в 10:00»)
    📍 Сочи → Анапа                          (через «→» может быть несколько пунктов)
    👥 1 чел · 💰 8 000 ₽
    🚗 Класс: Стандарт
    👤 Диспетчер: @username

Поэтому такое сообщение разбирается строками и регулярками, а LLM нужна только тому, что под формат не
подошло. Результат — тот же :class:`ParseResult`, что отдаёт LLM: дальше заказ проходит те же проверки,
дедупликацию и геокодирование.

Шаблон намеренно строгий: любая незнакомая строка или поле, которое не удалось прочитать, — это провал
(:class:`TemplateFailure` с понятной причиной). Сообщение тогда уходит в LLM, а владельцу приходит
уведомление, по которому можно доработать шаблон.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from app.parsing.bot_noise import strip_bot_noise
from app.parsing.closed import has_bot_taken_mark
from app.parsing.llm_parser import ParseResult, text_hash
from app.parsing.schema import ParsedOrder
from app.timeutil import now_msk_naive

#: Модель в ``parse_stats`` для заявок, разобранных шаблоном (у LLM там имя модели).
TEMPLATE_MODEL = "template"
#: Префикс ``parse_stats.error`` у сообщений, которые шаблон не разобрал и отдал LLM.
FALLBACK_ERROR_PREFIX = "template_fail"

_ROUTE_SPLIT = re.compile(r"\s*(?:→|->|➡️?|➜|⟶)\s*")
_HEADER_SEPARATORS = re.compile(r"[·•|]")
_HEADER_TOKEN = re.compile(
    r"^(?:(?P<word>сегодня|завтра|послезавтра)|(?P<d>\d{1,2})\.(?P<m>\d{1,2})(?:\.(?P<y>\d{4}|\d{2}))?)?"
    r"\s*(?:в\s*)?(?:(?P<h>\d{1,2}):(?P<mi>\d{2}))?$"
)
_PRICE = re.compile(r"^(\d[\d  \s]*)(?:[.,]\d+)?\s*(?:₽|руб\w*\.?|р\.?)?$")
_PASSENGERS = re.compile(r"^(\d{1,2})\s*(?:чел\w*|пасс\w*)?\.?$", re.IGNORECASE)
_USERNAME = re.compile(r"@([A-Za-z0-9_]{4,32})")
_CAR_CLASS = re.compile(r"^класс\s*:\s*(.+)$", re.IGNORECASE)
#: «Обычный» класс: схема заявки хранит car_class только для нестандартных машин.
_STANDARD_CLASSES = {"стандарт", "эконом", "стандартный", "обычный", "любой", "econom", "standard"}
#: Строки, которые бот добавляет от себя и которые для заявки ничего не значат.
_IGNORED_LINE = re.compile(r"^(?:здравствуйте|сообщение диспетчеру|группа закрытая|ℹ)", re.IGNORECASE)
_EMOJI_PREFIX = re.compile(r"^[^\w@+]+")
_MAX_POINT_LEN = 60


class TemplateFailure(Exception):
    """Сообщение не подошло под формат бота; ``reason`` — что именно (идёт владельцу в уведомление)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class TemplateOutcome:
    """Итог попытки шаблона.

    ``result`` — готовый результат (для «Заказ снят/взят» — пустой список заявок).
    ``reason`` — причина провала; ``bot_format`` — похоже ли сообщение на сообщение бота вообще
    (обычная болтовня в группе шаблоном не разбирается, но и тревоги не поднимает)."""

    result: Optional[ParseResult] = None
    reason: Optional[str] = None
    bot_format: bool = True


def looks_like_bot_message(text: str) -> bool:
    """Есть ли в тексте признаки сообщения бота: «Заказ» в заголовке и строка маршрута со стрелкой."""
    head = text.lstrip()[:60].lower()
    return ("заказ" in head) or ("📍" in text) or ("диспетчер:" in text.lower() and "→" in text)


def _header_date(word: Optional[str], d: Optional[str], m: Optional[str], y: Optional[str], today: date) -> date:
    if word:
        return today + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[word])
    day, month = int(d), int(m)
    if y:
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            return date(year, month, day)
        except ValueError:
            raise TemplateFailure(f"некорректная дата «{d}.{m}.{y}»") from None
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        raise TemplateFailure(f"некорректная дата «{d}.{m}»") from None
    # Дата без года: если она заметно позади — это следующий год (заявка в январе, а сейчас декабрь).
    if candidate < today - timedelta(days=30):
        candidate = date(today.year + 1, month, day)
    return candidate


def _parse_header(line: str, order: ParsedOrder, today: date) -> None:
    """«✅ Заказ · ⚡ СРОЧНО · сегодня в 18:00»: срочность, день и время подачи."""
    cleaned = _HEADER_SEPARATORS.sub("|", line)
    cleaned = re.sub(r"[^\w\s:.|/-]", " ", cleaned).lower()
    if "срочно" in cleaned:
        order.is_urgent = True
        cleaned = cleaned.replace("срочно", " ")
    cleaned = re.sub(r"\bзаказ\b", " ", cleaned, count=1)
    for token in cleaned.split("|"):
        token = " ".join(token.split())
        if not token:
            continue
        match = _HEADER_TOKEN.match(token)
        if match is None or not any(match.groupdict().values()):
            raise TemplateFailure(f"незнакомая часть заголовка: «{token}»")
        if match["word"] or match["d"]:
            order.pickup_date = _header_date(
                match["word"], match["d"], match["m"], match["y"], today
            ).isoformat()
        if match["h"]:
            hour, minute = int(match["h"]), int(match["mi"])
            if hour > 23 or minute > 59:
                raise TemplateFailure(f"некорректное время «{token}»")
            order.pickup_time = f"{hour:02d}:{minute:02d}"


def _point(raw: str) -> tuple[str, Optional[str]]:
    """Пункт маршрута -> (город, адрес). «Сочи, ул. Ленина 5» -> («Сочи», «ул. Ленина 5»)."""
    raw = raw.strip(" \t.;")
    city, _, address = raw.partition(",")
    city, address = city.strip(), address.strip()
    if not re.search(r"[^\W\d_]", city) or len(city) > _MAX_POINT_LEN:
        raise TemplateFailure(f"пункт маршрута не похож на город: «{raw[:80]}»")
    return city, address or None


def _parse_route(value: str, order: ParsedOrder) -> None:
    points = [p for p in _ROUTE_SPLIT.split(value.strip()) if p.strip()]
    if len(points) < 2:
        raise TemplateFailure(f"в маршруте меньше двух пунктов: «{value[:80]}»")
    parsed = [_point(p) for p in points]
    order.from_city, order.from_address = parsed[0]
    order.to_city, order.to_address = parsed[-1]
    order.via_points = [city for city, _ in parsed[1:-1]]


def _parse_price(value: str, order: ParsedOrder) -> None:
    match = _PRICE.match(value.strip().lower())
    if match is None:
        raise TemplateFailure(f"цена не число: «{value[:40]}»")
    digits = re.sub(r"\D", "", match.group(1))
    if not digits:
        raise TemplateFailure(f"цена не число: «{value[:40]}»")
    order.client_price = Decimal(digits)


def _parse_segment(segment: str, order: ParsedOrder, seen: set[str]) -> None:
    """Одна часть строки (строки делятся на части по «·»): по эмодзи или подписи понимаем, что это за поле."""
    body = _EMOJI_PREFIX.sub("", segment).strip()
    low = body.lower()
    if not body:
        return
    if "📍" in segment or "→" in body or "->" in body:
        _parse_route(body, order)
        seen.add("route")
    elif "💰" in segment:
        _parse_price(body, order)
        seen.add("price")
    elif "👥" in segment or re.search(r"\bчел\b", low):
        match = _PASSENGERS.match(low)
        if match is None:
            raise TemplateFailure(f"не удалось прочитать число пассажиров: «{body[:40]}»")
        order.passengers = int(match.group(1)) or None
    elif "🚗" in segment or low.startswith("класс"):
        match = _CAR_CLASS.match(body)
        if match is None:
            raise TemplateFailure(f"не удалось прочитать класс авто: «{body[:40]}»")
        car_class = match.group(1).strip()
        order.car_class = None if car_class.lower() in _STANDARD_CLASSES else car_class
    elif "🕒" in segment or low.startswith("опубликован"):
        return  # время публикации заявки не касается
    elif "👤" in segment or low.startswith("диспетчер"):
        if _USERNAME.search(body):
            seen.add("contact")
    elif "📞" in segment or low.startswith("телефон"):
        return  # номер диспетчера, не клиента; заказ ищет диспетчера по @username
    elif _IGNORED_LINE.match(low):
        return
    else:
        raise TemplateFailure(f"незнакомая строка: «{body[:60]}»")


def parse_pro_message(text: str, *, today: Optional[date] = None) -> TemplateOutcome:
    """Разбирает сообщение бота. Никогда не бросает: провал — в ``reason`` результата."""
    cleaned = strip_bot_noise((text or "").strip())
    if not cleaned:
        return TemplateOutcome(reason="пустое сообщение", bot_format=False)
    if not looks_like_bot_message(cleaned):
        return TemplateOutcome(reason="не сообщение бота (нет заголовка «Заказ» и строки 📍)", bot_format=False)
    try:
        return TemplateOutcome(result=_parse(cleaned, today or now_msk_naive().date()))
    except TemplateFailure as failure:
        return TemplateOutcome(reason=failure.reason)


def _parse(cleaned: str, today: date) -> ParseResult:
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    header = lines[0]
    if "заказ" not in header.lower():
        raise TemplateFailure(f"первая строка не заголовок «Заказ»: «{header[:60]}»")
    if has_bot_taken_mark(cleaned):
        # «⛔️ Заказ снят/взят»: заявки здесь нет. Снятие заказа делает вызывающий код по замку.
        return ParseResult(orders=[], model=TEMPLATE_MODEL, attempts=0, latency_ms=0, text_hash=text_hash(cleaned))

    order = ParsedOrder(raw_snippet=None)
    seen: set[str] = set()
    _parse_header(header, order, today)
    for line in lines[1:]:
        for segment in line.split("·"):
            _parse_segment(segment, order, seen)
    if "route" not in seen:
        raise TemplateFailure("нет строки маршрута «📍 Откуда → Куда»")
    if "price" not in seen:
        raise TemplateFailure("нет цены «💰 …»")
    return ParseResult(
        orders=[order], model=TEMPLATE_MODEL, attempts=0, latency_ms=0, text_hash=text_hash(cleaned)
    )
