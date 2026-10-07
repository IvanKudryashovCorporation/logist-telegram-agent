"""Резервная копия базы вне сервера (в Telegram, зашифрованная). Подробности — app/services/offsite_backup.py.

    python -m scripts.offsite_backup --init   # один раз: создать пароль и прислать его владельцу
    python -m scripts.offsite_backup          # отправить последний дамп (его запускает cron)
    # cron (раз в сутки, после ночного дампа в 03:15):
    #   30 3 * * * cd /root/logist-agent && .venv/bin/python -m scripts.offsite_backup >> /root/backups/offsite.log 2>&1

Нужны в .env: ALERT_CHAT_ID и токен бота (как у сторожа), а в системе — gpg.
"""

import argparse
import asyncio
import logging
import sys

from app.services import offsite_backup


async def main(init: bool) -> int:
    if init:
        return 0 if await offsite_backup.init_passphrase() else 1
    ok, message = await offsite_backup.run_once()
    print(("OK " if ok else "ОШИБКА ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="создать пароль шифрования и прислать его владельцу")
    sys.exit(asyncio.run(main(parser.parse_args().init)))
