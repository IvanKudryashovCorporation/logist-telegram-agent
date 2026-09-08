"""Точка входа веб-панели (Этап 5). Отдельный процесс от Telegram-агента,
общая с ним БД.

    python webapp.py

Без авторизации по решению из опроса — при выкладке на VPS ограничьте
доступ на уровне сети (firewall/VPN), не открывайте порт наружу напрямую.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.web.server:app", host="127.0.0.1", port=8000, reload=True)
