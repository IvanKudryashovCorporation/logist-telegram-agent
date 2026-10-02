"""Сторож: проверяет работу сервиса и пишет владельцу в Telegram, если что-то не так.

    python -m scripts.watchdog            # обычный проход (его запускает cron раз в 5 минут)
    python -m scripts.watchdog --check    # показать результаты проверок, ничего не отправляя
    python -m scripts.watchdog --test     # отправить тестовое сообщение в Telegram

Нужны в .env: ALERT_CHAT_ID (ваш id в Telegram; бот может писать только тому,
кто нажал в нём «Старт» или входил через виджет) и токен бота (по умолчанию
берётся TELEGRAM_LOGIN_BOT_TOKEN). Подробности — app/services/watchdog.py.
"""

import argparse
import asyncio
import logging
import socket
import sys

from app.services import watchdog


async def main(mode: str) -> int:
    if mode == "test":
        token, chat_id = watchdog.alert_credentials()
        if not token or not chat_id:
            print("Не заданы ALERT_CHAT_ID и/или токен бота в .env")
            return 1
        sent = await watchdog.send_telegram(f"✅ Сторож работает.\nПроверочное сообщение — {socket.gethostname()}")
        print("Сообщение отправлено." if sent else "Отправить не удалось — смотрите лог выше.")
        return 0 if sent else 1

    if mode == "check":
        failing = 0
        for check in await watchdog.run_checks():
            failing += not check.ok
            print(f"{'OK ' if check.ok else 'ПРОБЛЕМА'} {check.name}: {check.detail}")
        return 1 if failing else 0

    delivered = await watchdog.run_once()
    for message in delivered:
        print(message.replace("\n", " | "))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true")
    group.add_argument("--test", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main("test" if args.test else "check" if args.check else "run")))
