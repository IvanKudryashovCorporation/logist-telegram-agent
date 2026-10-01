"""Форматирование данных заказа для шаблонов.

Всё, что превращает строку БД в человекочитаемый текст, живёт здесь — раньше
это было перемешано с роутами в server.py, из-за чего форматирование нельзя
было ни протестировать, ни переиспользовать.

Про время: ``pickup_at`` — «гражданское» время по МСК, а ``created_at``/
``taken_at`` — UTC. Здесь это различие учтено явно (см. app/timeutil.py),
раньше обратный отсчёт считался от ``datetime.utcnow() + 3h`` в одном месте
и от ``datetime.utcnow()`` в другом, и согласованность держалась на честном слове.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from app.config import settings
from app.models import Order
from app.timeutil import now_msk_naive, now_utc_naive

BUCKET_TITLES = {
    "today": "Сегодня",
    "tomorrow": "Завтра",
    "later": "Позже",
    "no_date": "Без даты",
}


def order_bucket(order: Order, today: Optional[date] = None) -> str:
    """В какую секцию списка попадает заказ по дате подачи."""
    if order.pickup_at is None:
        return "no_date"
    today = today or now_msk_naive().date()
    pickup_date = order.pickup_at.date()
    # Просроченные (дата в прошлом) группируем вместе с сегодняшними —
    # отдельной секции для них нет, а карточка красится через is_overdue().
    if pickup_date <= today:
        return "today"
    if pickup_date == today + timedelta(days=1):
        return "tomorrow"
    return "later"


def dispatcher_message(order: Order) -> str:
    """Готовое первое сообщение диспетчеру: «Здравствуйте! Заказ A → B, 05.10 в
    14:00, 3000 ₽ — актуально?». Отсутствующие части просто пропускаются.

    Телефон и имя клиента сюда не попадают принципиально: текст уходит в чат
    и подставляется в ссылку, а контакты клиента скрыты от не взявших заказ.
    """
    origin = order.from_city or order.from_address
    destination = order.to_city or order.to_address
    fallback = origin or destination or f"№{order.id}"
    route = f"{origin} → {destination}" if origin and destination else fallback

    details: list[str] = []
    if order.pickup_at is not None:
        details.append(order.pickup_at.strftime("%d.%m в %H:%M"))
    else:
        details.append("в ближайшее время")
    if order.client_price is not None:
        details.append(f"{order.client_price:.0f} ₽")

    summary = ", ".join([route, *details])
    return f"Здравствуйте! Заказ {summary} — актуально?"


def dispatcher_link(order: Order, text: Optional[str] = None) -> Optional[str]:
    """Ссылка на диалог с диспетчером в Telegram, если известен его аккаунт.

    ``contact_username`` — явное «писать @...» из текста заявки — приоритетнее
    отправителя сообщения: часто заявку публикует не тот, кому по ней
    фактически нужно писать (пересылка, бот группы и т.п.).

    ``https://t.me/<username>`` — предпочтительно: работает в любом браузере,
    как в приложении, так и без него (откроет web.telegram.org). ``tg://user?id=``
    оставлен запасным для диспетчеров без публичного username — многие
    мобильные браузеры блокируют этот «сырой» протокол при переходе с сайта,
    поэтому им пользуемся только когда другого варианта нет.

    ``text`` подставляется в поле ввода чата (``?text=``). У ссылки по
    числовому id такого параметра нет — для неё текст не добавляется.
    """
    username = order.contact_username or order.dispatcher_username
    if username:
        url = f"https://t.me/{username}"
        return f"{url}?text={quote(text, safe='')}" if text else url
    if order.dispatcher_tg_id:
        return f"tg://user?id={order.dispatcher_tg_id}"
    return None


def duration_str(minutes: int) -> str:
    """«45 мин» / «3 ч 20 мин» / «2 дн»."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} мин"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч" + (f" {rem} мин" if rem else "")
    return f"{hours // 24} дн"


def is_overdue(order: Order, *, now: Optional[datetime] = None) -> bool:
    """Подача уже прошла по времени — красим карточку независимо от бакета."""
    if order.pickup_at is None:
        return False
    return order.pickup_at < (now or now_msk_naive())


def pickup_label(order: Order, *, now: Optional[datetime] = None) -> str:
    """Подача для карточки заказа: «сегодня в 15:40», «завтра в 00:30»,
    «в ближайшее время» (когда время определить не удалось)."""
    if order.pickup_at is None:
        return "в ближайшее время"
    today = (now or now_msk_naive()).date()
    pickup_date = order.pickup_at.date()
    clock = order.pickup_at.strftime("%H:%M")
    if pickup_date == today:
        return f"сегодня в {clock}"
    if pickup_date == today + timedelta(days=1):
        return f"завтра в {clock}"
    return f"{clock}, {order.pickup_at.strftime('%d.%m')}"


def pickup_subtext(order: Order, *, now: Optional[datetime] = None) -> str:
    """Короткая подпись под временем подачи — как давно/скоро подача."""
    if order.pickup_at is None:
        return ""  # главная надпись уже говорит «в ближайшее время»
    now = now or now_msk_naive()
    bucket = order_bucket(order, now.date())
    if bucket == "tomorrow":
        return "завтра"
    if bucket == "later":
        return order.pickup_at.strftime("%d.%m")
    delta_min = int((order.pickup_at - now).total_seconds() // 60)
    if delta_min >= 0:
        return f"через {duration_str(delta_min)}"
    return f"просрочено на {duration_str(-delta_min)}"


def relative_ago(moment: Optional[datetime]) -> str:
    """«N мин назад» — для служебных отметок в UTC (created_at, taken_at)."""
    if moment is None:
        return ""
    if moment.tzinfo is not None:
        # Aware-значение приводим к наивному UTC, чтобы не сравнивать несравнимое.
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    minutes = max(0, int((now_utc_naive() - moment).total_seconds() // 60))
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours, _ = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def order_tags(order: Order) -> list[tuple[str, bool]]:
    """Короткие теги для карточки: (текст, основной_ли/тёмный)."""
    tags: list[tuple[str, bool]] = []
    if order.is_urgent:
        tags.append(("Срочно", True))
    if order.passengers:
        tags.append((f"{order.passengers} пасс.", True))
    if order.car_class:
        tags.append((order.car_class, False))
    if order.flight_or_train:
        tags.append(("Рейс", False))
    if order.needs_child_seat:
        tags.append(("Кресло", False))
    if order.has_pets:
        tags.append(("Животное", False))
    if order.has_problem:
        tags.append(("Есть жалоба", False))
    return tags


def mask_phone(phone: Optional[str]) -> str:
    """Скрывает середину номера, оставляя последние две цифры.

    Сайт публичный и без регистрации: открытые телефоны клиентов собирают
    парсерами за минуты, а это персональные данные. Полный номер видит только
    водитель, взявший заказ, — ему он и нужен, чтобы договориться.
    """
    if not phone:
        return ""
    digits = [ch for ch in phone if ch.isdigit()]
    if len(digits) <= 2:
        return "скрыт"
    return f"***-***-**{''.join(digits[-2:])}"


def client_phone_for(order: Order, *, is_owner: bool) -> str:
    """Телефон клиента: полный — тому, кто взял заказ, иначе маскированный."""
    if not order.client_phone:
        return ""
    if is_owner or not settings.mask_client_contacts:
        return order.client_phone
    return mask_phone(order.client_phone)


def client_name_for(order: Order, *, is_owner: bool) -> str:
    """Имя клиента: только инициал, если заказ не взят."""
    if not order.client_name:
        return ""
    if is_owner or not settings.mask_client_contacts:
        return order.client_name
    stripped = order.client_name.strip()
    first = stripped.split()[0] if stripped else ""
    return f"{first[:1]}." if first else ""


#: Похожее на телефон место в тексте: цифры с пробелами, скобками, точками и
#: дефисами. Цену и время тоже захватывает — их отсеиваем по числу цифр.
_PHONE_LIKE_RE = re.compile(r"\+?\d[\d\s().-]{4,}\d")

#: Минимум цифр, чтобы последовательность считалась номером, а не ценой.
_PHONE_MIN_DIGITS = 10


def redact_raw_text(order: Order, *, is_owner: bool) -> str:
    """Исходное сообщение диспетчера с вымаранными контактами клиента.

    Карточка заказа показывает оригинал заявки — это полезно: водитель видит,
    что именно написал диспетчер. Но вместе с телефоном и именем это полностью
    обнуляло маскирование: любой посетитель сайта читал персональные данные
    в «сыром» тексте. Поэтому посторонним оригинал отдаётся вымаранным,
    а водителю, взявшему заказ, — как есть.
    """
    text = order.raw_text or ""
    if is_owner or not settings.mask_client_contacts or not text:
        return text

    def _mask(match: re.Match) -> str:
        digits = [char for char in match.group(0) if char.isdigit()]
        if len(digits) < _PHONE_MIN_DIGITS:
            # Цена, дата, время — их водитель должен видеть.
            return match.group(0)
        return mask_phone(match.group(0))

    text = _PHONE_LIKE_RE.sub(_mask, text)

    if order.client_name:
        initial = client_name_for(order, is_owner=False).rstrip(".")
        full_name = order.client_name.strip()
        if initial:
            # Сначала имя целиком, потом отдельные слова (диспетчер мог
            # написать их в другом порядке или только фамилию).
            text = re.sub(re.escape(full_name), initial, text, flags=re.IGNORECASE)
            for part in sorted(
                (word for word in full_name.split() if len(word) >= 3), key=len, reverse=True
            ):
                text = re.sub(rf"\b{re.escape(part)}\b", initial, text, flags=re.IGNORECASE)
    return text


#: Разделители, которые диспетчеры ставят внутри номера.
_PHONE_SEPARATORS = r"[\s().\-]*"


def _phone_pattern(phone: str) -> Optional[re.Pattern[str]]:
    """Регэксп номера клиента, допускающий любые разделители между цифрами.

    В исходном сообщении и в разобранном поле номер часто записан по-разному
    («+7 999 000-00-00» против «79990000000»), поэтому ищем цифры, а не строку.
    """
    digits = [char for char in phone if char.isdigit()]
    if len(digits) < 5:
        return None
    body = _PHONE_SEPARATORS.join(re.escape(digit) for digit in digits)
    return re.compile(r"\+?" + body)


def raw_text_for(order: Order, *, is_owner: bool) -> str:
    """Исходное сообщение диспетчера с вымаранными контактами клиента.

    Карточка показывает оригинал заявки — по нему водитель понимает контекст
    (что именно написал диспетчер). Но вместе с телефоном и именем это сводило
    на нет маскирование: любой посетитель сайта видел полные персональные
    данные прямо в тексте. Поэтому посторонним оригинал отдаётся с заменой
    номера на маску и имени — на инициал.
    """
    text = order.raw_text or ""
    if is_owner or not settings.mask_client_contacts:
        return text

    if order.client_phone:
        pattern = _phone_pattern(order.client_phone)
        if pattern is not None:
            text = pattern.sub(mask_phone(order.client_phone), text)

    if order.client_name:
        initial = client_name_for(order, is_owner=False).rstrip(".")
        if initial:
            name = order.client_name.strip()
            # Сначала полное имя, затем первое слово: иначе после замены
            # «Иван Петров» → «И.» в тексте остался бы «Петров».
            for variant in dict.fromkeys([name, name.split()[0]]):
                if len(variant) >= 2:
                    text = re.sub(
                        rf"\b{re.escape(variant)}\b", initial, text, flags=re.IGNORECASE
                    )
    return text
