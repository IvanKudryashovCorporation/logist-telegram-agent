"""Jinja2-окружение сайта.

Единственное место, где регистрируются функции форматирования для шаблонов.
Раньше ``templates.env.globals[...]`` вызывался в пяти разных местах server.py
вперемешку с бизнес-логикой — легко было добавить функцию и забыть её
зарегистрировать, получив падение на рендере.
"""

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.geo import RADIUS_CHOICES
from app.models import ORDER_STATUS_LABELS
from app.web import presenters
from app.web.filters import VEHICLE_CHOICES
from app.web.queries import DEFAULT_DIRECTION, DEFAULT_SORT, DIRECTION_LABELS, SORT_LABELS

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def static_url(name: str) -> str:
    """Адрес файла из /static с версией по времени изменения.

    Без версии браузер продолжает брать закэшированные style.css/app.js после
    выкладки: новая разметка с прежними стилями ломает страницу (раздутые
    иконки, неработающие кнопки).
    """
    try:
        version = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"

#: Функции, доступные во всех шаблонах без передачи через контекст.
TEMPLATE_GLOBALS = {
    "static_url": static_url,
    "status_labels": ORDER_STATUS_LABELS,
    "sort_labels": SORT_LABELS,
    "vehicle_choices": VEHICLE_CHOICES,
    "direction_labels": DIRECTION_LABELS,
    "default_directions": DEFAULT_DIRECTION,
    "radius_choices": RADIUS_CHOICES,
    "default_sort": DEFAULT_SORT,
    "bucket_titles": presenters.BUCKET_TITLES,
    "dispatcher_link": presenters.dispatcher_link,
    "pickup_label": presenters.pickup_label,
    "pickup_subtext": presenters.pickup_subtext,
    "distance_label": presenters.distance_label,
    "price_per_km_label": presenters.price_per_km_label,
    "is_overdue": presenters.is_overdue,
    "relative_ago": presenters.relative_ago,
    "order_tags": presenters.order_tags,
    "order_bucket": presenters.order_bucket,
    "mask_phone": presenters.mask_phone,
    "client_phone_for": presenters.client_phone_for,
    "client_name_for": presenters.client_name_for,
}


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals.update(TEMPLATE_GLOBALS)
    # ensure_ascii=False — города и подписи на русском, иначе в JSON-островке
    # для автодополнения будут \u044d\u043a\u0440\u0430\u043d\u044b.
    templates.env.filters["tojson"] = lambda value: json.dumps(value, ensure_ascii=False)
    templates.env.filters["price"] = lambda value: f"{value:,.0f}".replace(",", " ") if value else ""
    return templates


#: Общий экземпляр — его используют все роуты.
templates = build_templates()
