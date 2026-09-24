"""Публичная витрина заказов для водителей: список + карточка с кнопкой
"Написать диспетчеру". Без авторизации — любой по ссылке.

Цены показываются 1 в 1 из заявки, без наценки. Никакого редактирования
или бизнес-флоу — сайт только читает то, что разобрал агент.
"""

from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import ORDER_STATUS_LABELS, Order, OrderStatus

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Заказы для водителей")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["status_labels"] = ORDER_STATUS_LABELS


def _order_bucket(order: Order, today: date) -> str:
    if order.pickup_at is None:
        return "no_date"
    pickup_date = order.pickup_at.date()
    if pickup_date < today:
        return "overdue"
    if pickup_date == today:
        return "today"
    if pickup_date == today + timedelta(days=1):
        return "tomorrow"
    return "later"


def _matches_query(order: Order, q: str) -> bool:
    haystack = " ".join(
        str(v)
        for v in [
            order.id,
            order.client_phone,
            order.client_name,
            order.from_city,
            order.to_city,
            order.from_address,
            order.to_address,
            order.dispatcher_username,
        ]
        if v
    ).lower()
    return q.lower() in haystack


def dispatcher_link(order: Order) -> str | None:
    """Ссылка на диалог с диспетчером в Telegram, если известен его аккаунт."""
    if order.dispatcher_tg_id:
        return f"tg://user?id={order.dispatcher_tg_id}"
    if order.dispatcher_username:
        return f"https://t.me/{order.dispatcher_username}"
    return None


templates.env.globals["dispatcher_link"] = dispatcher_link


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = ""):
    async with SessionLocal() as session:
        stmt = (
            select(Order)
            .where(Order.status != OrderStatus.CANCELLED)
            .order_by(Order.pickup_at.is_(None), Order.pickup_at)
        )
        orders = (await session.execute(stmt)).scalars().all()

    q = q.strip()
    if q:
        orders = [o for o in orders if _matches_query(o, q)]

    today = date.today()
    buckets: dict[str, list[Order]] = {
        "overdue": [],
        "today": [],
        "tomorrow": [],
        "later": [],
        "no_date": [],
    }
    for order in orders:
        buckets[_order_bucket(order, today)].append(order)

    return templates.TemplateResponse(
        "orders_list.html",
        {
            "request": request,
            "buckets": buckets,
            "q": q,
            "bucket_titles": {
                "overdue": "Просрочено",
                "today": "Сегодня",
                "tomorrow": "Завтра",
                "later": "Позже",
                "no_date": "Без даты",
            },
        },
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

    return templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "order": order,
        },
    )
