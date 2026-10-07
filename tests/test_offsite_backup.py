"""Копия базы вне сервера: шифруется, уходит владельцу, сторож следит, что она свежая."""

import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services import offsite_backup, watchdog

NOW = datetime(2026, 10, 5, 12, 0)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(settings, "watchdog_backup_dir", str(backups))
    monkeypatch.setattr(settings, "offsite_backup_passphrase_path", str(tmp_path / "passphrase"))
    return backups


def _dump(directory, name="logist-20261005-031501.sql.gz", content=b"dump-data"):
    path = directory / name
    path.write_bytes(content)
    return path


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.files: list[tuple[str, str, bytes]] = []

    async def __call__(self, path, caption):
        self.files.append((path.name, caption, path.read_bytes()))
        return self.ok


async def _fake_encrypt(source, target, passphrase_file):
    target.write_bytes(b"ENC:" + source.read_bytes())


# --- Пароль ---------------------------------------------------------------------------------------------


async def test_init_creates_a_private_passphrase_and_sends_it_to_the_owner():
    messages = []

    async def send(text):
        messages.append(text)
        return True

    assert await offsite_backup.init_passphrase(send_message=send) is True

    secret = offsite_backup.passphrase_path().read_text().strip()
    assert len(secret) >= 24
    assert secret in messages[0] and "менеджер паролей" in messages[0]
    assert oct(offsite_backup.passphrase_path().stat().st_mode & 0o777) in ("0o600", "0o666")  # Windows не знает 600


async def test_init_does_not_replace_an_existing_passphrase():
    offsite_backup.passphrase_path().write_text("старый\n")

    async def send(text):
        raise AssertionError("пароль не должен пересылаться повторно")

    assert await offsite_backup.init_passphrase(send_message=send) is True
    assert offsite_backup.passphrase_path().read_text() == "старый\n"


async def test_passphrase_is_removed_if_the_owner_did_not_get_it():
    async def send(text):
        return False

    assert await offsite_backup.init_passphrase(send_message=send) is False
    assert not offsite_backup.passphrase_path().exists()  # копии под неизвестным паролем не нужны


# --- Отправка -------------------------------------------------------------------------------------------


async def test_latest_dump_is_encrypted_and_sent(isolated):
    offsite_backup.passphrase_path().write_text("secret\n")
    _dump(isolated, "logist-20261004-031501.sql.gz", b"old")
    _dump(isolated, "logist-20261005-031501.sql.gz", b"new")
    sender = FakeSender()

    ok, message = await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt)

    assert ok and "logist-20261005-031501.sql.gz" in message
    name, caption, body = sender.files[0]
    assert name == "logist-20261005-031501.sql.gz.gpg" and body == b"ENC:new"  # не открытый дамп
    assert "sha256" in caption
    assert offsite_backup.load_state()["last_file"] == "logist-20261005-031501.sql.gz"


async def test_the_same_dump_is_not_sent_twice(isolated):
    offsite_backup.passphrase_path().write_text("secret\n")
    _dump(isolated)
    sender = FakeSender()
    await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt)

    ok, message = await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt)

    assert ok and "уже отправлен" in message
    assert len(sender.files) == 1


async def test_failed_delivery_is_retried_next_time(isolated):
    offsite_backup.passphrase_path().write_text("secret\n")
    _dump(isolated)

    ok, message = await offsite_backup.run_once(now=NOW, send_file=FakeSender(ok=False), encrypt_file=_fake_encrypt)

    assert not ok and "не принял" in message
    assert offsite_backup.load_state() == {}  # не помечено отправленным
    sender = FakeSender()
    assert (await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt))[0]
    assert len(sender.files) == 1


async def test_without_a_passphrase_nothing_is_sent(isolated):
    _dump(isolated)
    sender = FakeSender()

    ok, message = await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt)

    assert not ok and "--init" in message and sender.files == []


async def test_no_dump_is_reported(isolated):
    offsite_backup.passphrase_path().write_text("secret\n")

    ok, message = await offsite_backup.run_once(now=NOW, send_file=FakeSender(), encrypt_file=_fake_encrypt)

    assert not ok and "нет ни одного дампа" in message


async def test_file_over_the_telegram_limit_is_not_sent(isolated, monkeypatch):
    offsite_backup.passphrase_path().write_text("secret\n")
    _dump(isolated)
    monkeypatch.setattr(offsite_backup, "MAX_FILE_BYTES", 3)
    sender = FakeSender()

    ok, message = await offsite_backup.run_once(now=NOW, send_file=sender, encrypt_file=_fake_encrypt)

    assert not ok and "лимит" in message and sender.files == []


async def test_encryption_failure_is_reported_not_raised(isolated):
    offsite_backup.passphrase_path().write_text("secret\n")
    _dump(isolated)

    async def broken(source, target, passphrase_file):
        raise RuntimeError("gpg: нет такого файла")

    ok, message = await offsite_backup.run_once(now=NOW, send_file=FakeSender(), encrypt_file=broken)

    assert not ok and "зашифровать" in message


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg не установлен")
async def test_real_gpg_roundtrip(tmp_path):
    """Настоящий gpg: файл расшифровывается тем же паролем и не читается без него."""
    source = tmp_path / "dump.sql.gz"
    source.write_bytes(b"clients: +79990000000")
    passphrase = tmp_path / "pass"
    passphrase.write_text("correct horse\n")
    target = tmp_path / "dump.sql.gz.gpg"

    await offsite_backup.encrypt(source, target, passphrase)

    assert b"+79990000000" not in target.read_bytes()
    decrypted = subprocess.run(
        ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase-file", str(passphrase), "--decrypt", str(target)],
        capture_output=True,
    )
    assert decrypted.stdout == b"clients: +79990000000"


# --- Сторож ---------------------------------------------------------------------------------------------


def test_watchdog_ignores_an_unconfigured_offsite_backup():
    check = offsite_backup.check_offsite_backup(now=NOW)

    assert check.ok and "не настроена" in check.detail


def test_watchdog_flags_a_backup_that_was_never_sent():
    offsite_backup.passphrase_path().write_text("secret\n")

    assert not offsite_backup.check_offsite_backup(now=NOW).ok


def test_watchdog_accepts_a_fresh_offsite_backup_and_flags_a_stale_one():
    offsite_backup.passphrase_path().write_text("secret\n")
    offsite_backup._save_state({"last_file": "x", "sent_at": (NOW - timedelta(hours=5)).isoformat()})
    assert offsite_backup.check_offsite_backup(now=NOW).ok

    offsite_backup._save_state({"last_file": "x", "sent_at": (NOW - timedelta(hours=30)).isoformat()})
    stale = offsite_backup.check_offsite_backup(now=NOW)
    assert not stale.ok and "30 ч" in stale.detail


async def test_offsite_check_is_part_of_the_watchdog_run(monkeypatch):
    async def no_services():
        return []

    async def site_ok():
        return watchdog.Check("сайт", True)

    monkeypatch.setattr(watchdog, "check_services", no_services)
    monkeypatch.setattr(watchdog, "check_site", site_ok)

    names = [c.name for c in await watchdog.run_checks()]

    assert "копия базы вне сервера" in names
