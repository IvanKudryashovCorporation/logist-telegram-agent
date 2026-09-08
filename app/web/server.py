"""Веб-панель логиста (Этап 5): список заказов, карточка, поиск, отчёт за период.

Без авторизации по решению из опроса — доступ ограничивается на уровне
сети/VPS (см. README), а не в самом приложении.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    ActorType,
    Driver,
    DriverGroup,
    DriverResponse,
    Order,
    OrderStatus,
    PendingAction,
    PendingActionStatus,
    PendingActionType,
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

        pending_actions = (
            await session.execute(
                select(PendingAction)
                .where(PendingAction.order_id == order_id)
                .order_by(PendingAction.created_at.desc())
                .limit(10)
            )
        ).scalars().all()

    has_pending = any(a.status == PendingActionStatus.PENDING for a in pending_actions)
    has_active_publications = any(pub.deleted_at is None for pub, _group in publications)

    return templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "order": order,
            "assigned_driver": assigned_driver,
            "responses": responses,
            "publications": publications,
            "logs": logs,
            "statuses": list(OrderStatus),
            "pending_actions": pending_actions,
            "has_pending": has_pending,
            "has_active_publications": has_active_publications,
        },
    )


async def _queue_action(order_id: int, action: PendingActionType, response_id: int | None = None) -> None:
    """Ставит задачу в очередь для агента. Не дублирует, если такая же уже висит необработанной."""
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(PendingAction).where(
                    PendingAction.order_id == order_id,
                    PendingAction.action == action,
                    PendingAction.status == PendingActionStatus.PENDING,
                    PendingAction.response_id == response_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(PendingAction(order_id=order_id, action=action, response_id=response_id))
        await session.commit()


@app.post("/orders/{order_id}/publish")
async def queue_publish(order_id: int):
    await _queue_action(order_id, PendingActionType.PUBLISH)
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/assign")
async def queue_assign(order_id: int, response_id: int = Form(...)):
    await _queue_action(order_id, PendingActionType.ASSIGN, response_id)
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/complete")
async def queue_complete(order_id: int):
    await _queue_action(order_id, PendingActionType.COMPLETE)
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/cancel")
async def queue_cancel(order_id: int):
    await _queue_action(order_id, PendingActionType.CANCEL)
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/confirm-payment")
async def queue_confirm_payment(order_id: int):
    await _queue_action(order_id, PendingActionType.CONFIRM_PAYMENT)
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/update")
async def update_order(request: Request, order_id: int):
    form = await request.form()

    def _s(name: str) -> str | None:
        value = form.get(name)
        value = value.strip() if isinstance(value, str) else value
        return value or None

    def _dec(name: str, current: Decimal | None) -> Decimal | None:
        """Пустое поле — явная очистка. Нечисловой ввод — не трогаем старое значение,
        а не молча обнуляем его (ошибка ввода не должна стирать данные заказа)."""
        value = _s(name)
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except Exception:
            return current
        return parsed if parsed.is_finite() else current

    def _int(name: str, current: int | None) -> int | None:
        value = _s(name)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return current

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

        status_value = _s("status")
        if status_value:
            try:
                order.status = OrderStatus(status_value)
            except ValueError:
                pass

        pickup_raw = _s("pickup_at")
        if pickup_raw:
            try:
                order.pickup_at = datetime.fromisoformat(pickup_raw)
            except ValueError:
                pass
        else:
            order.pickup_at = None

        order.from_city = _s("from_city")
        order.to_city = _s("to_city")
        order.from_address = _s("from_address")
        order.to_address = _s("to_address")
        # Рейс/поезд убрали из формы веб-панели (не показываем) — не трогаем поле,
        # чтобы значение из Telegram-заявки не затиралось сохранением формы.
        order.car_class = _s("car_class")
        order.passengers = _int("passengers", order.passengers)
        order.luggage = _s("luggage")
        order.has_pets = form.get("has_pets") is not None
        order.needs_child_seat = form.get("needs_child_seat") is not None
        order.client_name = _s("client_name")
        order.client_phone = _s("client_phone")
        order.client_price = _dec("client_price", order.client_price)
        order.driver_payment = _dec("driver_payment", order.driver_payment)
        order.commission = _dec("commission", order.commission)
        order.commission_paid = form.get("commission_paid") is not None
        order.has_problem = form.get("has_problem") is not None
        order.problem_note = _s("problem_note")
        order.is_urgent = form.get("is_urgent") is not None

        session.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.LOGIST,
                action="web_edit",
                details="изменено через веб-панель",
            )
        )
        await session.commit()

    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


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
