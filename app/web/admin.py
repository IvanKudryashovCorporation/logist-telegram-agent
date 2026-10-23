"""Админка владельца: состояние ленты, очередь разбора, качество парсинга.

Раньше единственный способ что-то узнать о работе системы — читать лог агента
вручную. Здесь собрано то, что нужно каждый день:

* сколько заказов в каждом статусе и сколько помечено водителями как проблемные;
* сколько заказов появляется в день и какие направления самые популярные;
* очередь повторного разбора (сообщения, где LLM отказала) и кнопка «повторить»;
* качество разбора: доля неполных заявок, расход токенов, латентность,
  насколько предфильтр экономит обращения к LLM;
* раздел «Нужно уточнить»: заявки, где модель не уверена в данных, с причиной и кнопками
  «Подтвердить» / «Скрыть»;
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

from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    ActorType,
    Order,
    OrderStatus,
    PendingMessage,
    PendingStatus,
    WorkGroup,
)
from app.parsing.llm_parser import cache_stats
from app.services import billing, geo_health, notifications, reporting
from app.services import billing_events as events
from app.services.subscriptions import describe_filters, set_subscription_active
from app.timeutil import now_utc_naive
from app.web import queries
from app.web.deps import is_admin
from app.web.rate_limit import limiter_stats
from app.web.templates_env import templates
from app.web.traffic import source_label

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


async def _clarify_count() -> int:
    """Сколько заявок ждут уточнения — число в пункте меню."""
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(func.count(Order.id)).where(Order.status == OrderStatus.NEEDS_CLARIFICATION)
            )
        ).scalar_one()


async def _groups_count() -> int:
    """Сколько групп сейчас читает агент («Live · N групп» в шапке админки)."""
    async with SessionLocal() as session:
        return (
            await session.execute(select(func.count(WorkGroup.id)).where(WorkGroup.is_active.is_(True)))
        ).scalar_one()


async def _render(request: Request, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        template,
        {
            "request": request, "admin_enabled": True, "clarify_count": await _clarify_count(),
            "groups_count": await _groups_count(), **context,
        },
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
    delay = await reporting.delay_summary(hours=24)
    daily = await reporting.orders_per_day(days=days)
    routes = await reporting.popular_routes(days=days)
    hidden = await reporting.hidden_breakdown(days=days)

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
        distance_issues = await geo_health.distance_issues(session)

    return await _render(
        request,
        "admin.html",
        statuses=statuses,
        parse=parse,
        queue=queue,
        delay=delay,
        daily=daily,
        routes=routes,
        hidden=hidden,
        problems=problems,
        distance_issues=distance_issues,
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

    return await _render(
        request,
        "admin_orders.html",
        orders=orders,
        status=status,
        q=q,
        status_choices=[(s.value, ORDER_STATUS_LABELS[s]) for s in OrderStatus],
        section="orders",
    )


def clarification_reason(order: Order, logged: Optional[str]) -> str:
    """Что именно не удалось определить в заявке.

    Основной источник — то, что написала модель разбора (журнал ``order_created``/``order_edited``);
    если её пометок нет, показываем очевидные пробелы в полях."""
    notes = (logged or "").removeprefix("missing_fields=").strip()
    if notes.startswith("["):
        notes = notes.strip("[]").replace("'", "").replace('"', "").strip()
    reasons = [notes] if notes else []
    if not order.from_city or not order.to_city:
        reasons.append("не определён город " + ("отправления" if not order.from_city else "назначения"))
    if order.client_price is None:
        reasons.append("не указана цена")
    return "; ".join(reasons) or "причина не записана"


@router.get("/clarify", response_class=HTMLResponse)
async def clarify_list(request: Request):
    """Заявки со статусом «Нужно уточнить»: модель разбора не уверена в данных."""
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.status == OrderStatus.NEEDS_CLARIFICATION)
                    .order_by(Order.id.desc())
                    .limit(_PAGE)
                )
            ).scalars().all()
        )
        logged: dict[int, str] = {}
        if orders:
            rows = (
                await session.execute(
                    select(ActionLog.order_id, ActionLog.details)
                    .where(
                        ActionLog.order_id.in_([o.id for o in orders]),
                        ActionLog.action.in_(("order_created", "order_edited")),
                    )
                    .order_by(ActionLog.id.asc())
                )
            ).all()
            logged = {order_id: details for order_id, details in rows if details}  # последняя запись побеждает

    now = now_utc_naive()
    items = [
        {
            "order": order,
            "reason": clarification_reason(order, logged.get(order.id)),
            "age_hours": max(0, int((now - order.created_at).total_seconds() // 3600)),
        }
        for order in orders
    ]
    return await _render(request, "admin_clarify.html", items=items, section="clarify")


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, q: str = "", sort: str = "login"):
    """Все зарегистрированные пользователи с короткой статистикой."""
    denied = _guard(request)
    if denied is not None:
        return denied

    overview = await reporting.users_overview(q=q, sort=sort)
    return await _render(
        request,
        "admin_users.html",
        overview=overview,
        q=q,
        sort=sort if sort in reporting.USER_SORTS else "login",
        sorts=reporting.USER_SORTS,
        admin_ids=settings.admin_ids,
        section="users",
    )


@router.get("/users/{telegram_id}", response_class=HTMLResponse)
async def user_card(request: Request, telegram_id: int):
    """Карточка пользователя: профиль, статистика, подписка, заказы, история и кнопки."""
    denied = _guard(request)
    if denied is not None:
        return denied

    detail = await reporting.user_detail(telegram_id)
    if detail is None:
        return _disabled()
    return await _render(
        request,
        "admin_user.html",
        u=detail,
        filter_lines={sub.id: describe_filters(sub.params) for sub in detail["subscriptions"]},
        status_labels=ORDER_STATUS_LABELS,
        is_admin_user=telegram_id in settings.admin_ids,
        section="users",
    )


@router.post("/users/{telegram_id}/filters/{sub_id}/{action}")
async def user_filter(request: Request, telegram_id: int, sub_id: int, action: str):
    """Включить или выключить уведомления по фильтру пользователя (например, если он жалуется на спам)."""
    denied = _guard(request)
    if denied is not None:
        return denied
    if action not in ("on", "off"):
        return _disabled()

    async with SessionLocal() as session:
        sub = await set_subscription_active(session, telegram_id, sub_id, action == "on")
    if sub is not None:
        log.info(
            "Админ %s уведомления по фильтру #%s у tg:%s",
            "включил" if action == "on" else "выключил", sub_id, telegram_id,
        )
    return RedirectResponse(url=f"/admin/users/{telegram_id}", status_code=303)


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, days: int = 7):
    """Статистика по группам: сколько заказов дала каждая, сколько дублей и спама."""
    denied = _guard(request)
    if denied is not None:
        return denied

    stats = await reporting.group_stats(days=max(1, min(days, 30)))
    return await _render(request, "admin_groups.html", g=stats, section="groups")


@router.get("/traffic", response_class=HTMLResponse)
async def traffic_page(request: Request, days: int = 30):
    """Откуда на сайт идут посетители: источники, переходы с сайтов, метки, регистрации по источникам."""
    denied = _guard(request)
    if denied is not None:
        return denied

    days = max(1, min(days, 90))
    overview = await reporting.traffic_overview(days=days)
    return await _render(
        request, "admin_traffic.html", t=overview, source_label=source_label, section="traffic",
    )


@router.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, days: int = 30):
    """Платежи и журнал подписки: что пришло, что вернули, что пошло не так."""
    denied = _guard(request)
    if denied is not None:
        return denied

    days = max(1, min(days, 365))
    overview = await reporting.billing_overview(days=days)
    funnel = await reporting.subscription_metrics(days=days)
    return await _render(
        request, "admin_billing.html", o=overview, f=funnel, kind_labels=events.KIND_LABELS,
        problem_kinds=events.PROBLEM_KINDS, section="billing",
    )


@router.post("/notify")
async def broadcast_notification(request: Request):
    """Объявление всем водителям: появится в колокольчике на сайте."""
    denied = _guard(request)
    if denied is not None:
        return denied

    form = await request.form()
    title = str(form.get("title") or "").strip()[:120]
    text = str(form.get("text") or "").strip()[:1000]
    url = str(form.get("url") or "").strip()[:300] or None
    if title:
        await notifications.broadcast(title, text, url=url)
        log.info("Админ отправил объявление в колокольчик: %s", title)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{telegram_id}/subscription/{action}")
async def user_subscription(request: Request, telegram_id: int, action: str):
    """Подписка пользователя вручную: добавить дни (подарок, компенсация) или обнулить доступ."""
    denied = _guard(request)
    if denied is not None:
        return denied
    if action not in ("grant", "reset"):
        return _disabled()

    async with SessionLocal() as session:
        if action == "grant":
            form = await request.form()
            try:
                days = max(1, min(int(str(form.get("days") or "0")), 366))
            except ValueError:
                days = 0
            if days:
                until = await billing.grant_days(session, telegram_id, days)
                log.info("Админ добавил %s дн. подписки tg:%s (до %s)", days, telegram_id, until)
                await events.record(events.ADMIN_GRANT, telegram_id=telegram_id, details=f"+{days} дн., до {until}")
        else:
            await billing.reset_access(session, telegram_id)
            log.info("Админ обнулил доступ по подписке у tg:%s", telegram_id)
            await events.record(events.ADMIN_RESET, telegram_id=telegram_id, details="доступ обнулён")
    return RedirectResponse(url=f"/admin/users/{telegram_id}", status_code=303)


@router.post("/users/{telegram_id}/release/{order_id}")
async def user_release_order(request: Request, telegram_id: int, order_id: int):
    """Снять заказ с водителя (взял и пропал): заказ возвращается в ленту."""
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        result = await queries.release_order(session, order_id, f"tg:{telegram_id}")
    if result.ok:
        log.info("Админ снял заказ #%s с tg:%s", order_id, telegram_id)
    return RedirectResponse(url=f"/admin/users/{telegram_id}", status_code=303)


@router.post("/orders/{order_id}/confirm")
async def confirm_order(request: Request, order_id: int):
    """«Подтвердить»: админ проверил заявку — убираем пометку «Нужно уточнить» (статус «Новая»)."""
    denied = _guard(request)
    if denied is not None:
        return denied

    async with SessionLocal() as session:
        result = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.NEEDS_CLARIFICATION)
            .values(status=OrderStatus.NEW)
        )
        if result.rowcount == 1:
            session.add(
                ActionLog(order_id=order_id, actor=ActorType.LOGIST, action="confirmed_by_admin")
            )
            await session.commit()
            log.info("Админ подтвердил заказ #%s", order_id)
        else:
            await session.rollback()

    return RedirectResponse(url=request.headers.get("referer") or "/admin/clarify", status_code=303)


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
    return await _render(
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
