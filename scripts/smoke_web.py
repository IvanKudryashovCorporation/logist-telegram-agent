"""Живой smoke-тест сайта: поднимает uvicorn и проверяет маршруты по настоящему HTTP.

    python -m scripts.smoke_web                       # свой сервер на тестовой БД
    python -m scripts.smoke_web --url http://127.0.0.1:8000   # проверить уже запущенный

Зачем отдельный скрипт, если есть pytest: тесты гоняют приложение через
``httpx.ASGITransport``, то есть без сокета и без реального кодирования
HTTP-заголовков. Именно на живом сервере видно то, что в тестах не воспроизводится:

* ``Set-Cookie`` с русским текстом проходит latin-1 (flash-сообщения);
* статика реально отдаётся с диска с правильным content-type;
* uvicorn способен импортировать ``app.web.server:app``;
* middleware безопасности дописывает заголовки в настоящем ASGI-конвейере.

В режиме ``--url`` данные не создаются и не изменяются — только чтение, поэтому
скрипт безопасен для работающего прода (кроме одного POST-входа в админку,
который выполняется лишь при переданном ``--admin-password``).
"""

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

# ВАЖНО: окружение выставляется до импорта app.* — settings и движок БД
# создаются один раз при импорте модуля.
_TEST_DB = Path(__file__).resolve().parent.parent / "_smoke_web.db"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _prepare_env(port: int) -> dict[str, str]:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
    os.environ["WEB_HOST"] = "127.0.0.1"
    os.environ["WEB_PORT"] = str(port)
    os.environ["ADMIN_PASSWORD"] = "smoke-admin-password"
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    os.environ["MASK_CLIENT_CONTACTS"] = "true"
    os.environ["NOTIFY_CHAT_ID"] = ""
    os.environ["WEB_PAGE_SIZE"] = "20"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _seed() -> int:
    """Создаёт схему и одну заявку, возвращает её id."""
    from app.db.base import Base, SessionLocal, engine
    from app.models import Order, OrderStatus
    from app.search import refresh_derived
    from app.timeutil import now_msk_naive

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    pickup = now_msk_naive() + timedelta(days=1, hours=3)
    order = Order(
        source_chat_id=-100999888777,
        source_message_id=1,
        source_sub_index=0,
        dispatcher_tg_id=111,
        dispatcher_username="smoke_dispatcher",
        raw_text=f"{pickup:%d.%m %H:%M} Симферополь — Сочи, 2 пасс., 14000, "
        f"Иван Петров +79990000000",
        pickup_at=pickup,
        from_city="Симферополь",
        to_city="Сочи",
        from_address="Симферополь, ул. Ленина 1",
        to_address="Сочи, аэропорт",
        passengers=2,
        client_name="Иван Петров",
        client_phone="+79990000000",
        client_price=Decimal("14000"),
        status=OrderStatus.NEW,
    )
    refresh_derived(order)
    async with SessionLocal() as session:
        session.add(order)
        await session.commit()
        return int(order.id)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        self.checks += 1
        mark = "OK " if condition else "НЕТ"
        suffix = f" — {detail}" if detail and not condition else ""
        print(f"  [{mark}] {label}{suffix}")
        if not condition:
            self.failures.append(label)


def _wait_ready(base_url: str, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.3)
    return False


def check_read_only(base_url: str, report: Report, order_id: int | None) -> None:
    """Проверки, которые ничего не меняют — безопасны и для работающего прода."""
    import httpx

    print("\n=== Служебные маршруты ===")
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        health = client.get("/healthz")
        report.check(health.status_code == 200, "GET /healthz -> 200", str(health.status_code))
        if health.status_code == 200:
            payload = health.json()
            report.check(payload.get("database") is True, "healthz: БД доступна")
            report.check(payload.get("status") == "ok", "healthz: status == ok", str(payload))

        robots = client.get("/robots.txt")
        report.check(robots.status_code == 200, "GET /robots.txt -> 200")
        report.check("Disallow: /" in robots.text, "robots.txt запрещает индексацию")

        report.check(client.get("/docs").status_code == 404, "GET /docs -> 404 (API закрыто)")
        report.check(client.get("/нет-такой-страницы").status_code == 404, "неизвестный маршрут -> 404")

        print("\n=== Статика ===")
        for path, marker, ctype in (
            ("/static/style.css", ".card", "text/css"),
            ("/static/app.js", "addEventListener", "javascript"),
            ("/static/admin.css", ".admin-nav", "text/css"),
        ):
            response = client.get(path)
            report.check(response.status_code == 200, f"GET {path} -> 200", str(response.status_code))
            report.check(marker in response.text, f"{path}: содержимое на месте")
            actual = response.headers.get("content-type", "")
            report.check(ctype in actual, f"{path}: content-type ~ {ctype!r}", actual)

        print("\n=== Лента и заголовки безопасности ===")
        feed = client.get("/")
        report.check(feed.status_code == 200, "GET / -> 200", str(feed.status_code))
        report.check(
            bool(client.cookies.get("driver_id")), "лента выдаёт анонимную cookie водителя"
        )
        for header, expected in (
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "same-origin"),
        ):
            actual = feed.headers.get(header, "<нет>")
            report.check(actual == expected, f"заголовок {header}: {expected}", actual)

    if order_id is None:
        print("\n(заявка не создавалась — проверки карточки пропущены)")
        return

    print("\n=== Карточка заказа и маскирование контактов ===")
    with httpx.Client(base_url=base_url, timeout=10.0) as stranger:
        page = stranger.get(f"/orders/{order_id}")
        report.check(page.status_code == 200, f"GET /orders/{order_id} -> 200")
        report.check("+79990000000" not in page.text, "телефон клиента скрыт от посторонних")
        report.check("Петров" not in page.text, "имя клиента скрыто от посторонних")
        report.check("***-***-**00" in page.text, "вместо телефона показана маска")
        report.check("Симферополь" in page.text, "маршрут виден")


def check_driver_actions(base_url: str, report: Report, order_id: int) -> None:
    """Действия водителя — меняют данные, поэтому только на своей тестовой БД."""
    import httpx

    print("\n=== Действия водителя (реальный сокет, latin-1 в заголовках) ===")
    driver_a = httpx.Client(base_url=base_url, timeout=10.0)
    driver_a.cookies.set("driver_id", "smoke-driver-a")
    take = driver_a.post(f"/orders/{order_id}/take")
    report.check(take.status_code == 303, "POST /orders/{id}/take -> 303", str(take.status_code))

    mine = driver_a.get(f"/orders/{order_id}")
    report.check("+79990000000" in mine.text, "взявший заказ видит полный телефон")
    report.check("Иван Петров" in mine.text, "взявший заказ видит полное имя")

    my_page = driver_a.get("/my")
    report.check(my_page.status_code == 200, "GET /my -> 200")
    report.check(f"/orders/{order_id}" in my_page.text, "заказ виден в «Моих заказах»")

    # Ключевая проверка именно живого сервера: русское flash-сообщение уходит в
    # Set-Cookie, а заголовки HTTP кодируются в latin-1. Через ASGITransport это
    # не воспроизводится — там заголовки остаются строками и не кодируются.
    driver_b = httpx.Client(base_url=base_url, timeout=10.0)
    driver_b.cookies.set("driver_id", "smoke-driver-b")
    refused = driver_b.post(f"/orders/{order_id}/take")
    report.check(
        refused.status_code == 303, "второй водитель: 303, а не 500", str(refused.status_code)
    )
    set_cookie = refused.headers.get("set-cookie", "")
    report.check("flash_notice=" in set_cookie, "выдана cookie с уведомлением")
    report.check(set_cookie.isascii(), "значение Set-Cookie в latin-1", set_cookie[:120])
    after = driver_b.get(f"/orders/{order_id}")
    report.check(after.status_code == 200, "карточка после отказа -> 200")
    report.check("другой водитель" in after.text, "водитель видит причину отказа")

    feedback = driver_a.post(
        f"/orders/{order_id}/feedback",
        data={"reason": "price_outdated", "note": "проверка живой формы"},
    )
    report.check(feedback.status_code == 303, "POST /orders/{id}/feedback -> 303")
    driver_a.close()
    driver_b.close()


def check_admin(base_url: str, report: Report, admin_password: str, order_id: int | None) -> None:
    """Вход в админку, сводка, список заказов, очередь, выход."""
    import httpx

    print("\n=== Админка ===")
    admin = httpx.Client(base_url=base_url, timeout=10.0)
    guarded = admin.get("/admin")
    report.check(guarded.status_code == 303, "GET /admin без входа -> 303", str(guarded.status_code))
    location = guarded.headers.get("location", "<нет>")
    report.check(location == "/admin/login", "редирект на /admin/login", location)

    login_page = admin.get("/admin/login")
    report.check(login_page.status_code == 200, "GET /admin/login -> 200")
    report.check('type="password"' in login_page.text, "форма входа с полем пароля")

    bad = admin.post("/admin/login", data={"password": "неверный-пароль"})
    report.check(bad.status_code == 200, "неверный пароль -> страница входа (200)")
    report.check("Неверный пароль" in bad.text, "сообщение об ошибке входа")
    report.check(not admin.cookies.get("admin_session"), "cookie админки не выдана")

    good = admin.post("/admin/login", data={"password": admin_password})
    report.check(good.status_code == 303, "верный пароль -> 303", str(good.status_code))
    report.check(bool(admin.cookies.get("admin_session")), "выдана подписанная cookie")

    dashboard = admin.get("/admin")
    report.check(dashboard.status_code == 200, "GET /admin -> 200", str(dashboard.status_code))
    for marker in ("всего заказов", "Качество разбора", "Очередь повторного разбора", "Жалобы"):
        report.check(marker in dashboard.text, f"сводка содержит «{marker}»")

    orders_page = admin.get("/admin/orders")
    report.check(orders_page.status_code == 200, "GET /admin/orders -> 200")
    if order_id is not None:
        report.check(f"/orders/{order_id}" in orders_page.text, "в админке видна заявка")
    report.check(
        admin.get("/admin/queue?status=failed").status_code == 200, "GET /admin/queue -> 200"
    )

    report.check(admin.post("/admin/logout").status_code == 303, "POST /admin/logout -> 303")
    report.check(admin.get("/admin").status_code == 303, "после выхода админка снова закрыта")
    admin.close()


def _finish(report: Report) -> int:
    print(f"\nПроверок: {report.checks}, провалено: {len(report.failures)}")
    if report.failures:
        print("Провалены:")
        for name in report.failures:
            print(f"  ! {name}")
        return 1
    print("Сайт работает.")
    return 0


async def _run_hosted(port: int) -> int:
    """Поднимает свой uvicorn на отдельной тестовой БД и прогоняет все проверки."""
    if _TEST_DB.exists():
        _TEST_DB.unlink()
    env = _prepare_env(port)
    base_url = f"http://127.0.0.1:{port}"

    order_id = await _seed()
    print(f"Тестовая БД: {_TEST_DB.name}, заявка #{order_id}, адрес {base_url}")

    # Отдельный процесс, а не uvicorn в потоке: проверяется ровно то, что
    # запускается на сервере, включая импорт "app.web.server:app".
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.web.server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    report = Report()
    try:
        if not _wait_ready(base_url):
            print("Сервер не поднялся за 30 секунд — проверка прервана.")
            return 1
        print("uvicorn поднялся, /healthz отвечает.")
        check_read_only(base_url, report, order_id)
        check_driver_actions(base_url, report, order_id)
        check_admin(base_url, report, env["ADMIN_PASSWORD"], order_id)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        if _TEST_DB.exists():
            _TEST_DB.unlink()
    return _finish(report)


def _run_external(base_url: str, admin_password: str | None, order_id: int | None) -> int:
    """Проверяет уже запущенный сайт. По умолчанию — только чтение."""
    report = Report()
    if not _wait_ready(base_url, timeout=5.0):
        print(f"{base_url} не отвечает на /healthz — проверять нечего.")
        return 1

    check_read_only(base_url, report, order_id)
    if admin_password:
        # Вход в админку и действия водителя меняют состояние, поэтому без
        # явного пароля (и id заявки) они не выполняются.
        if order_id is not None:
            check_driver_actions(base_url, report, order_id)
        check_admin(base_url, report, admin_password, order_id)
    else:
        print("\n(админка не проверялась: не передан --admin-password)")
    return _finish(report)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", default=None,
        help="проверить уже запущенный сайт вместо поднятия своего",
    )
    parser.add_argument(
        "--order-id", type=int, default=None,
        help="id существующей заявки — для проверок карточки при --url",
    )
    parser.add_argument(
        "--admin-password", default=None,
        help="пароль админки при --url (без него проверяется только чтение)",
    )
    args = parser.parse_args()

    if args.url:
        return _run_external(args.url.rstrip("/"), args.admin_password, args.order_id)
    return asyncio.run(_run_hosted(_free_port()))


if __name__ == "__main__":
    sys.exit(main())
