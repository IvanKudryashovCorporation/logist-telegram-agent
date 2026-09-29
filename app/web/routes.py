"""HTTP-роуты публичной витрины заказов.

Тонкие: достали токен водителя → вызвали запрос из :mod:`app.web.queries` →
отдали шаблон. Ни SQL, ни форматирования здесь нет — их можно тестировать
отдельно, без поднятия HTTP.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db.base import SessionLocal
from app.models import Order
from app.timeutil import now_msk_naive
from app.web import queries
from app.web.deps import (
    attach_driver_cookie,
    clear_flash,
    driver_token,
    is_admin,
    pop_flash,
    set_flash,
)
from app.web.filters import Filters
from app.web.presenters import (
    client_name_for,
    client_phone_for,
    dispatcher_link,
    order_bucket,
    redact_raw_text,
)
from app.web.templates_env import templates

log = logging.getLogger("web.routes")

router = APIRouter()


def _parse_coords(lat: str, lon: str) -> Optional[tuple[float, float]]:
    """Геопозиция водителя для сортировки «по близости»."""
    try:
        coords = (float(lat), float(lon))
    except (TypeError, ValueError):
        return None
    # Широта/долгота вне диапазонов — явно мусор, а не точка на Земле.
    if not (-90.0 <= coords[0] <= 90.0 and -180.0 <= coords[1] <= 180.0):
        return None
    return coords


def _base_context(request: Request, counts: dict, notice: Optional[str] = None) -> dict:
    """Общий контекст для всех страниц (шапка, счётчики, flash-сообщение)."""
    return {
        "request": request,
        "notice": notice,
        "is_admin": is_admin(request),
        **counts,
    }


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request,
    q: str = "",
    from_city: str = "",
    to_city: str = "",
    passengers: str = "",
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    price_min: str = "",
    price_max: str = "",
    sort: str = queries.DEFAULT_SORT,
    lat: str = "",
    lon: str = "",
    page: int = 1,
):
    token, new_token = driver_token(request)
    filters = Filters(
        from_city, to_city, passengers, date_from, date_to,
        time_from, time_to, price_min, price_max,
    )
    if sort not in queries.SORT_LABELS:
        sort = queries.DEFAULT_SORT
    coords = _parse_coords(lat, lon) if sort == "distance" else None

    async with SessionLocal() as session:
        feed_page = await queries.fetch_feed(
            session,
            filters=filters,
            q=q,
            sort=sort,
            page=page,
            coords=coords,
        )
        counts = await queries.header_counts(session, token)
        cities = await queries.known_cities(session)

    html = templates.TemplateResponse(
        "orders_list.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "orders": feed_page.items,
            "page_info": feed_page,
            # Все текущие query-параметры кроме page — ссылки пагинации должны
            # сохранять и фильтры, и сортировку, и поисковый запрос.
            "page_query": {
                key: value for key, value in request.query_params.items() if key != "page"
            },
            "q": q.strip(),
            "filters": filters,
            "known_cities": cities,
            "sort": sort,
            "lat": lat,
            "lon": lon,
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.get("/my", response_class=HTMLResponse)
async def my_orders_page(request: Request):
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        orders = await queries.my_orders(session, token)
        counts = await queries.header_counts(session, token)

    today = now_msk_naive().date()
    buckets: dict[str, list] = {"today": [], "tomorrow": [], "later": [], "no_date": []}
    for order in orders:
        buckets[order_bucket(order, today)].append(order)

    html = templates.TemplateResponse(
        "my_orders.html",
        _base_context(request, counts, pop_flash(request)) | {"buckets": buckets},
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)
        counts = await queries.header_counts(session, token)
        is_mine = order.taken_by_token == token
        link = dispatcher_link(order)
        # Телефон/имя клиента готовим здесь, а не в шаблоне: шаблон не должен
        # знать, кому разрешено видеть персональные данные.
        phone = client_phone_for(order, is_owner=is_mine)
        client_name = client_name_for(order, is_owner=is_mine)
        # Оригинал заявки тоже содержит телефон и имя — показывать его
        # посторонним без вымарывания значит обойти маскирование целиком.
        raw_text = redact_raw_text(order, is_owner=is_mine)

    html = templates.TemplateResponse(
        "order_detail.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "order": order,
            "is_mine": is_mine,
            "is_taken_by_someone_else": bool(order.taken_by_token) and not is_mine,
            "dispatcher_url": link,
            "phone_display": phone,
            "client_name_display": client_name,
            "raw_text_display": raw_text,
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


def _redirect(url: str, new_token: Optional[str], notice: str = "") -> Response:
    """303 See Other — ответ на POST, который браузер откроет как GET."""
    response = RedirectResponse(url=url, status_code=303)
    attach_driver_cookie(response, new_token)
    # Всегда вызываем set_flash: при пустом тексте она удаляет cookie. Иначе
    # сообщение от предыдущего действия показывалось бы ещё раз.
    set_flash(response, notice)
    return response


@router.post("/orders/{order_id}/take")
async def take_order(request: Request, order_id: int):
    """«Взять заказ» без перехода в Telegram (оставлено для прямых ссылок)."""
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        result = await queries.take_order(session, order_id, token)

    return _redirect(
        f"/orders/{order_id}", new_token,
        "" if result.ok else result.message,
    )


@router.post("/orders/{order_id}/take-and-contact")
async def take_and_contact(request: Request, order_id: int):
    """Единая кнопка «Написать диспетчеру»: берёт заказ себе (если ещё свободен)
    и сразу открывает диалог с диспетчером в Telegram."""
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)

        result = await queries.take_order(session, order_id, token)
        if not result.ok and result.reason == "taken_by_other":
            # Уже взят другим водителем — просто открываем карточку,
            # без перехода к диспетчеру.
            return _redirect(f"/orders/{order_id}", new_token, result.message)
        link = dispatcher_link(order)

    if not result.ok and result.reason not in ("", "not_owner", "already_mine"):
        # Заказ закрыт/просрочен: к диспетчеру не пускаем, показываем причину.
        # "already_mine" сюда не относится: водитель просто нажал «Написать
        # диспетчеру» второй раз, заказ уже его — открываем диалог как обычно.
        return _redirect(f"/orders/{order_id}", new_token, result.message)

    return _redirect(link or f"/orders/{order_id}", new_token)


@router.post("/orders/{order_id}/release")
async def release_order(request: Request, order_id: int):
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        result = await queries.release_order(session, order_id, token)

    return _redirect("/my", new_token, "" if result.ok else result.message)


@router.post("/orders/{order_id}/agree")
async def agree_order(request: Request, order_id: int):
    """Водитель подтвердил, что договорился с диспетчером — заказ закрыт.

    «Отменить взятие» по-прежнему откатывает это обратно, если передумали.
    """
    token, new_token = driver_token(request)

    async with SessionLocal() as session:
        result = await queries.agree_order(session, order_id, token)

    return _redirect(
        f"/orders/{order_id}", new_token, "" if result.ok else result.message
    )


#: Готовые варианты обратной связи — кнопками, чтобы водитель не печатал.
FEEDBACK_REASONS = {
    "price_outdated": "Цена неактуальна",
    "no_answer": "Диспетчер не отвечает",
    "taken_already": "Рейс уже занят",
    "wrong_data": "Данные в заявке неверные",
}


@router.post("/orders/{order_id}/feedback")
async def order_feedback(request: Request, order_id: int):
    """Обратная связь водителя по заявке.

    Без этого владелец агрегатора не узнаёт, что диспетчер публикует
    несуществующие рейсы или давно не отвечает, — а именно это убивает
    доверие водителей к ленте.
    """
    token, new_token = driver_token(request)
    form = await request.form()
    reason_key = str(form.get("reason") or "").strip()
    reason = FEEDBACK_REASONS.get(reason_key, reason_key)
    note = str(form.get("note") or "").strip()
    text = " · ".join(part for part in (reason, note) if part)[:500]

    if not text:
        return _redirect(f"/orders/{order_id}", new_token, "Выберите причину жалобы.")

    async with SessionLocal() as session:
        result = await queries.report_problem(session, order_id, token, text)

    if result.ok:
        log.info("Жалоба на заказ #%s от водителя: %s", order_id, text)
    return _redirect(
        f"/orders/{order_id}", new_token,
        "Спасибо, жалоба отправлена владельцу." if result.ok else result.message,
    )
