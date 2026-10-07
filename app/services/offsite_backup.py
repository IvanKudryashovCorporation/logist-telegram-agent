"""Резервная копия базы вне сервера: зашифрованный дамп уходит владельцу в Telegram.

Копии в ``/root/backups`` лежат на том же диске, что и база: если сервер пропадёт, пропадут
и они. Поэтому после ночного дампа (``deploy/backup-db.sh``) последний файл шифруется паролем
(gpg, AES-256) и отправляется ботом в личные сообщения владельца (``ALERT_CHAT_ID``). Шифровать
нужно, потому что в дампе телефоны клиентов и аккаунты водителей, а Telegram хранит файлы в
облаке: без пароля прочитать файл не сможет никто, включая Telegram.

Пароль создаётся один раз командой ``--init`` и **только** присылается владельцу тем же ботом, в
логи и в терминал он не попадает. Если не сохранить его отдельно (менеджер паролей), копии
нельзя будет расшифровать. Файл копии Telegram хранит у себя, лимит бота на загрузку — 50 МБ
(сейчас дамп меньше мегабайта).

Восстановление:  ``gpg --decrypt logist-….sql.gz.gpg | gunzip | psql <строка подключения>``.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import socket
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.services import watchdog
from app.timeutil import now_utc_naive

log = logging.getLogger("app.offsite_backup")

#: Лимит Bot API на загрузку файла — 50 МБ; оставляем запас на служебные данные.
MAX_FILE_BYTES = 45 * 1024 * 1024
#: Свежесть внешней копии для сторожа: ночной дамп + запас.
MAX_AGE_HOURS = 26

SendFile = Callable[[Path, str], Awaitable[bool]]
Encrypt = Callable[[Path, Path, Path], Awaitable[None]]


def passphrase_path() -> Path:
    return Path(settings.offsite_backup_passphrase_path)


def state_path() -> Path:
    return Path(settings.watchdog_backup_dir) / "offsite-state.json"


def latest_dump(directory: Optional[Path] = None) -> Optional[Path]:
    directory = directory or Path(settings.watchdog_backup_dir)
    dumps = sorted(directory.glob("logist-*.sql.gz"), key=lambda p: p.stat().st_mtime) if directory.exists() else []
    return dumps[-1] if dumps else None


def load_state(path: Optional[Path] = None) -> dict:
    try:
        return json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, path: Optional[Path] = None) -> None:
    (path or state_path()).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Шифрование ---------------------------------------------------------------------


async def encrypt(source: Path, target: Path, passphrase_file: Path) -> None:
    """gpg --symmetric, AES-256. Пароль читается из файла, а не из аргументов (их видно в ps)."""
    proc = await asyncio.create_subprocess_exec(
        "gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
        "--symmetric", "--cipher-algo", "AES256",
        "--passphrase-file", str(passphrase_file), "--output", str(target), str(source),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"gpg завершился с кодом {proc.returncode}: {err.decode(errors='replace')[:200]}")


# --- Отправка -----------------------------------------------------------------------


async def send_document(path: Path, caption: str) -> bool:
    token, chat_id = watchdog.alert_credentials()
    if not token or not chat_id:
        log.error("Копию некуда отправить: не заданы ALERT_CHAT_ID / токен бота")
        return False
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (path.name, path.read_bytes(), "application/octet-stream")},
            )
    except httpx.HTTPError as exc:
        log.error("Telegram недоступен при отправке копии: %s", type(exc).__name__)
        return False
    if response.status_code != 200:
        log.error("Telegram отклонил копию: %s %s", response.status_code, response.text[:200])
    return response.status_code == 200


# --- Пароль -------------------------------------------------------------------------


async def init_passphrase(*, send_message: Callable[[str], Awaitable[bool]] = watchdog.send_telegram) -> bool:
    """Создаёт пароль шифрования и присылает его владельцу. Уже созданный не трогает.

    Если сообщение не дошло, пароль удаляется: копии, зашифрованные неизвестным паролем,
    хуже, чем их отсутствие."""
    path = passphrase_path()
    if path.exists():
        return True
    secret = secrets.token_urlsafe(24)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    delivered = await send_message(
        "🔐 Пароль для расшифровки резервных копий базы:\n\n"
        f"{secret}\n\n"
        "Сохраните его отдельно (менеджер паролей) и удалите это сообщение: без пароля копии не открыть. "
        "Каждые сутки сюда будет приходить файл logist-….sql.gz.gpg.\n"
        "Восстановление: gpg --decrypt файл.gpg | gunzip | psql …"
    )
    if not delivered:
        path.unlink(missing_ok=True)
        log.error("Пароль не доставлен владельцу — не создаю, чтобы не шифровать неизвестным паролем")
        return False
    log.info("Пароль шифрования создан и отправлен владельцу")
    return True


# --- Один проход --------------------------------------------------------------------


def _caption(dump: Path, encrypted: Path, digest: str) -> str:
    return (
        f"Копия базы {dump.name} · {socket.gethostname()}\n"
        f"{encrypted.stat().st_size // 1024} КБ, sha256 {digest[:16]}…\n"
        "Зашифровано паролем, который вы получили при настройке."
    )


async def run_once(
    *,
    now: Optional[datetime] = None,
    directory: Optional[Path] = None,
    send_file: SendFile = send_document,
    encrypt_file: Encrypt = encrypt,
) -> tuple[bool, str]:
    """Шифрует последний дамп и отправляет, если он ещё не отправлялся.

    Возвращает ``(успех, пояснение)``. Успех — и «отправили», и «уже отправляли этот файл»."""
    now = now or now_utc_naive()
    passphrase = passphrase_path()
    if not passphrase.exists():
        return False, "не настроено: запустите python -m scripts.offsite_backup --init"

    dump = latest_dump(directory)
    if dump is None:
        return False, "в каталоге копий нет ни одного дампа"
    state = load_state()
    if state.get("last_file") == dump.name:
        return True, f"{dump.name} уже отправлен"

    with tempfile.TemporaryDirectory() as tmp:
        encrypted = Path(tmp) / f"{dump.name}.gpg"
        try:
            await encrypt_file(dump, encrypted, passphrase)
        except (OSError, RuntimeError) as exc:
            return False, f"не удалось зашифровать: {exc}"
        size = encrypted.stat().st_size
        if size > MAX_FILE_BYTES:
            return False, f"копия {size // (1024 * 1024)} МБ не помещается в лимит Telegram (50 МБ)"
        digest = hashlib.sha256(encrypted.read_bytes()).hexdigest()
        if not await send_file(encrypted, _caption(dump, encrypted, digest)):
            return False, "Telegram не принял файл"

    _save_state({"last_file": dump.name, "sent_at": now.isoformat(), "size": size, "sha256": digest})
    return True, f"отправлен {dump.name} ({size // 1024} КБ)"


# --- Проверка для сторожа ------------------------------------------------------------


def check_offsite_backup(*, now: Optional[datetime] = None) -> watchdog.Check:
    name = "копия базы вне сервера"
    if not passphrase_path().exists():
        return watchdog.Check(name, True, "не настроена (python -m scripts.offsite_backup --init)")
    sent = load_state().get("sent_at")
    if not sent:
        return watchdog.Check(name, False, "ещё ни разу не отправлялась")
    age_hours = ((now or now_utc_naive()) - datetime.fromisoformat(sent)).total_seconds() / 3600
    return watchdog.Check(name, age_hours < MAX_AGE_HOURS, f"последняя отправка {age_hours:.0f} ч назад")
