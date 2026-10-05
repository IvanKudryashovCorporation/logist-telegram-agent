"""Вход через Telegram Login Widget.

Маршруты:

* ``GET /login`` — страница входа: основной способ — через бота (см.
  :mod:`app.web.bot_login`), запасной — виджет Telegram.
* ``POST /auth/bot/start`` и ``GET /auth/bot/status`` — вход через бота.
* ``GET /auth/telegram`` — сюда виджет редиректит браузер после подтверждения
  в Telegram; проверяем подпись, заводим/обновляем :class:`Driver`, ставим
  подписанную сессию.
* ``POST /auth/logout`` — выход.

Работает, только если :data:`app.config.settings.telegram_login_enabled` —
иначе ``/login`` отдаёт объяснение, а не форму (виджет всё равно не заработает
без домена и токена бота).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import settings
from app.db.base import SessionLocal
from app.web import bot_login, queries
from app.web.deps import (
    clear_driver_session_cookie,
    is_admin,
    logged_in_telegram_id,
    set_driver_session_cookie,
)
from app.web.telegram_login import verify_telegram_login
from app.web.templates_env import templates

log = logging.getLogger("web.auth")

router = APIRouter()


def _safe_next(raw: str) -> str:
    """Только локальный путь — иначе /login?next=https://evil.example открыл
    бы открытый редирект после входа."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if logged_in_telegram_id(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "is_admin": is_admin(request),
            "telegram_login_enabled": settings.telegram_login_enabled,
            "bot_username": settings.telegram_login_bot_username,
            "next": _safe_next(next),
        },
    )


@router.get("/auth/telegram")
async def telegram_callback(request: Request):
    """Редирект от виджета: все поля данных Telegram приходят query-параметрами."""
    if not settings.telegram_login_enabled:
        return HTMLResponse("Вход через Telegram сейчас недоступен.", status_code=404)

    params = dict(request.query_params)
    next_url = _safe_next(params.pop("next", "/"))

    if not verify_telegram_login(params, settings.telegram_login_bot_token):
        log.warning("Отклонён вход: неверная или просроченная подпись Telegram-виджета")
        return HTMLResponse(
            "Не удалось подтвердить вход через Telegram — подпись неверна "
            "или ссылка устарела. Попробуйте войти ещё раз.",
            status_code=400,
        )

    async with SessionLocal() as session:
        driver = await queries.upsert_driver(session, params)

    response = RedirectResponse(next_url, status_code=303)
    set_driver_session_cookie(response, driver.telegram_id)
    log.info("Вход: водитель tg:%s (%s)", driver.telegram_id, driver.display_name)
    return response


_NO_STORE = {"Cache-Control": "no-store"}


@router.post("/auth/bot/start")
async def bot_login_start(next: str = "/"):
    """Заводит одноразовый вход через бота: токен и ссылку на бота с этим токеном."""
    if not settings.telegram_login_enabled:
        return JSONResponse({"error": "disabled"}, status_code=404, headers=_NO_STORE)
    item = bot_login.store.create(_safe_next(next))
    return JSONResponse(
        {"token": item.token, "url": bot_login.bot_link(item.token)}, headers=_NO_STORE
    )


@router.get("/auth/bot/status")
async def bot_login_status(token: str = ""):
    """Опрос страницей входа: ждём бота → просим подтвердить → готово (ставим сессию)."""
    if not settings.telegram_login_enabled:
        return JSONResponse({"status": "expired"}, status_code=404, headers=_NO_STORE)

    item = bot_login.store.get(token)
    if item is None:
        return JSONResponse({"status": "expired"}, headers=_NO_STORE)
    if item.state != bot_login.CONFIRMED:
        return JSONResponse({"status": item.state}, headers=_NO_STORE)

    confirmed = bot_login.store.take_confirmed(token)
    if confirmed is None:  # другой запрос успел забрать вход
        return JSONResponse({"status": "expired"}, headers=_NO_STORE)

    async with SessionLocal() as session:
        driver = await queries.upsert_driver(session, confirmed.user)
    response = JSONResponse({"status": "done", "next": confirmed.next_url}, headers=_NO_STORE)
    set_driver_session_cookie(response, driver.telegram_id)
    log.info("Вход через бота: водитель tg:%s (%s)", driver.telegram_id, driver.display_name)
    return response


@router.get("/auth/bot/enter")
async def bot_login_enter(token: str = ""):
    """Кнопка «На сайт» из сообщения бота: входит в том браузере, где её открыли.

    Нужна, потому что ссылка из Telegram чаще всего открывается во встроенном
    браузере, а не там, где человек начинал вход, — и без этого сайт открылся бы
    без входа. Токен одноразовый и приходит только подтвердившему вход.
    """
    if not settings.telegram_login_enabled:
        return RedirectResponse("/", status_code=303)

    confirmed = bot_login.store.take_by_enter(token)
    if confirmed is None:
        # Ссылка устарела или уже использована. Если этот браузер уже вошёл,
        # /login сразу перекинет на сайт, иначе покажет страницу входа.
        return RedirectResponse("/login", status_code=303, headers=_NO_STORE)

    async with SessionLocal() as session:
        driver = await queries.upsert_driver(session, confirmed.user)
    response = RedirectResponse(confirmed.next_url, status_code=303, headers=_NO_STORE)
    set_driver_session_cookie(response, driver.telegram_id)
    log.info("Вход через бота (кнопка «На сайт»): водитель tg:%s", driver.telegram_id)
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse("/", status_code=303)
    clear_driver_session_cookie(response)
    return response
