"""FastAPI-приложение публичной витрины заказов.

Здесь только сборка: middleware, статика, служебные маршруты и подключение
роутеров. Сами роуты — в :mod:`app.web.routes` (сайт) и
:mod:`app.web.admin` (админка), запросы — в :mod:`app.web.queries`.

Раньше весь веб-слой (594 строки) жил в одном файле вперемешку: фильтры,
форматирование дат, SQL, cookie и пять роутов. Его было невозможно
протестировать по частям, поэтому файл разобран на модули по ответственности.

Запуск по-прежнему ``python webapp.py`` — строка ``app.web.server:app``
не изменилась.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.db.base import SessionLocal
from app.web import admin as admin_module
from app.web import auth as auth_module
from app.web import routes as public_routes
from app.web.rate_limit import RateLimitMiddleware

log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Сайт отдаёт HTML, а не API, поэтому интерактивная документация не нужна —
#: а вот публичный /docs с перечнем всех эндпоинтов (включая админку) не нужен
#: тем более.
_DOCS_DISABLED = {"docs_url": None, "redoc_url": None, "openapi_url": None}

_ROBOTS_TXT = """User-agent: *
Disallow: /
"""


class SecurityHeadersMiddleware:
    """Минимальный набор заголовков безопасности.

    Сайт публичный и открывается в том числе внутри WebView Telegram, поэтому:
    * ``nosniff`` — браузер не будет угадывать тип содержимого;
    * ``DENY`` для фреймов — защиту от кликджекинга на кнопке «Взять заказ»;
    * ``Referrer-Policy`` — не передаём URL заявок посторонним сайтам;
    * ``HSTS`` — только когда реально работаем по HTTPS.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = dict(message.setdefault("headers", []))
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"same-origin",
                }
                if settings.web_host not in {"127.0.0.1", "localhost", "::1"}:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                for name, value in extra.items():
                    if name not in headers:
                        message["headers"].append((name, value))
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app() -> FastAPI:
    application = FastAPI(title="Лента заказов — заказы для водителей", **_DOCS_DISABLED)

    # Порядок важен: middleware, добавленные позже, выполняются раньше.
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    application.include_router(public_routes.router)
    application.include_router(auth_module.router)
    application.include_router(admin_module.router)

    @application.get("/healthz")
    async def healthz() -> Response:
        """Для мониторинга и для балансировщика: жив ли процесс и БД.

        Отдаёт 503, если БД недоступна, — иначе «процесс отвечает, но база
        лежит» выглядит для мониторинга как норма.
        """
        database_ok = True
        error = ""
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 — статус важнее деталей
            database_ok = False
            error = f"{type(exc).__name__}: {exc}"
            log.error("healthz: БД недоступна: %s", error)

        payload = {
            "status": "ok" if database_ok else "degraded",
            "database": database_ok,
            "rate_limit": settings.rate_limit_enabled,
        }
        if error:
            payload["error"] = error
        return JSONResponse(payload, status_code=200 if database_ok else 503)

    @application.get("/robots.txt", response_class=PlainTextResponse)
    async def robots_txt() -> str:
        """Запрещаем индексацию целиком.

        Лента содержит телефоны клиентов и аккаунты диспетчеров; поисковику
        там делать нечего, а вот скрейперам — очень даже.
        """
        return _ROBOTS_TXT

    @application.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> Response:
        """Единый обработчик: логируем стектрейс и отдаём внятную страницу.

        Без него FastAPI возвращает голый «Internal Server Error», а причина
        остаётся только в логе uvicorn — легко пропустить, что сайт сломан.
        """
        log.exception("Необработанная ошибка на %s %s", request.method, request.url.path)
        return PlainTextResponse(
            "Внутренняя ошибка сервера. Попробуйте обновить страницу.", status_code=500
        )

    return application


app = create_app()
