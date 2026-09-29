"""Точка входа веб-панели. Отдельный процесс от Telegram-агента, общая с ним БД.

    python webapp.py

Сайт публичный и без регистрации водителя (см. app/web/routes.py). Хост и порт —
из .env (WEB_HOST, WEB_PORT). Локально по умолчанию 127.0.0.1 (не торчит наружу);
на VPS для внешнего доступа задайте WEB_HOST=0.0.0.0 и откройте порт в файрволе.

Само приложение собирается в :func:`app.web.server.create_app`; здесь только
запуск uvicorn с параметрами из конфига.
"""

import logging

import uvicorn

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("webapp")


def run() -> None:
    log.info("Веб-панель: http://%s:%s", settings.web_host, settings.web_port)
    log.info(
        "Админка /admin: %s · rate limit: %s · маскирование контактов: %s",
        "включена" if settings.admin_enabled else "ВЫКЛЮЧЕНА (ADMIN_PASSWORD пуст)",
        "включён" if settings.rate_limit_enabled else "выключен",
        "включено" if settings.mask_client_contacts else "ВЫКЛЮЧЕНО",
    )
    if settings.web_host == "0.0.0.0" and not settings.admin_enabled:
        # Типичная ошибка при выкладке на VPS: сайт торчит наружу, а админка
        # отключена — владелец даже не может посмотреть статистику разбора.
        log.warning("WEB_HOST=0.0.0.0: сайт доступен из интернета. Проверьте файрвол и ADMIN_PASSWORD.")

    uvicorn.run(
        "app.web.server:app",
        host=settings.web_host,
        port=settings.web_port,
        # TRUST_PROXY_HEADERS=true — сайт за nginx/Caddy, и тогда реальный IP
        # клиента берётся из X-Forwarded-For (нужно лимитеру частоты запросов).
        # У uvicorn по умолчанию доверенным считается 127.0.0.1, чего хватает
        # для прокси на том же хосте; для внешнего балансировщика задайте
        # переменную окружения UVICORN_FORWARDED_ALLOW_IPS.
        proxy_headers=settings.trust_proxy_headers,
        # reload=True звучит удобно для разработки, но на практике не стоит того:
        # его watcher-процесс переживает Ctrl+C/kill родителя и продолжает держать
        # порт со старым кодом, плюс шаблоны иногда не подхватывались на лету.
        # После правок просто перезапускайте процесс вручную.
        reload=False,
    )


if __name__ == "__main__":
    run()
