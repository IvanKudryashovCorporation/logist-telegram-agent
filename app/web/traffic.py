"""Откуда посетители приходят на сайт: источник заходов для страницы ``/admin/traffic``.

Пишется одна строка на сеанс (первая страница после паузы в :data:`SESSION_GAP_SECONDS`). Источник определяется по
метке в адресе (``utm_source``, ``src``, ``ref``), затем по заголовку Referer, затем по браузеру (встроенный
браузер Telegram) и, если ничего нет, это «прямой заход». Роботы поисковиков и скрипты не считаются.

Куки не ставим: посетителя опознаёт хэш «день + IP + браузер», который каждый день меняется. Сами IP и User-Agent
в базу не попадают.
"""

import asyncio
import hashlib
import logging
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from starlette.requests import Request

from app.config import settings
from app.db.base import SessionLocal
from app.models import SiteVisit
from app.timeutil import now_utc_naive
from app.web.deps import client_ip, logged_in_telegram_id

log = logging.getLogger("web.traffic")

#: Новый заход, если с прошлой страницы этого посетителя прошло не меньше стольких секунд.
SESSION_GAP_SECONDS = 30 * 60
#: Служебные адреса: заходом на сайт не считаются.
_IGNORED_PREFIXES = ("/static/", "/healthz", "/robots.txt", "/sitemap.xml", "/favicon.ico",
                     "/manifest.webmanifest", "/sw.js", "/payments/", "/admin", "/auth/")
_MAX_TRACKED = 50_000

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|preview|monitor|uptime|curl|wget|python|httpx|go-http|java/|"
    r"headless|lighthouse|scrapy|okhttp|libwww|ahrefs|semrush",
    re.IGNORECASE,
)
_MOBILE_RE = re.compile(r"mobi|android|iphone|ipad|ipod", re.IGNORECASE)

DIRECT = "direct"
TELEGRAM = "telegram"
PWA = "pwa"
OTHER_SITE = "site"

#: Домены -> код источника. Ищется по концу домена, поэтому ``m.vk.com`` и ``away.vk.com`` тоже подходят.
_HOSTS = (
    (("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru", "yandex.net"), "yandex"),
    (("google.com", "google.ru", "google.by", "google.kz", "googleusercontent.com"), "google"),
    (("t.me", "telegram.org", "telegram.me", "telegram.dog", "tgstat.ru", "tgstat.com", "telemetr.io"), TELEGRAM),
    (("vk.com", "vk.ru", "vkontakte.ru", "vk.cc"), "vk"),
    (("ok.ru",), "ok"),
    (("mail.ru",), "mailru"),
    (("bing.com", "duckduckgo.com", "yahoo.com", "ecosia.org", "brave.com"), "search_other"),
    (("youtube.com", "youtu.be"), "youtube"),
    (("instagram.com", "facebook.com", "fb.com"), "social_other"),
    (("wa.me", "whatsapp.com"), "whatsapp"),
    (("avito.ru",), "avito"),
)

#: Подписи для админки.
SOURCE_LABELS = {
    DIRECT: "Прямой заход (адрес, закладка)",
    TELEGRAM: "Telegram",
    PWA: "Иконка на экране телефона",
    OTHER_SITE: "Другие сайты",
    "yandex": "Яндекс",
    "google": "Google",
    "vk": "ВКонтакте",
    "ok": "Одноклассники",
    "mailru": "Mail.ru",
    "search_other": "Другие поисковики",
    "youtube": "YouTube",
    "social_other": "Instagram / Facebook",
    "whatsapp": "WhatsApp",
    "avito": "Авито",
}

#: Что из этого — поисковая выдача (для сводки «поиск / соцсети / напрямую»).
SEARCH_SOURCES = {"yandex", "google", "mailru", "search_other"}


def source_label(source: str) -> str:
    if source.startswith("utm:"):
        return f"Метка: {source[4:]}"
    return SOURCE_LABELS.get(source, source)


def is_bot(user_agent: str) -> bool:
    """Пустой User-Agent и роботы не считаются посетителями."""
    return not user_agent or bool(_BOT_RE.search(user_agent))


def device_of(user_agent: str) -> str:
    if "telegram" in user_agent.lower():
        return "telegram"
    return "mobile" if _MOBILE_RE.search(user_agent) else "desktop"


def _clean(value: Optional[str], limit: int) -> Optional[str]:
    value = (value or "").strip().lower()
    return value[:limit] or None


def _host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _source_for_host(host: str) -> str:
    for domains, code in _HOSTS:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return code
    return OTHER_SITE


def classify(*, referer: str, query: dict, user_agent: str, own_host: str) -> Optional[dict]:
    """Источник захода или ``None``, если это переход внутри сайта (заходом не считается)."""
    host = _host_of(referer) if referer else ""
    if host and own_host and (host == own_host or host.endswith("." + own_host)):
        return None

    utm_source = _clean(query.get("utm_source"), 64)
    utm_medium = _clean(query.get("utm_medium"), 64)
    utm_campaign = _clean(query.get("utm_campaign"), 64)
    tag = utm_source or _clean(query.get("src"), 48) or _clean(query.get("ref"), 48)

    if tag == PWA:
        source = PWA
    elif tag:
        # Метка из ссылки, которую владелец сам расставил (канал, чат, реклама): в отчёте она отдельной строкой.
        source = f"utm:{tag[:44]}"
    elif host:
        source = _source_for_host(host)
    elif "telegram" in user_agent.lower():
        source = TELEGRAM
    else:
        source = DIRECT

    return {
        "source": source,
        "referrer_host": host[:128] or None,
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": utm_campaign,
    }


def _digest(*parts: str) -> str:
    material = "|".join((settings.signing_secret, *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def visitor_hashes(ip: str, user_agent: str, *, day: str) -> tuple[str, str]:
    """``(visitor, ip_hash)``: с браузером и без; оба привязаны к дню и сменятся завтра."""
    return _digest(day, ip, user_agent), _digest(day, ip)


_last_seen: dict[tuple[str, int], float] = {}


def is_new_session(visitor: str, telegram_id: Optional[int], *, now: Optional[float] = None) -> bool:
    """True, если пора записать заход (и запоминает время).

    Вошедший и невошедший — разные сеансы: так вход через Telegram даёт отдельную строку с ``telegram_id``,
    и по ней регистрацию можно отнести к источнику.
    """
    now = time.monotonic() if now is None else now
    key = (visitor, telegram_id or 0)
    previous = _last_seen.get(key)
    if previous is not None and now - previous < SESSION_GAP_SECONDS:
        return False
    if len(_last_seen) >= _MAX_TRACKED:
        _last_seen.clear()
    _last_seen[key] = now
    return True


async def record(visit: SiteVisit) -> None:
    """Пишет заход; сбой не должен мешать странице."""
    try:
        async with SessionLocal() as session:
            session.add(visit)
            await session.commit()
    except Exception:
        log.warning("Не удалось записать заход на сайт", exc_info=True)


class TrafficMiddleware:
    """Фиксирует начало сеанса посетителя и его источник."""

    def __init__(self, app) -> None:
        self.app = app
        self._tasks: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "GET" and not scope["path"].startswith(_IGNORED_PREFIXES):
            try:
                self._track(Request(scope))
            except Exception:
                log.warning("Не удалось разобрать источник захода", exc_info=True)
        await self.app(scope, receive, send)

    def _track(self, request: Request) -> None:
        headers = request.headers
        # Страницы открывает браузер (text/html); фоновые запросы ленты и prefetch заходом не считаются.
        if "text/html" not in headers.get("accept", "") or headers.get("purpose") == "prefetch":
            return
        user_agent = headers.get("user-agent", "")
        if is_bot(user_agent):
            return

        own_host = _host_of(settings.public_base_url)
        info = classify(
            referer=headers.get("referer", ""), query=dict(request.query_params),
            user_agent=user_agent, own_host=own_host,
        )
        if info is None:
            return

        now = now_utc_naive()
        visitor, ip_hash = visitor_hashes(client_ip(request), user_agent, day=now.strftime("%Y%m%d"))
        telegram_id = logged_in_telegram_id(request)
        if not is_new_session(visitor, telegram_id):
            return

        visit = SiteVisit(
            **info, landing=request.url.path[:96] or "/", device=device_of(user_agent),
            visitor=visitor, ip_hash=ip_hash, telegram_id=telegram_id,
        )
        task = asyncio.create_task(record(visit))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
