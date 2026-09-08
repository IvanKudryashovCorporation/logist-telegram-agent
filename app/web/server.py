"""Веб-панель логиста (Этап 5): список заказов, карточка, поиск, отчёт за период.

Без авторизации по решению из опроса — доступ ограничивается на уровне
сети/VPS (см. README), а не в самом приложении.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    Driver,
    DriverGroup,
    DriverResponse,
    Order,
    OrderStatus,
    Publication,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Логист — панель заказов")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["status_labels"] = ORDER_STATUS_LABELS


def _order_bucket(order: Order, today: date) -> str:
    if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        return "closed"
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


def _matches_query(order: Order, driver: Driver | None, q: str) -> bool:
    haystack = " ".join(
        str(v)
        for v in [
            order.id,
            order.client_phone,
            order.client_name,
            order.from_city,
            order.to_city,
            order.dispatcher_username,
            driver.name if driver else None,
            driver.phone if driver else None,
            driver.tg_username if driver else None,
            driver.car_plate if driver else None,
        ]
        if v
    ).lower()
    return q.lower() in haystack


async def _drivers_by_id(session, driver_ids: set[int]) -> dict[int, Driver]:
    if not driver_ids:
        return {}
    rows = (await session.execute(select(Driver).where(Driver.id.in_(driver_ids)))).scalars().all()
    return {d.id: d for d in rows}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", status: str = ""):
    async with SessionLocal() as session:
        stmt = select(Order).order_by(Order.pickup_at.is_(None), Order.pickup_at)
        if status:
            try:
                stmt = stmt.where(Order.status == OrderStatus(status))
            except ValueError:
                pass
        orders = (await session.execute(stmt)).scalars().all()

        driver_ids = {o.assigned_driver_id for o in orders if o.assigned_driver_id}
        drivers = await _drivers_by_id(session, driver_ids)

    q = q.strip()
    if q:
        orders = [o for o in orders if _matches_query(o, drivers.get(o.assigned_driver_id), q)]

    today = date.today()
    buckets: dict[str, list[Order]] = {
        "overdue": [],
        "today": [],
        "tomorrow": [],
        "later": [],
        "no_date": [],
        "closed": [],
    }
    for order in orders:
        buckets[_order_bucket(order, today)].append(order)

    return templates.TemplateResponse(
        "orders_list.html",
        {
            "request": request,
            "buckets": buckets,
            "drivers": drivers,
            "q": q,
            "status": status,
            "statuses": list(OrderStatus),
            "bucket_titles": {
                "overdue": "Просрочено",
                "today": "Сегодня",
                "tomorrow": "Завтра",
                "later": "Позже",
                "no_date": "Без даты",
                "closed": "Завершённые/отменённые",
            },
        },
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

        assigned_driver = await session.get(Driver, order.assigned_driver_id) if order.assigned_driver_id else None

        responses = (
            await session.execute(
                select(DriverResponse, Driver)
                .join(Driver, DriverResponse.driver_id == Driver.id)
                .where(DriverResponse.order_id == order_id)
                .order_by(DriverResponse.created_at)
            )
        ).all()

        publications = (
            await session.execute(
                select(Publication, DriverGroup)
                .join(DriverGroup, Publication.group_id == DriverGroup.id)
                .where(Publication.order_id == order_id)
                .order_by(Publication.created_at)
            )
        ).all()

        logs = (
            await session.execute(
                select(ActionLog).where(ActionLog.order_id == order_id).order_by(ActionLog.created_at)
            )
        ).scalars().all()

    return templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "order": order,
            "assigned_driver": assigned_driver,
            "responses": responses,
            "publications": publications,
            "logs": logs,
        },
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports(request: Request, days: int = Query(30, ge=1, le=365)):
    since = datetime.utcnow() - timedelta(days=days)
    async with SessionLocal() as session:
        orders = (
            await session.execute(select(Order).where(Order.created_at >= since))
        ).scalars().all()

    total = len(orders)
    completed = [o for o in orders if o.status == OrderStatus.COMPLETED]
    cancelled = [o for o in orders if o.status == OrderStatus.CANCELLED]

    def _sum(values: list[Decimal | None]) -> Decimal:
        return sum((v for v in values if v is not None), Decimal("0"))

    revenue = _sum([o.client_price for o in completed])
    driver_payouts = _sum([o.driver_payment for o in completed])
    commission = _sum([o.commission for o in completed if o.commission_paid])

    return templates.TemplateResponse(
        "reports.html",
        {
            "request": request,
            "days": days,
            "total": total,
            "completed_count": len(completed),
            "cancelled_count": len(cancelled),
            "revenue": revenue,
            "driver_payouts": driver_payouts,
            "margin": revenue - driver_payouts,
            "commission": commission,
        },
    )
