"""Сторож: проверяет, что всё работает, и пишет владельцу в Telegram, если нет.

Запускается отдельно от агента (cron, ``python -m scripts.watchdog``): если бы
сторож жил внутри агента, то при падении агента предупреждать было бы некому.
Сообщения уходят через Bot API от имени бота — это независимо от Telethon-сессий.

Что проверяется:

* службы systemd (агент, сайт, Caddy, PostgreSQL);
* сайт отвечает 200;
* агент жив: за последние ``WATCHDOG_AGENT_SILENCE_HOURS`` часов обработано
  хотя бы одно сообщение из групп (иначе сессия слетела или агент завис);
* каждый Telegram-аккаунт отдельно: из его групп за ``WATCHDOG_ACCOUNT_SILENCE_HOURS``
  часов пришло хоть что-то (иначе у него слетела сессия, а остальные это маскируют);
* новые заявки поступают: за ``WATCHDOG_ORDERS_SILENCE_HOURS`` часов создана хотя бы одна;
* LLM не падает: мало ошибок разбора за час;
* очередь повторного разбора без «застрявших» сообщений;
* бэкап базы свежий (младше 26 часов);
* копия базы вне сервера отправлена за последние 26 часов (если настроена);
* на диске есть место.

Чтобы не спамить, о неполадке сообщается после двух проверок подряд (деплой с
перезапуском на 15 секунд тревогу не поднимает), напоминание — раз в
``WATCHDOG_REMINDER_HOURS`` часов, а когда всё починилось — сообщение «восстановлено».
"""

import asyncio
import json
import logging
import shutil
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Order, ParseOutcome, ParseStat, PendingMessage, PendingStatus, WorkGroup
from app.timeutil import now_utc_naive

log = logging.getLogger("app.watchdog")
# httpx на уровне INFO пишет в лог полный адрес запроса, а в адресе Bot API лежит
# токен бота — в логи (и cron-файл) он попадать не должен.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVICES = ("logist-agent", "logist-web", "caddy", "postgresql")
#: Сколько проверок подряд должно упасть, прежде чем поднять тревогу.
FAILS_BEFORE_ALERT = 2


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


# --- Проверки ----------------------------------------------------------------


async def _unit_state(unit: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        return "unknown"
    return out.decode().strip() or "unknown"


async def check_services() -> list[Check]:
    checks = []
    for unit in SERVICES:
        state = await _unit_state(unit)
        if state == "unknown":
            continue  # нет systemd (разработка) — проверять нечего
        checks.append(Check(f"служба {unit}", state == "active", f"состояние: {state}"))
    return checks


async def check_site() -> Check:
    url = f"http://127.0.0.1:{settings.web_port}/"
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(url)
        ok = response.status_code == 200
        return Check("сайт", ok, f"{url} -> {response.status_code}")
    except httpx.HTTPError as exc:
        return Check("сайт", False, f"{url} не отвечает ({type(exc).__name__})")


async def check_agent_alive(*, now: Optional[datetime] = None) -> Check:
    """Агент жив, если из групп приходили сообщения (любой исход разбора)."""
    now = now or now_utc_naive()
    hours = settings.watchdog_agent_silence_hours
    async with SessionLocal() as session:
        last = (await session.execute(select(func.max(ParseStat.created_at)))).scalar_one()
    if last is None:
        return Check("агент читает группы", False, "в базе нет ни одного разобранного сообщения")
    silent = now - last
    ok = silent < timedelta(hours=hours)
    minutes = int(silent.total_seconds() // 60)
    return Check(
        "агент читает группы", ok,
        f"последнее сообщение {minutes} мин назад (порог {hours} ч)",
    )


async def check_account_activity(*, now: Optional[datetime] = None) -> list[Check]:
    """Каждый Telegram-аккаунт отдельно: за последние часы из его групп пришло хоть что-то.

    Общая проверка «агент жив» зелёная, пока читает хотя бы один аккаунт. Если у другого
    слетела сессия (вышли из Telegram, аккаунт заморозили), его группы молча перестают
    попадать в ленту — это видно только по аккаунту.
    """
    now = now or now_utc_naive()
    hours = settings.watchdog_account_silence_hours
    async with SessionLocal() as session:
        groups = (
            await session.execute(
                select(WorkGroup.tg_chat_id, WorkGroup.session_name, WorkGroup.created_at).where(
                    WorkGroup.is_active.is_(True)
                )
            )
        ).all()
        by_session: dict[str, list] = {}
        for chat_id, session_name, created_at in groups:
            by_session.setdefault(session_name or settings.tg_session_name, []).append((chat_id, created_at))

        checks = []
        for name in sorted(by_session):
            chat_ids = [chat_id for chat_id, _ in by_session[name]]
            newest_group = max(created_at for _, created_at in by_session[name])
            last = (
                await session.execute(
                    select(func.max(ParseStat.created_at)).where(ParseStat.chat_id.in_(chat_ids))
                )
            ).scalar_one()
            title = f"аккаунт {name} читает группы"
            if last is None:
                # Только что подключённому аккаунту дадим время, чтобы в группах хоть что-то написали.
                fresh = now - newest_group < timedelta(hours=hours)
                checks.append(Check(title, fresh, f"из {len(chat_ids)} групп не пришло ни одного сообщения"))
                continue
            minutes = int((now - last).total_seconds() // 60)
            checks.append(
                Check(
                    title, now - last < timedelta(hours=hours),
                    f"последнее сообщение {minutes} мин назад, групп {len(chat_ids)} (порог {hours} ч)",
                )
            )
    return checks


async def check_orders_flow(*, now: Optional[datetime] = None) -> Check:
    """Новые заявки появляются: сообщения идут, а заявок нет — значит, ломается разбор."""
    now = now or now_utc_naive()
    hours = settings.watchdog_orders_silence_hours
    async with SessionLocal() as session:
        last = (await session.execute(select(func.max(Order.created_at)))).scalar_one()
    if last is None:
        return Check("новые заявки", True, "заявок ещё не было")
    minutes = int((now - last).total_seconds() // 60)
    return Check(
        "новые заявки поступают", now - last < timedelta(hours=hours),
        f"последняя заявка {minutes} мин назад (порог {hours} ч)",
    )


async def check_llm_errors(*, now: Optional[datetime] = None) -> Check:
    now = now or now_utc_naive()
    async with SessionLocal() as session:
        errors = (
            await session.execute(
                select(func.count())
                .select_from(ParseStat)
                .where(
                    ParseStat.outcome == ParseOutcome.ERROR,
                    ParseStat.created_at > now - timedelta(hours=1),
                )
            )
        ).scalar_one()
    limit = settings.watchdog_llm_errors_per_hour
    return Check("разбор заявок (LLM)", errors < limit, f"ошибок за последний час: {errors} (порог {limit})")


async def check_queue() -> Check:
    async with SessionLocal() as session:
        failed = (
            await session.execute(
                select(func.count())
                .select_from(PendingMessage)
                .where(PendingMessage.status == PendingStatus.FAILED)
            )
        ).scalar_one()
    return Check(
        "очередь разбора", failed == 0,
        f"не разобрано после всех попыток: {failed} (scripts.retry_queue)",
    )


def check_backup(*, now: Optional[datetime] = None, directory: Optional[Path] = None) -> Check:
    directory = directory or Path(settings.watchdog_backup_dir)
    files = sorted(directory.glob("logist-*.sql.gz"), key=lambda p: p.stat().st_mtime) if directory.exists() else []
    if not files:
        return Check("бэкап базы", False, f"в {directory} нет ни одного дампа")
    now_ts = (now or datetime.now()).timestamp()
    age_hours = (now_ts - files[-1].stat().st_mtime) / 3600
    return Check("бэкап базы", age_hours < 26, f"последний дамп {age_hours:.0f} ч назад")


def check_disk(*, path: str = "/") -> Check:
    usage = shutil.disk_usage(path)
    used = usage.used / usage.total * 100
    limit = settings.watchdog_disk_percent
    return Check("место на диске", used < limit, f"занято {used:.0f}% (порог {limit}%)")


async def run_checks() -> list[Check]:
    checks: list[Check] = []
    checks += await check_services()
    checks.append(await check_site())
    for factory in (check_agent_alive, check_account_activity, check_orders_flow, check_llm_errors, check_queue):
        try:
            result = await factory()
        except Exception as exc:  # noqa: BLE001 — если база недоступна, это сама по себе тревога
            checks.append(Check("база данных", False, f"запрос не удался: {type(exc).__name__}: {exc}"[:200]))
            break
        checks += result if isinstance(result, list) else [result]
    checks.append(check_backup())
    from app.services.offsite_backup import check_offsite_backup  # здесь, иначе круговой импорт

    checks.append(check_offsite_backup())
    checks.append(check_disk())
    return checks


# --- Решение «что и когда сообщать» -----------------------------------------


def decide(
    state: dict, checks: list[Check], now: datetime, *, reminder_hours: Optional[int] = None
) -> tuple[list[tuple[str, str]], dict]:
    """По результатам проверок и прошлому состоянию возвращает
    ``([(имя проверки, текст сообщения)], новое состояние)``."""
    reminder = timedelta(hours=reminder_hours or settings.watchdog_reminder_hours)
    new_state: dict = {}
    messages: list[tuple[str, str]] = []

    for check in checks:
        prev = state.get(check.name, {})
        if check.ok:
            if prev.get("alerted"):
                since = datetime.fromisoformat(prev["since"])
                minutes = max(1, int((now - since).total_seconds() // 60))
                messages.append(
                    (check.name, f"🟢 {check.name}: восстановлено (проблема длилась ~{minutes} мин)\n{check.detail}")
                )
            continue

        fails = prev.get("fails", 0) + 1
        entry = {
            "fails": fails,
            "since": prev.get("since") or now.isoformat(),
            "alerted": prev.get("alerted", False),
            "last_alert": prev.get("last_alert"),
        }
        if fails >= FAILS_BEFORE_ALERT:
            last_alert = datetime.fromisoformat(entry["last_alert"]) if entry["last_alert"] else None
            if not entry["alerted"]:
                messages.append((check.name, f"🔴 {check.name}: проблема\n{check.detail}"))
                entry.update(alerted=True, last_alert=now.isoformat())
            elif last_alert is None or now - last_alert >= reminder:
                messages.append((check.name, f"🔴 {check.name}: всё ещё не работает\n{check.detail}"))
                entry["last_alert"] = now.isoformat()
        new_state[check.name] = entry

    return messages, new_state


# --- Отправка и запуск ---------------------------------------------------------


def alert_credentials() -> tuple[str, str]:
    token = settings.alert_bot_token.strip() or settings.telegram_login_bot_token.strip()
    return token, settings.alert_chat_id.strip()


async def send_telegram(text: str, *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    default_token, default_chat = alert_credentials()
    token, chat_id = token or default_token, chat_id or default_chat
    if not token or not chat_id:
        log.error("Алерт некуда отправить: не заданы ALERT_CHAT_ID / токен бота")
        return False
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        if response.status_code != 200:
            log.error("Telegram отклонил алерт: %s %s", response.status_code, response.text[:200])
        return response.status_code == 200
    except httpx.HTTPError as exc:
        log.error("Не удалось отправить алерт: %s", exc)
        return False


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_once(
    *,
    state_path: Optional[Path] = None,
    send: Callable = send_telegram,
    now: Optional[datetime] = None,
    checks: Optional[list[Check]] = None,
) -> list[str]:
    """Один проход: проверки -> решение -> отправка. Возвращает отправленные сообщения."""
    state_path = state_path or Path(settings.watchdog_state_path)
    now = now or now_utc_naive()
    checks = checks if checks is not None else await run_checks()

    state = _load_state(state_path)
    messages, new_state = decide(state, checks, now)

    delivered: list[str] = []
    host = socket.gethostname()
    for name, message in messages:
        if await send(f"{message}\n— {host}"):
            delivered.append(message)
        elif name in state:
            # Не дошло — возвращаем прежнее состояние проверки, и следующий проход
            # попробует отправить то же сообщение ещё раз.
            new_state[name] = state[name]
        else:
            new_state.pop(name, None)
    _save_state(state_path, new_state)
    return delivered
