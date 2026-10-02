"""HTTP-роуты публичной витрины заказов.

Тонкие: достали токен водителя → вызвали запрос из :mod:`app.web.queries` →
отдали шаблон. Ни SQL, ни форматирования здесь нет — их можно тестировать
отдельно, без поднятия HTTP.
"""

import logging
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Driver, Order
from app.services.subscriptions import (
    describe_filters,
    feed_path,
    get_subscription,
    has_route,
    save_subscription,
    set_subscription_active,
)
from app.timeutil import now_msk_naive
from app.web import queries
from app.web.deps import (
    attach_driver_cookie,
    clear_flash,
    driver_token,
    is_admin,
    logged_in_telegram_id,
    pop_flash,
    resolve_driver,
    set_flash,
)
from app.web.filters import Filters
from app.web.presenters import (
    client_name_for,
    client_phone_for,
    dispatcher_link,
    dispatcher_message,
    order_bucket,
    redact_raw_text,
)
from app.web.templates_env import STATIC_DIR, templates

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


def _order_url(order_id: int, back: str = "") -> str:
    """Ссылка на карточку заказа, сохраняющая ``back`` (фильтры/сортировка
    ленты, откуда пришёл водитель) через все действия на странице заказа."""
    url = f"/orders/{order_id}"
    return f"{url}?back={quote(back, safe='')}" if back else url


def _login_redirect(request: Request, next_url: Optional[str] = None) -> RedirectResponse:
    """Действие или «Мои заказы» требуют входа — уводим на /login с возвратом
    после успешной авторизации.

    ``next_url`` обязателен для POST-действий: адрес самого запроса после входа
    открывается как GET и даёт 405, вернуть нужно на карточку заказа.
    """
    target = next_url
    if target is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


async def _attach_radius_centers(session, filters: Filters) -> None:
    """Находит центры кругов для радиуса «откуда/куда» (только кэш, без сети)."""
    if filters.from_radius and filters.from_city:
        filters.from_centers, missing = await geo.centers_for(session, filters.from_city)
        filters.radius_missing += missing
    if filters.to_radius and filters.to_city:
        filters.to_centers, missing = await geo.centers_for(session, filters.to_city)
        filters.radius_missing += missing


def _base_context(request: Request, counts: dict, notice: Optional[str] = None) -> dict:
    """Общий контекст для всех страниц (шапка, счётчики, flash-сообщение)."""
    return {
        "request": request,
        "notice": notice,
        "telegram_login_enabled": settings.telegram_login_enabled,
        "is_logged_in": logged_in_telegram_id(request) is not None,
        "is_admin": is_admin(request),
        **counts,
    }


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


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
    from_radius: str = "",
    to_radius: str = "",
    sort: str = queries.DEFAULT_SORT,
    lat: str = "",
    lon: str = "",
    page: int = 1,
):
    token, new_token = driver_token(request)
    filters = Filters(
        from_city, to_city, passengers, date_from, date_to,
        time_from, time_to, price_min, price_max, from_radius, to_radius,
    )
    if sort not in queries.SORT_LABELS:
        sort = queries.DEFAULT_SORT
    coords = _parse_coords(lat, lon) if sort == "distance" else None

    async with SessionLocal() as session:
        await _attach_radius_centers(session, filters)
        telegram_id = logged_in_telegram_id(request) if settings.telegram_login_enabled else None
        subscription = await get_subscription(session, telegram_id) if telegram_id else None
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
            # Галочка «присылать в Telegram»: по умолчанию включена, пока водитель
            # сам её не выключил (или бот не смог ему написать).
            "notify_checked": subscription is None or subscription.is_active,
            "subscription_error": subscription.error if subscription else None,
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
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request)

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


_FILTER_FIELDS = (
    "from_city", "to_city", "passengers", "date_from", "date_to",
    "time_from", "time_to", "price_min", "price_max", "from_radius", "to_radius",
)


@router.post("/filter")
async def apply_filter(request: Request):
    """«Применить» у вошедшего водителя: показывает ленту по фильтру и, если стоит
    галочка, подписывает его на новые подходящие заказы в Telegram.

    Адрес ленты собирается из НОРМАЛИЗОВАННЫХ параметров (``Filters``), поэтому
    мусор из формы в ссылку не попадает.
    """
    form = await request.form()
    filters = Filters(**{name: str(form.get(name) or "") for name in _FILTER_FIELDS})
    params = filters.as_dict()

    view = {key: value for key, value in params.items() if value}
    for extra in ("q", "lat", "lon"):
        if form.get(extra):
            view[extra] = str(form[extra])[:200]
    if str(form.get("sort") or "") in queries.SORT_LABELS:
        view["sort"] = str(form["sort"])
    target = f"/?{urlencode(view)}" if view else "/"

    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, target)

    notice = ""
    telegram_id = logged_in_telegram_id(request)
    async with SessionLocal() as session:
        existing = await get_subscription(session, telegram_id)
        if form.get("notify"):
            if has_route(params):
                await save_subscription(session, telegram_id, params)
                notice = "Фильтр применён. Новые подходящие заказы пришлём вам в Telegram."
            else:
                notice = "Для уведомлений укажите город «Откуда» или «Куда» — фильтр применён без них."
        elif existing is not None and existing.is_active:
            await set_subscription_active(session, telegram_id, False)
            notice = "Уведомления о новых заказах выключены."
    return _redirect(target, new_token, notice)


@router.post("/subscription/{action}")
async def subscription_toggle(request: Request, action: str):
    """Кнопки в профиле: выключить / включить уведомления по сохранённому фильтру."""
    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/profile")
    if action not in ("on", "off"):
        return HTMLResponse("Страница не найдена", status_code=404)

    async with SessionLocal() as session:
        sub = await set_subscription_active(session, logged_in_telegram_id(request), action == "on")
    notice = ""
    if sub is not None:
        notice = "Уведомления включены." if action == "on" else "Уведомления выключены."
    return _redirect("/profile", new_token, notice)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    """Профиль вошедшего водителя: кто он, статистика, последние заказы, выход."""
    if not settings.telegram_login_enabled:
        return HTMLResponse("Страница не найдена", status_code=404)
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request)

    async with SessionLocal() as session:
        driver = (
            await session.execute(
                select(Driver).where(Driver.telegram_id == logged_in_telegram_id(request))
            )
        ).scalar_one_or_none()
        if driver is None:
            # Сессия подписана, а записи водителя нет (удалили вручную) — войти заново.
            return _login_redirect(request)
        stats = await queries.driver_stats(session, token, driver.created_at)
        recent = await queries.recent_driver_orders(session, token)
        counts = await queries.header_counts(session, token)
        subscription = await get_subscription(session, driver.telegram_id)

    html = templates.TemplateResponse(
        "profile.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "driver": driver,
            "stats": stats,
            "recent": recent,
            "subscription": subscription,
            "subscription_lines": describe_filters(subscription.params) if subscription else [],
            "subscription_link": feed_path(subscription.params) if subscription else "/",
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int, back: str = ""):
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        # Гость: карточку смотреть можно, но ничего не «его». Вход нужен только
        # для действий (написать диспетчеру, договориться) — см. POST-маршруты.
        token, new_token = "", None

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return HTMLResponse("Заказ не найден", status_code=404)
        counts = await queries.header_counts(session, token)
        # bool(token): у гостя токена нет, а у свободного заказа taken_by_token
        # тоже пуст — без этой проверки гость считался бы «хозяином» любого
        # свободного заказа и видел бы телефон клиента.
        is_mine = bool(token) and order.taken_by_token == token
        if order.status in HIDDEN_STATUSES and not is_mine:
            # Протухшая/снятая/закрытая заявка пропала из ленты — по прямой
            # ссылке её тоже не показываем. Свою историю водитель видит.
            gone = templates.TemplateResponse(
                "order_gone.html",
                _base_context(request, counts) | {"order_id": order_id, "back": back},
                status_code=410,
            )
            attach_driver_cookie(gone, new_token)
            return gone
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
            "dispatcher_text": dispatcher_message(order),
            "phone_display": phone,
            "client_name_display": client_name,
            "raw_text_display": raw_text,
            "back": back,
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
async def take_order(request: Request, order_id: int, back: str = ""):
    """«Взять заказ» без перехода в Telegram (оставлено для прямых ссылок)."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    async with SessionLocal() as session:
        result = await queries.take_order(session, order_id, token)

    return _redirect(
        _order_url(order_id, back), new_token,
        "" if result.ok else result.message,
    )


@router.post("/orders/{order_id}/contact")
async def contact_dispatcher(request: Request, order_id: int, back: str = ""):
    """«Написать диспетчеру»: открывает чат с готовым текстом. Заказ при этом НЕ
    берётся — «моим» он становится только после «Договорился с диспетчером»."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
    if order is None:
        return HTMLResponse("Заказ не найден", status_code=404)

    is_mine = order.taken_by_token == token
    if order.taken_by_token and not is_mine:
        return _redirect(
            _order_url(order_id, back), new_token,
            queries.ActionResult(False, "taken_by_other").message,
        )
    if order.status in HIDDEN_STATUSES and not is_mine:
        return _redirect(
            _order_url(order_id, back), new_token,
            queries.ActionResult(False, "closed").message,
        )

    link = dispatcher_link(order, text=dispatcher_message(order))
    return _redirect(link or _order_url(order_id, back), new_token)


@router.post("/orders/{order_id}/release")
async def release_order(request: Request, order_id: int):
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id))

    async with SessionLocal() as session:
        result = await queries.release_order(session, order_id, token)

    return _redirect("/my", new_token, "" if result.ok else result.message)


@router.post("/orders/{order_id}/agree")
async def agree_order(request: Request, order_id: int, back: str = ""):
    """«Договорился с диспетчером»: заказ уходит из ленты и появляется в «Моих
    заказах». «Отменить» там откатывает это обратно, если передумали."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    async with SessionLocal() as session:
        result = await queries.agree_order(session, order_id, token)

    if result.ok or result.reason == "already_mine":
        notice = "Заказ перенесён в «Мои заказы»." if result.ok else ""
        return _redirect("/my", new_token, notice)
    return _redirect(_order_url(order_id, back), new_token, result.message)


@router.post("/orders/{order_id}/complete")
async def complete_order(request: Request, order_id: int, back: str = ""):
    """«Выполнил заказ»: заказ становится «Выполнен» и идёт в заработок профиля."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    async with SessionLocal() as session:
        result = await queries.complete_order(session, order_id, token)

    if result.ok:
        return _redirect("/my", new_token, "Заказ отмечен выполненным.")
    return _redirect(_order_url(order_id, back), new_token, result.message)


#: Готовые варианты обратной связи — кнопками, чтобы водитель не печатал.
FEEDBACK_REASONS = {
    "price_outdated": "Цена неактуальна",
    "no_answer": "Диспетчер не отвечает",
    "taken_already": "Рейс уже занят",
    "wrong_data": "Данные в заявке неверные",
}


@router.post("/orders/{order_id}/feedback")
async def order_feedback(request: Request, order_id: int, back: str = ""):
    """Обратная связь водителя по заявке.

    Без этого владелец агрегатора не узнаёт, что диспетчер публикует
    несуществующие рейсы или давно не отвечает, — а именно это убивает
    доверие водителей к ленте.
    """
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))
    form = await request.form()
    reason_key = str(form.get("reason") or "").strip()
    reason = FEEDBACK_REASONS.get(reason_key, reason_key)
    note = str(form.get("note") or "").strip()
    text = " · ".join(part for part in (reason, note) if part)[:500]

    if not text:
        return _redirect(_order_url(order_id, back), new_token, "Выберите причину жалобы.")

    async with SessionLocal() as session:
        result = await queries.report_problem(session, order_id, token, text)

    if result.ok:
        log.info("Жалоба на заказ #%s от водителя: %s", order_id, text)
    return _redirect(
        _order_url(order_id, back), new_token,
        "Спасибо, жалоба отправлена владельцу." if result.ok else result.message,
    )
