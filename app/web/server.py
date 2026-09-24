"""Публичная витрина заказов для водителей: общий список, "Мои заказы" и
карточка с кнопками "Написать диспетчеру" / "Взять заказ". Без авторизации.

Водителя различаем анонимной cookie в браузере (никаких паролей/логинов) —
кто первым нажал "Взять заказ", тот и забрал его себе в "Мои заказы", и
заказ пропадает из общего списка.

Цены показываются 1 в 1 из заявки, без наценки.
"""

import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import ORDER_STATUS_LABELS, Order, OrderStatus

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Заказы для водителей")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["status_labels"] = ORDER_STATUS_LABELS

DRIVER_COOKIE = "driver_id"
DRIVER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 года


def _driver_token(request: Request) -> tuple[str, str | None]:
    """Возвращает (текущий токен, новый_токен_если_надо_поставить_cookie)."""
    existing = request.cookies.get(DRIVER_COOKIE)
    if existing:
        return existing, None
    new_token = uuid.uuid4().hex
    return new_token, new_token


def _set_driver_cookie(response: Response, new_token: str | None) -> None:
    if new_token:
        response.set_cookie(
            DRIVER_COOKIE, new_token, max_age=DRIVER_COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )


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
    """Ссылка на диалог с диспетчером в Telegram, если известен его аккаунт.

    https://t.me/<username> — предпочтительно: работает в любом браузере,
    как в приложении, так и без него (откроет web.telegram.org). tg://user?id=
    оставлен запасным для диспетчеров без публичного username — многие
    мобильные браузеры блокируют этот "сырой" протокол при переходе с сайта,
    поэтому им пользуемся только когда другого варианта нет.
    """
    if order.dispatcher_username:
        return f"https://t.me/{order.dispatcher_username}"
    if order.dispatcher_tg_id:
        return f"tg://user?id={order.dispatcher_tg_id}"
    return None


templates.env.globals["dispatcher_link"] = dispatcher_link


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = ""):
    _token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        stmt = (
            select(Order)
            .where(Order.status != OrderStatus.CANCELLED, Order.taken_by_token.is_(None))
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

    html = templates.TemplateResponse(
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
    _set_driver_cookie(html, new_token)
    return html


@app.get("/my", response_class=HTMLResponse)
async def my_orders(request: Request):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        stmt = select(Order).where(Order.taken_by_token == token).order_by(Order.taken_at.desc())
        orders = (await session.execute(stmt)).scalars().all()

    html = templates.TemplateResponse("my_orders.html", {"request": request, "orders": orders})
    _set_driver_cookie(html, new_token)
    return html


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

    html = templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "order": order,
            "is_mine": order.taken_by_token == token,
            "is_taken_by_someone_else": bool(order.taken_by_token) and order.taken_by_token != token,
        },
    )
    _set_driver_cookie(html, new_token)
    return html


@app.post("/orders/{order_id}/take")
async def take_order(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is not None and order.taken_by_token is None:
            # Кто первый нажал — тот и взял; SQLite обрабатывает запросы
            # последовательно, гонки между двумя водителями тут не будет.
            order.taken_by_token = token
            order.taken_at = datetime.utcnow()
            await session.commit()

    redirect = RedirectResponse(url=f"/orders/{order_id}", status_code=303)
    _set_driver_cookie(redirect, new_token)
    return redirect


@app.post("/orders/{order_id}/release")
async def release_order(request: Request, order_id: int):
    token, new_token = _driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is not None and order.taken_by_token == token:
            order.taken_by_token = None
            order.taken_at = None
            await session.commit()

    redirect = RedirectResponse(url="/my", status_code=303)
    _set_driver_cookie(redirect, new_token)
    return redirect
