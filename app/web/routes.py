"""HTTP-роуты публичной витрины заказов.

Тонкие: достали токен водителя → вызвали запрос из :mod:`app.web.queries` →
отдали шаблон. Ни SQL, ни форматирования здесь нет — их можно тестировать
отдельно, без поднятия HTTP.
"""

import json
import logging
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Driver, Order
from app.services import billing, notifications, user_events
from app.services.subscriptions import (
    ADDED,
    EXISTS,
    LIMIT,
    add_subscription,
    delete_subscription,
    describe_filters,
    feed_path,
    get_subscription,
    has_criteria,
    list_subscriptions,
    set_subscription_active,
    update_subscription,
)
from app.timeutil import now_msk_naive
from app.web import favorites, queries
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
    """Куда вернуть водителя после действия с заказом: карточки заказа нет, поэтому в ленту
    (с теми же фильтрами, откуда он пришёл — ``back``)."""
    return f"/?{back}" if back else "/"


def _owner_token(request: Request, anonymous_token: str) -> str:
    """Чьё это избранное: ``tg:<id>`` после входа через Telegram, иначе анонимная cookie."""
    telegram_id = logged_in_telegram_id(request)
    return f"tg:{telegram_id}" if telegram_id is not None else anonymous_token


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
        "owner_contact_url": (
            f"https://t.me/{settings.owner_contact_username.strip().lstrip('@')}"
            if settings.owner_contact_username.strip() else ""
        ),
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
    vehicle: str = "",
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    price_min: str = "",
    price_max: str = "",
    from_radius: str = "",
    to_radius: str = "",
    sort: str = queries.DEFAULT_SORT,
    dir: str = "",
    lat: str = "",
    lon: str = "",
    page: int = 1,
    edit: int = 0,
    filters_open: int = 0,
):
    token, new_token = driver_token(request)
    telegram_id = logged_in_telegram_id(request)
    if request.query_params.get("reset"):
        # Кнопка «Сбросить»: забываем запомненный фильтр и открываем полную ленту.
        await _remember_filter(telegram_id, {})
        return _redirect(f"/?{urlencode({'q': q.strip()})}" if q.strip() else "/", new_token)
    if telegram_id is not None and not request.query_params:
        # Вошёл заново без параметров — открываем ленту с его последним фильтром.
        saved = await _saved_filter(telegram_id)
        if saved:
            return _redirect(
                f"/?{urlencode(saved)}", new_token,
                "Показан ваш последний фильтр. Чтобы вернуть всю ленту, нажмите «Сбросить» в фильтре.",
            )
    filters = Filters(
        from_city=from_city, to_city=to_city, vehicle=vehicle,
        date_from=date_from, date_to=date_to, time_from=time_from, time_to=time_to,
        price_min=price_min, price_max=price_max, from_radius=from_radius, to_radius=to_radius,
    )
    # Сортировки на сайте нет: всегда свежие сверху (старые ссылки с sort/dir просто игнорируем).
    sort = queries.DEFAULT_SORT
    direction = queries.effective_direction(sort, "")
    coords = None
    if telegram_id is not None and any(value for value in filters.as_dict().values()):
        # Фильтр задан ссылкой (а не формой) — тоже запоминаем.
        await _remember_filter(telegram_id, {key: value for key, value in filters.as_dict().items() if value})

    async with SessionLocal() as session:
        await _attach_radius_centers(session, filters)
        feed_page = await queries.fetch_feed(
            session,
            filters=filters,
            q=q,
            sort=sort,
            direction=direction,
            page=page,
            coords=coords,
        )
        counts = await queries.header_counts(
            session, token,
            telegram_id=logged_in_telegram_id(request) if settings.telegram_login_enabled else None,
        )
        cities = await queries.known_cities(session)
        favorite_set = await favorites.favorite_ids(
            session, _owner_token(request, token), [order.id for order in feed_page.items]
        )
        editing = None
        if edit and telegram_id is not None:
            editing = await get_subscription(session, telegram_id, edit)

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
            # Галочка «сохранить в Мои фильтры»: каждое «Применить» не должно плодить фильтры,
            # поэтому по умолчанию снята — водитель отмечает её, когда фильтр нужен надолго.
            "notify_checked": False,
            "favorite_set": favorite_set,
            # Фильтр, который сейчас правят: форма сохранит изменения в него, а не создаст новый.
            "editing": editing,
            "open_filters": bool(filters_open or editing),
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.get("/feed/count")
async def feed_count(
    q: str = "",
    from_city: str = "",
    to_city: str = "",
    vehicle: str = "",
    date_from: str = "",
    date_to: str = "",
    time_from: str = "",
    time_to: str = "",
    price_min: str = "",
    price_max: str = "",
    from_radius: str = "",
    to_radius: str = "",
) -> JSONResponse:
    """Сколько заказов найдётся по фильтру — число на кнопке «Применить (N)».

    Пересчитывается на лету, пока водитель меняет поля фильтра. Те же условия,
    что у ленты (:func:`queries.feed_where`), поэтому число совпадает с
    «Показано … из N» после применения.
    """
    filters = Filters(
        from_city=from_city, to_city=to_city, vehicle=vehicle,
        date_from=date_from, date_to=date_to, time_from=time_from, time_to=time_to,
        price_min=price_min, price_max=price_max, from_radius=from_radius, to_radius=to_radius,
    )
    async with SessionLocal() as session:
        await _attach_radius_centers(session, filters)
        total = await queries.count_feed(session, filters=filters, q=q)
    return JSONResponse({"total": total}, headers={"Cache-Control": "no-store"})


@router.get("/favorites", response_class=HTMLResponse)
@router.get("/my", response_class=HTMLResponse)
async def favorites_page(request: Request):
    """«Избранное»: заказы с сердечком и, ниже, те, по которым уже договорились."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request)

    async with SessionLocal() as session:
        taken = await queries.my_orders(session, token)
        marked = await favorites.favorite_orders(session, token)
        counts = await queries.header_counts(session, token)

    taken_ids = {order.id for order in taken}
    marked = [order for order in marked if order.id not in taken_ids]  # договорились — показываем ниже, не дважды
    today = now_msk_naive().date()
    buckets: dict[str, list] = {"today": [], "tomorrow": [], "later": [], "no_date": []}
    for order in taken:
        buckets[order_bucket(order, today)].append(order)

    html = templates.TemplateResponse(
        "my_orders.html",
        _base_context(request, counts, pop_flash(request))
        | {"buckets": buckets, "favorites": marked, "favorite_set": {order.id for order in marked}},
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


_FILTER_FIELDS = (
    "from_city", "to_city", "vehicle", "date_from", "date_to",
    "time_from", "time_to", "price_min", "price_max", "from_radius", "to_radius",
)


_SAVED_KEYS = frozenset(_FILTER_FIELDS) | {"sort", "dir"}


async def _remember_filter(telegram_id: Optional[int], view: dict) -> None:
    """Запоминает последний фильтр ленты вошедшего водителя (пустой — забывает)."""
    if telegram_id is None:
        return
    saved = {key: value for key, value in view.items() if key in _SAVED_KEYS and value}
    if not any(saved.get(name) for name in _FILTER_FIELDS):
        saved = {}
    async with SessionLocal() as session:
        driver = (
            await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if driver is None:
            return
        value = json.dumps(saved, ensure_ascii=False) if saved else None
        if driver.saved_filter != value:
            driver.saved_filter = value
            await session.commit()


async def _saved_filter(telegram_id: Optional[int]) -> dict:
    """Последний фильтр ленты водителя, прогнанный через те же проверки, что и обычный фильтр."""
    if telegram_id is None:
        return {}
    async with SessionLocal() as session:
        raw = (
            await session.execute(select(Driver.saved_filter).where(Driver.telegram_id == telegram_id))
        ).scalar_one_or_none()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    params = {k: v for k, v in Filters(**{n: str(data.get(n) or "") for n in _FILTER_FIELDS}).as_dict().items() if v}
    if str(data.get("sort") or "") in queries.SORT_LABELS:
        params["sort"] = str(data["sort"])
        if str(data.get("dir") or "") in queries.DIRECTION_LABELS:
            params["dir"] = str(data["dir"])
    return params


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
    if form.get("q"):
        view["q"] = str(form["q"])[:200]
    target = f"/?{urlencode(view)}" if view else "/"

    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, target)

    tg_id = logged_in_telegram_id(request)
    summary = "; ".join(describe_filters(params)) or "без условий"

    edit_id = str(form.get("edit_id") or "")
    if edit_id.isdigit():
        # Правка сохранённого фильтра из «Мои фильтры»: не создаём новый, а меняем этот.
        if not has_criteria(params):
            return _redirect(
                "/filters", new_token,
                "Чтобы сохранить фильтр, задайте хотя бы одно условие: город, тип авто, дату, время или цену.",
            )
        async with SessionLocal() as session:
            sub, outcome = await update_subscription(session, tg_id, int(edit_id), params)
        if sub is None:
            return _redirect("/filters", new_token, "Такого фильтра уже нет.")
        if outcome == EXISTS:
            return _redirect("/filters", new_token, "Такой фильтр уже есть в «Мои фильтры» — изменения не сохранены.")
        await user_events.record(user_events.FILTER_SAVED, tg_id, f"изменён: {summary}")
        return _redirect("/filters", new_token, "Фильтр изменён.")

    await _remember_filter(tg_id, view)
    await user_events.record(user_events.FILTER_APPLIED, tg_id, summary)

    notice = ""
    if form.get("notify") and not await _has_subscription(request):
        notice = "Подписка закончилась: фильтр применён без сохранения. Уведомления в Telegram доступны по подписке."
    elif form.get("notify"):
        if not has_criteria(params):
            notice = (
                "Чтобы сохранить фильтр, задайте хотя бы одно условие: город, тип авто, дату, время или цену — "
                "фильтр применён без сохранения."
            )
        else:
            async with SessionLocal() as session:
                _sub, outcome = await add_subscription(session, logged_in_telegram_id(request), params)
            await user_events.record(
                {ADDED: user_events.FILTER_SAVED, EXISTS: user_events.FILTER_EXISTS, LIMIT: user_events.FILTER_LIMIT}[outcome],
                tg_id, summary,
            )
            notice = {
                ADDED: "Фильтр применён и добавлен в «Мои фильтры». Новые подходящие заказы пришлём вам в Telegram.",
                EXISTS: "Такой фильтр уже есть в «Мои фильтры» — уведомления по нему включены.",
                LIMIT: (
                    f"В «Мои фильтры» уже {settings.max_filters_per_driver} фильтров — это максимум. "
                    "Удалите ненужный, и сохраните этот. Фильтр применён без сохранения."
                ),
            }[outcome]
    return _redirect(target, new_token, notice)


@router.get("/filters", response_class=HTMLResponse)
async def my_filters_page(request: Request):
    """«Мои фильтры»: сохранённые фильтры водителя и уведомления по ним в Telegram."""
    if not settings.telegram_login_enabled:
        return HTMLResponse("Страница не найдена", status_code=404)
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/filters")

    async with SessionLocal() as session:
        subs = await list_subscriptions(session, logged_in_telegram_id(request))
        counts = await queries.header_counts(session, token)

    html = templates.TemplateResponse(
        "filters.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "filters_items": [
                {
                    "sub": sub, "lines": describe_filters(sub.params), "link": feed_path(sub.params),
                    "edit_link": feed_path({**sub.params, "edit": sub.id, "filters_open": 1}),
                }
                for sub in subs
            ],
            "max_filters": settings.max_filters_per_driver,
            "bot_url": f"https://t.me/{settings.telegram_login_bot_username.strip().lstrip('@')}",
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.post("/filters/{sub_id}/{action}")
async def my_filter_action(request: Request, sub_id: int, action: str):
    """Кнопки на странице «Мои фильтры»: включить / выключить уведомления, удалить фильтр."""
    _token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, "/filters")
    if action not in ("on", "off", "delete"):
        return HTMLResponse("Страница не найдена", status_code=404)

    telegram_id = logged_in_telegram_id(request)
    if action == "on" and not await _has_subscription(request):
        return _redirect("/subscription", new_token, queries.ActionResult(False, "no_subscription").message)
    async with SessionLocal() as session:
        known = {s.id: s for s in await list_subscriptions(session, telegram_id)}
        summary = "; ".join(describe_filters(known[sub_id].params)) if sub_id in known else ""
        if action == "delete":
            done = await delete_subscription(session, telegram_id, sub_id)
            notice = "Фильтр удалён." if done else ""
            if done:
                await user_events.record(user_events.FILTER_DELETED, telegram_id, summary)
        else:
            sub = await set_subscription_active(session, telegram_id, sub_id, action == "on")
            notice = "" if sub is None else (
                "Уведомления по фильтру включены." if action == "on" else "Уведомления по фильтру выключены."
            )
            if sub is not None:
                await user_events.record(
                    user_events.FILTER_ON if action == "on" else user_events.FILTER_OFF, telegram_id, summary
                )
    return _redirect("/filters", new_token, notice)


@router.post("/notifications/read")
async def notifications_read(request: Request):
    """Колокольчик открыли: всё, что было, считается прочитанным."""
    telegram_id = logged_in_telegram_id(request)
    if telegram_id is not None:
        async with SessionLocal() as session:
            await notifications.mark_seen(session, telegram_id)
    return Response(status_code=204)


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
        subs = await list_subscriptions(session, driver.telegram_id)

    html = templates.TemplateResponse(
        "profile.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "driver": driver,
            "stats": stats,
            "recent": recent,
            "filters_total": len(subs),
            "filters_active": sum(1 for sub in subs if sub.is_active),
        },
    )
    attach_driver_cookie(html, new_token)
    clear_flash(html)
    return html


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int, back: str = ""):
    token, new_token, login_required = resolve_driver(request)
    if login_required:
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
        # Админ видит любой заказ целиком — и снятый, и уже взятый другим.
        # Действий (договориться, написать) в этом режиме нет: карточка нужна
        # для разбора, а не чтобы действовать от имени водителя.
        admin_view = (
            is_admin(request)
            and not is_mine
            and (order.status in HIDDEN_STATUSES or bool(order.taken_by_token))
        )
        if order.status in HIDDEN_STATUSES and not is_mine and not admin_view:
            # Протухшая/снятая/закрытая заявка пропала из ленты — по прямой
            # ссылке её тоже не показываем. Свою историю водитель видит.
            gone = templates.TemplateResponse(
                "order_gone.html",
                _base_context(request, counts) | {"order_id": order_id, "back": back},
                status_code=410,
            )
            attach_driver_cookie(gone, new_token)
            return gone
        group_name = await queries.group_username(session, order.source_chat_id)
        link = dispatcher_link(order, group_username=group_name)
        # Телефон/имя клиента готовим здесь, а не в шаблоне: шаблон не должен
        # знать, кому разрешено видеть персональные данные.
        phone = client_phone_for(order, is_owner=is_mine or admin_view)
        client_name = client_name_for(order, is_owner=is_mine or admin_view)
        # Оригинал заявки тоже содержит телефон и имя — показывать его
        # посторонним без вымарывания значит обойти маскирование целиком.
        raw_text = redact_raw_text(order, is_owner=is_mine or admin_view)

    html = templates.TemplateResponse(
        "order_detail.html",
        _base_context(request, counts, pop_flash(request))
        | {
            "order": order,
            "is_mine": is_mine,
            "admin_view": admin_view,
            "is_taken_by_someone_else": bool(order.taken_by_token) and not is_mine,
            "dispatcher_url": link,
            "group_is_public": bool(group_name),
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


async def _has_subscription(request: Request) -> bool:
    """Есть ли у вошедшего водителя подписка (пробный период или оплата). Без входа проверять нечего."""
    telegram_id = logged_in_telegram_id(request)
    if telegram_id is None:
        return True
    async with SessionLocal() as session:
        return await billing.has_access_for(session, telegram_id)


def _no_subscription(order_id: Optional[int], back: str, new_token: Optional[str]) -> Response:
    """Функция закрыта: подписка закончилась. Ведём на страницу оплаты с понятным сообщением."""
    return _redirect("/subscription", new_token, queries.ActionResult(False, "no_subscription").message)


@router.post("/orders/{order_id}/take")
async def take_order(request: Request, order_id: int, back: str = ""):
    """«Взять заказ» без перехода в Telegram (оставлено для прямых ссылок)."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    if not await _has_subscription(request):
        return _no_subscription(order_id, back, new_token)

    async with SessionLocal() as session:
        result = await queries.take_order(session, order_id, token)

    return _redirect(
        _order_url(order_id, back), new_token,
        "" if result.ok else result.message,
    )


@router.post("/orders/{order_id}/favorite")
async def toggle_favorite(request: Request, order_id: int, back: str = ""):
    """Сердечко: добавить заказ в «Избранное» или убрать. Для fetch из ленты отвечает JSON, иначе перенаправляет."""
    token, new_token, login_required = resolve_driver(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if login_required:
        if wants_json:
            return JSONResponse({"error": "login"}, status_code=401)
        return _login_redirect(request, _order_url(order_id, back))

    async with SessionLocal() as session:
        state = await favorites.toggle_favorite(session, token, order_id)
    if state is None:
        return JSONResponse({"error": "not_found"}, status_code=404) if wants_json else HTMLResponse(
            "Заказ не найден", status_code=404
        )
    if wants_json:
        response = JSONResponse({"favorite": state}, headers={"Cache-Control": "no-store"})
        attach_driver_cookie(response, new_token)
        return response
    return _redirect(_order_url(order_id, back) if back else "/my", new_token)


@router.post("/orders/{order_id}/contact")
async def contact_dispatcher(request: Request, order_id: int, back: str = ""):
    """«Написать диспетчеру»: открывает чат с готовым текстом. Заказ при этом НЕ
    берётся — «моим» он становится только после «Договорился с диспетчером»."""
    token, new_token, login_required = resolve_driver(request)
    if login_required:
        return _login_redirect(request, _order_url(order_id, back))

    if not await _has_subscription(request):
        return _no_subscription(order_id, back, new_token)

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        group_name = await queries.group_username(session, order.source_chat_id) if order else None
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

    link = dispatcher_link(order, text=dispatcher_message(order), group_username=group_name)
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

    if not await _has_subscription(request):
        return _no_subscription(order_id, back, new_token)

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
