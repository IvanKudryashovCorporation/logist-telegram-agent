"""Вход через Telegram-бота: «Старт» в боте вместо виджета Telegram.

Виджет входа запоминает в браузере аккаунт, которым входили раньше, и после
выхода снова предлагает «Войти как …» — завести новый аккаунт через него
нельзя. Вход через бота этой привязки не имеет: человек сам открывает бота в
том Telegram, в котором хочет войти.

Как это работает:

1. Страница ``/login`` просит у сайта одноразовый вход (``POST /auth/bot/start``)
   и получает секретный токен и ссылку ``t.me/<бот>?start=<токен>``.
2. Человек открывает бота, жмёт «Старт» — бот получает токен и **кто** нажал
   (это сообщает сам Telegram, подделать нельзя) и просит подтвердить вход.
3. После нажатия «Войти» в боте страница (она опрашивает
   ``GET /auth/bot/status``) получает сессию и переходит дальше.

Хранилище — в памяти процесса сайта: вход живёт минуты, а при перезапуске
сайта человеку достаточно нажать «Войти» ещё раз. Опрос Telegram
(``getUpdates``) идёт в том же процессе, поэтому токены общие. Подтверждение
кнопкой нужно, чтобы чужая ссылка не входила за человека молча: тот, кто
получил ссылку от злоумышленника, увидит запрос и может нажать «Отмена».
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.web import bot_orders

log = logging.getLogger("web.bot_login")

#: Сколько живёт непотверждённый вход.
LOGIN_TTL_SECONDS = 600
#: Сколько входов держим одновременно (защита памяти от флуда ``/auth/bot/start``).
MAX_PENDING = 2000

WAITING, ASKED, CONFIRMED = "waiting", "asked", "confirmed"

#: ``api(method, payload) -> result`` — вызов Bot API; в тестах подменяется.
BotApi = Callable[[str, dict], Awaitable[Optional[dict]]]


@dataclass
class PendingLogin:
    token: str
    next_url: str
    created_at: float
    state: str = WAITING
    user: dict = field(default_factory=dict)  # id/first_name/last_name/username из Telegram
    #: Одноразовый токен кнопки «На сайт» в сообщении бота: открывает сайт сразу
    #: залогиненным в том браузере, где нажали (например во встроенном в Telegram).
    enter_token: str = ""
    poll_claimed: bool = False
    enter_claimed: bool = False


class LoginStore:
    def __init__(self, ttl: float = LOGIN_TTL_SECONDS, max_pending: int = MAX_PENDING) -> None:
        self.ttl = ttl
        self.max_pending = max_pending
        self._items: dict[str, PendingLogin] = {}
        self._by_enter: dict[str, str] = {}

    def _drop(self, token: str) -> None:
        item = self._items.pop(token, None)
        if item is not None and item.enter_token:
            self._by_enter.pop(item.enter_token, None)

    def _purge(self, now: float) -> None:
        for token in [t for t, item in self._items.items() if now - item.created_at > self.ttl]:
            self._drop(token)
        while len(self._items) >= self.max_pending:  # словарь хранит порядок добавления
            self._drop(next(iter(self._items)))

    def create(self, next_url: str = "/", *, now: Optional[float] = None) -> PendingLogin:
        moment = time.monotonic() if now is None else now
        self._purge(moment)
        # Начинается с буквы, длина 33: укладывается в 64 символа параметра /start.
        token = "L" + secrets.token_urlsafe(24)
        item = PendingLogin(token=token, next_url=next_url, created_at=moment)
        self._items[token] = item
        return item

    def get(self, token: str, *, now: Optional[float] = None) -> Optional[PendingLogin]:
        moment = time.monotonic() if now is None else now
        item = self._items.get(token or "")
        if item is None:
            return None
        if moment - item.created_at > self.ttl:
            self._drop(token)
            return None
        return item

    def discard(self, token: str) -> None:
        self._drop(token)

    def confirm(self, item: PendingLogin) -> str:
        """Помечает вход подтверждённым и выдаёт токен для кнопки «На сайт»."""
        item.state = CONFIRMED
        item.enter_token = "E" + secrets.token_urlsafe(24)
        self._by_enter[item.enter_token] = item.token
        return item.enter_token

    def _claimed(self, item: PendingLogin) -> None:
        if item.poll_claimed and item.enter_claimed:
            self._drop(item.token)

    def take_confirmed(self, token: str, *, now: Optional[float] = None) -> Optional[PendingLogin]:
        """Отдаёт подтверждённый вход странице входа ОДИН раз."""
        item = self.get(token, now=now)
        if item is None or item.state != CONFIRMED or item.poll_claimed:
            return None
        item.poll_claimed = True
        self._claimed(item)
        return item

    def take_by_enter(self, enter_token: str, *, now: Optional[float] = None) -> Optional[PendingLogin]:
        """Вход по кнопке «На сайт» из бота — тоже ОДИН раз."""
        item = self.get(self._by_enter.get(enter_token or "", ""), now=now)
        if item is None or item.state != CONFIRMED or item.enter_claimed:
            return None
        item.enter_claimed = True
        self._claimed(item)
        return item


store = LoginStore()


def _user_info(raw: dict) -> dict:
    return {
        "id": int(raw["id"]),
        "first_name": raw.get("first_name") or "",
        "last_name": raw.get("last_name") or "",
        "username": raw.get("username") or "",
    }


_WELCOME = (
    "Это бот сайта «Лента заказов».\n"
    "Чтобы войти на сайт, нажмите на нём «Войти через Telegram» и откройте ссылку на бота. "
    "Сюда же приходят уведомления по вашим подпискам."
)
_EXPIRED = "Ссылка для входа устарела. Нажмите «Войти через Telegram» на сайте ещё раз."
_ASK = (
    "Вход на сайт «Лента заказов».\n\n"
    "Подтверждайте, только если вы сами только что нажали «Войти» на сайте. "
    "Если не нажимали — нажмите «Отмена»."
)
_DONE = (
    "Готово, вы вошли. Нажмите «На сайт» — он откроется сразу под вашим аккаунтом. "
    "Если вход начинали в другом браузере, просто вернитесь в него: страница откроется сама."
)
_CANCELLED = "Вход отменён."


async def handle_update(update: dict, api: BotApi, login_store: LoginStore = store) -> None:
    """Обрабатывает одно обновление бота."""
    message = update.get("message")
    if message and message.get("from") and not message["from"].get("is_bot"):
        await _handle_message(message, api, login_store)
        return
    callback = update.get("callback_query")
    if callback and callback.get("from") and not callback["from"].get("is_bot"):
        if str(callback.get("data", "")).startswith(bot_orders.TAKE_PREFIX):
            await bot_orders.handle_take(callback, api)  # «Взять заказ» под уведомлением
            return
        await _handle_callback(callback, api, login_store)


async def _handle_message(message: dict, api: BotApi, login_store: LoginStore) -> None:
    text = (message.get("text") or "").strip()
    chat_id = message["chat"]["id"]
    if message["chat"].get("type") != "private" or not text.startswith("/start"):
        return

    token = text[len("/start"):].strip()
    if not token:
        await api("sendMessage", {"chat_id": chat_id, "text": _WELCOME})
        return

    item = login_store.get(token)
    if item is None or item.state == CONFIRMED:
        await api("sendMessage", {"chat_id": chat_id, "text": _EXPIRED})
        return

    item.user = _user_info(message["from"])
    item.state = ASKED
    await api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": _ASK,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Войти", "callback_data": f"ok:{token}"},
                        {"text": "Отмена", "callback_data": f"no:{token}"},
                    ]
                ]
            },
        },
    )


async def _handle_callback(callback: dict, api: BotApi, login_store: LoginStore) -> None:
    data = callback.get("data") or ""
    action, _, token = data.partition(":")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    message_id = callback.get("message", {}).get("message_id")
    item = login_store.get(token)

    async def reply(text: str, markup: Optional[dict] = None) -> None:
        await api("answerCallbackQuery", {"callback_query_id": callback["id"]})
        if chat_id is not None and message_id is not None:
            payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
            if markup:
                payload["reply_markup"] = markup
            await api("editMessageText", payload)

    # Подтвердить может только тот же человек, который нажал «Старт» с этим токеном.
    if (
        action not in ("ok", "no")
        or item is None
        or item.state != ASKED
        or item.user.get("id") != callback["from"]["id"]
    ):
        await reply(_EXPIRED)
        return

    if action == "no":
        login_store.discard(token)
        await reply(_CANCELLED)
        return

    enter_token = login_store.confirm(item)
    site_button = {
        "inline_keyboard": [
            [{"text": "На сайт", "url": f"{settings.public_base_url.rstrip('/')}/auth/bot/enter?token={enter_token}"}]
        ]
    }
    await reply(_DONE, site_button)


# --- Опрос Telegram (getUpdates) ---------------------------------------------------


def make_api(token: str, client: httpx.AsyncClient) -> BotApi:
    async def api(method: str, payload: dict) -> Optional[dict]:
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=40
            )
        except httpx.HTTPError as exc:
            log.warning("Бот входа: Telegram недоступен (%s): %s", method, type(exc).__name__)
            return None
        if response.status_code != 200:
            log.warning("Бот входа: %s -> %s %s", method, response.status_code, response.text[:120])
            return None
        return response.json().get("result")

    return api


async def run_bot_poller(stop_event: Optional[asyncio.Event] = None) -> None:
    """Принимает сообщения бота входа, пока не остановят. Любая ошибка — лог и пауза."""
    token = settings.telegram_login_bot_token.strip()
    offset = 0
    async with httpx.AsyncClient(trust_env=False) as client:
        api = make_api(token, client)
        log.info("Вход через бота @%s: слушаю сообщения", settings.telegram_login_bot_username)
        while stop_event is None or not stop_event.is_set():
            updates = await api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            if updates is None:
                # 409 — бота читает ещё кто-то (старый процесс при перезапуске или
                # вебхук); сеть пропала — тоже сюда. Не долбим Telegram.
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                try:
                    await handle_update(update, api)
                except Exception:
                    log.exception("Бот входа: ошибка в обновлении %s", update.get("update_id"))


def bot_link(token: str) -> str:
    return f"https://t.me/{settings.telegram_login_bot_username.strip().lstrip('@')}?start={token}"
