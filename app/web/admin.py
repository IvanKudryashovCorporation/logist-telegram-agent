"""Админка владельца: состояние ленты, очередь разбора, качество парсинга.

Раньше единственный способ что-то узнать о работе системы — читать лог агента
вручную. Здесь собрано то, что нужно каждый день:

* сколько заказов в каждом статусе и сколько помечено водителями как проблемные;
* очередь повторного разбора (сообщения, где LLM отказала) и кнопка «повторить»;
* качество разбора: доля неполных заявок, расход токенов, латентность,
  насколько предфильтр экономит обращения к LLM;
* ручное скрытие/возврат заявки в ленту.

Доступ — только вошедшему через Telegram владельцу (``ADMIN_TELEGRAM_IDS``).
Всем остальным, включая гостей, админка отвечает 404 на все маршруты: страницы
входа у неё нет, так что снаружи не видно даже того, что она существует.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, update

from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    ActorType,
    Order,
    OrderStatus,
    PendingMessage,
    PendingStatus,
)
from app.parsing.llm_parser import cache_stats
from app.services import reporting
from app.timeutil import now_utc_naive
from app.web.deps import is_admin
from app.web.rate_limit import limiter_stats
from app.web.templates_env import templates

log = logging.getLogger("web.admin")

router = APIRouter(prefix="/admin")

#: Сколько строк показывать в списках админки.
_PAGE = 100


def _disabled() -> HTMLResponse:
    return HTMLResponse("Not Found", status_code=404)


def _guard(request: Request) -> Optional[Response]:
    """404 для всех, кроме владельца: админки для остальных просто нет."""
    if not is_admin(request):
        return _disabled()
    return None


def _render(request: Request, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        template, {"request": request, "admin_enabled": True, **context}
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, days: int = 7):
    denied = _guard(request)
    if denied is not None:
        return denied

    days = max(1, min(days, 90))
    statuses = await reporting.order_status_breakdown()
    parse = await reporting.parse_summary(days=days)
    queue = await reporting.queue_summary()

    async with SessionLocal() as session:
        problems = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.has_problem.is_(True))
                    .order_by(Order.updated_at.desc())
                    .limit(_PAGE)
                )
            ).scalars().all()
        )

    return _render(
        request,
        "admin.html",
        statuses=statuses,
        parse=parse,
        queue=queue,
        problems=problems,
        days=days,
        cache=cache_stats(),
        rate_limit=limiter_stats(),
        section="dashboard",
    )


@router.get("/orders", response_class=HTMLResponse)
async def orders_list(request: Request, status: str = "", q: str = ""):
    denied = _guard(request)
    if denied is not None:
        return denied

    conditions = []
    if status:
        try:
            conditions.append(Order.status == OrderStatus(status))
        except ValueError:
            conditions.append(Order.status == OrderStatus.NEW)
    if q.strip():
        conditions.append(Order.search_text.contains(q.strip().lower(), autoescape=True))

    async with SessionLocal() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(*conditions)
                    .order_by(Order.id.desc())
                    .limit(_PAGE)
                )
            ).scalars().all()
        )

    return _render(
        request,
        "admin_orders.html",
        orders=orders,
        status=status,
        q=q,
        status_choices=[(s.value, ORDER_STATUS_LABELS[s]) for s in OrderStatus],
        section="orders",
    )


@router.post("/orders/{order_id}/hide")
async def hide_order(request: Request, order_id: int):
    """Скрывает заявку из ленты (статус CANCELLED) — мусор, дубль, тест."""
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        result = await session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(status=OrderStatus.CANCELLED, has_problem=False)
        )
        if result.rowcount == 1:
            session.add(
                ActionLog(
                    order_id=order_id, actor=ActorType.LOGIST, action="hidden_by_admin"
                )
            )
            await session.commit()
            log.info("Админ скрыл заказ #%s", order_id)
        else:
            await session.rollback()

    return RedirectResponse(url=request.headers.get("referer") or "/admin/orders", status_code=303)


@router.post("/orders/{order_id}/unhide")
async def unhide_order(request: Request, order_id: int):
    """Возвращает скрытую заявку обратно в ленту."""
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        result = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.CANCELLED)
            .values(status=OrderStatus.NEW)
        )
        if result.rowcount == 1:
            session.add(
                ActionLog(
                    order_id=order_id, actor=ActorType.LOGIST, action="restored_by_admin"
                )
            )
            await session.commit()
            log.info("Админ вернул заказ #%s в ленту", order_id)
        else:
            await session.rollback()

    return RedirectResponse(url=request.headers.get("referer") or "/admin/orders", status_code=303)


@router.get("/queue", response_class=HTMLResponse)
async def queue_list(request: Request, status: str = "failed"):
    """Сообщения, которые не удалось разобрать.

    ``failed`` — попытки исчерпаны, нужен человек. ``pending`` — ещё в работе,
    воркер сам повторит. ``done`` — разобранные, для проверки.
    """
    denied = _guard(request)
    if denied is not None:
        return denied

    try:
        wanted = PendingStatus(status)
    except ValueError:
        wanted = PendingStatus.FAILED

    async with SessionLocal() as session:
        items = list(
            (
                await session.execute(
                    select(PendingMessage)
                    .where(PendingMessage.status == wanted)
                    .order_by(PendingMessage.id.desc())
                    .limit(_PAGE)
                )
            ).scalars().all()
        )
        totals = dict(
            (
                await session.execute(
                    select(PendingMessage.status, func.count(PendingMessage.id)).group_by(
                        PendingMessage.status
                    )
                )
            ).all()
        )

    counts = {
        key.value: int(totals.get(key, totals.get(key.value, 0)) or 0)
        for key in PendingStatus
    }
    return _render(
        request,
        "admin_queue.html",
        items=items,
        status=wanted.value,
        counts=counts,
        section="queue",
    )


@router.post("/queue/{pending_id}/retry")
async def retry_queued(request: Request, pending_id: int):
    """Сбрасывает счётчик попыток и возвращает сообщение в очередь.

    Используется после того, как устранена причина сбоя (пополнили квоту LLM,
    поправили ключ) — иначе сообщения так и останутся в ``failed``.
    """
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        pending = await session.get(PendingMessage, pending_id)
        if pending is not None:
            pending.status = PendingStatus.PENDING
            pending.attempts = 0
            pending.next_attempt_at = now_utc_naive()
            pending.last_error = "reset_by_admin"
            session.add(pending)
            await session.commit()
            log.info("Админ вернул в очередь сообщение id=%s", pending_id)

    return RedirectResponse(url="/admin/queue?status=failed", status_code=303)
