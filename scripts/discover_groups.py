"""Поиск новых публичных групп с потоком заказов, которые можно читать без вступления.

    python -m scripts.discover_groups --session acc_4077 --known known.json --out found.json
    python -m scripts.discover_groups --session acc_4077 --known known.json --queries "такси межгород,трансфер"

Что делает: ищет публичные чаты глобальным поиском Telegram по ключевым словам, отбрасывает уже известные
(``--known`` — JSON ``{"ids": [...], "usernames": [...]}`` с группами из ``work_groups``), для каждого кандидата
читает последние сообщения БЕЗ вступления и оценивает, сколько в них заявок в сутки (по тем же правилам, что
префильтр агента: есть маршрут/цена, это не предложение водителя). Результат — JSON, отсортированный по потоку.

Безопасность: работает с КОПИЕЙ файла сессии и ``receive_updates=False`` (рабочий агент не затрагивается), ничего не
пишет и никуда не вступает; пауза между запросами; при длинном FloodWait останавливается.
"""

import argparse
import asyncio
import json
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.city_aliases import CITY_ALIASES
from app.config import settings
from app.parsing.not_orders import is_driver_offer
from app.parsing.prefilter import prefilter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Слова, по которым чат вообще может быть про заказы такси/трансфера (название или описание).
RELEVANT_RE = re.compile(
    r"такси|taxi|трансфер|transfer|межгород|перевозк|пассажир|водител|заказ|поездк|аэропорт|попутч",
    re.IGNORECASE,
)
#: Подработки и вакансии: в таксистских чатах их постит спам, на заявки они не похожи, хотя в них есть цена.
JOB_SPAM_RE = re.compile(
    r"шабашк|за выход|грузчик|маляр|курьер|вакан|подработк|без опыта|набор (?:людей|сотрудник)|участник массовк|"
    r"помощник на|контролёр|контролер|ищем (?:людей|сотрудник)|работа на|в лс|в личку|"
    r"\bсвободен\b|\bсвободна\b|катаюсь|ищу заказ|возьму заказ|готов(?:а)? ехать",
    re.IGNORECASE,
)
#: Слова «откуда … куда» как явный признак маршрута.
FROM_TO_RE = re.compile(r"откуда.+куда|куда.+откуда", re.IGNORECASE | re.DOTALL)
#: «Город - Город» с заглавных букв через тире или стрелку: маршрут и для городов, которых нет в нашем списке.
ROUTE_PAIR_RE = re.compile(r"[А-ЯЁ][а-яё]{3,}\s*(?:[-–—]|→|->|=>)\s*[А-ЯЁ][а-яё]{3,}")
#: Известные написания городов (как у префильтра агента): по ним считаем, сколько разных городов в сообщении.
_CITY_TOKENS = sorted(
    {t.lower() for t in CITY_ALIASES} | {c.lower() for c in CITY_ALIASES.values()}, key=len, reverse=True
)

BASE_QUERIES = [
    "такси межгород", "трансфер заказы", "межгород заказы", "заказы такси", "такси заявки", "биржа заказов такси",
    "диспетчер такси", "заказы для водителей", "такси новые территории", "такси днр лнр", "такси крым",
    "трансфер россия", "такси россия", "межгород россия водители", "отдам заказ такси", "заказы трансфер аэропорт",
    "такси опт заказы", "перевозки пассажиров заказы", "заказы межгород крым", "заказы межгород москва",
]
CITIES = [
    "Москва", "Санкт-Петербург", "Краснодар", "Ростов", "Сочи", "Симферополь", "Севастополь", "Новосибирск",
    "Екатеринбург", "Казань", "Самара", "Воронеж", "Нижний Новгород", "Минеральные Воды", "Уфа", "Челябинск",
    "Волгоград", "Белгород", "Курск", "Тула", "Анапа", "Ставрополь", "Пермь", "Тюмень",
]


@dataclass
class Candidate:
    id: int
    username: str
    title: str
    kind: str  # группа | канал
    members: Optional[int] = None
    join_request: bool = False
    readable: bool = False
    error: str = ""
    sample: int = 0
    orders_per_day: float = 0.0
    orders_in_sample: int = 0
    last_message: str = ""
    example: str = ""
    queries: list[str] = field(default_factory=list)


def distinct_cities(text: str) -> int:
    """Сколько РАЗНЫХ городов названо в тексте (псевдонимы одного города считаются за один)."""
    lowered = text.lower()
    found = set()
    for token in _CITY_TOKENS:
        if len(token) >= 4 and token in lowered:
            found.add(CITY_ALIASES.get(token, token).lower())
    return len(found)


def looks_like_order(text: Optional[str]) -> bool:
    """Похоже ли сообщение на заявку: маршрут (два города, «Город - Город» или «откуда/куда»), не вакансия
    и не предложение водителя."""
    text = (text or "").strip()
    if len(text) < 25 or is_driver_offer(text) or JOB_SPAM_RE.search(text):
        return False
    if not prefilter(text).send_to_llm:
        return False
    return distinct_cities(text) >= 2 or bool(FROM_TO_RE.search(text)) or bool(ROUTE_PAIR_RE.search(text))


def estimate_orders_per_day(
    dates: Iterable[datetime], texts: Iterable[Optional[str]], now: datetime
) -> tuple[float, int]:
    """Оценка числа заявок в сутки по последним сообщениям: ``(в сутки, заявок в выборке)``.

    Если выборка покрывает меньше суток, ставку экстраполируем на сутки; если больше — берём ровно последние 24 часа."""
    pairs = [(d, t) for d, t in zip(dates, texts, strict=False) if d is not None]
    if not pairs:
        return 0.0, 0
    oldest = min(d for d, _ in pairs)
    span = now - oldest
    day = timedelta(hours=24)
    orders = [d for d, t in pairs if looks_like_order(t)]
    if span >= day:
        return float(sum(1 for d in orders if now - d <= day)), len(orders)
    seconds = max(span.total_seconds(), 600.0)  # меньше 10 минут не экстраполируем: выйдет бессмыслица
    return round(len(orders) * 86400.0 / seconds, 1), len(orders)


def relevant(title: str, about: str = "") -> bool:
    return bool(RELEVANT_RE.search(f"{title} {about}"))


def all_queries(extra: Optional[list[str]] = None) -> list[str]:
    queries = list(extra) if extra else list(BASE_QUERIES)
    if not extra:
        for city in CITIES:
            queries.append(f"такси межгород {city}")
    return queries


def load_known(path: Optional[Path]) -> tuple[set[int], set[str]]:
    if path is None or not path.exists():
        return set(), set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("ids", [])), {str(u).lower().lstrip("@") for u in data.get("usernames", []) if u}


async def search_candidates(client, queries: list[str], known_ids: set[int], known_names: set[str], delay: float):
    """Глобальный поиск по ключевым словам. Возвращает словарь id -> (Channel, [запросы])."""
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel
    from telethon.utils import get_peer_id

    found: dict[int, tuple[Channel, list[str]]] = {}
    for query in queries:
        try:
            result = await client(SearchRequest(q=query, limit=50))
        except FloodWaitError as exc:
            print(f"FloodWait {exc.seconds}s на поиске «{query}»")
            if exc.seconds > 120:
                print("Слишком долго — останавливаюсь, показываю то, что уже нашли.")
                break
            await asyncio.sleep(exc.seconds + 2)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"Поиск «{query}»: {type(exc).__name__}")
            continue
        for chat in result.chats:
            if not isinstance(chat, Channel) or not chat.username:
                continue
            peer_id = get_peer_id(chat)
            if peer_id in known_ids or chat.username.lower() in known_names:
                continue
            if getattr(chat, "scam", False) or getattr(chat, "fake", False):
                continue
            if not relevant(chat.title or ""):
                continue
            entry = found.setdefault(peer_id, (chat, []))
            entry[1].append(query)
        await asyncio.sleep(delay)
    return found


async def inspect_candidates(client, found, delay: float, per_chat: int) -> list[Candidate]:
    """Читает последние сообщения каждого кандидата без вступления и оценивает поток заявок."""
    from telethon.errors import FloodWaitError, RPCError

    now = datetime.now(timezone.utc)
    out: list[Candidate] = []
    for peer_id, (chat, queries) in found.items():
        item = Candidate(
            id=peer_id, username=chat.username, title=chat.title or chat.username,
            kind="канал" if getattr(chat, "broadcast", False) else "группа",
            members=getattr(chat, "participants_count", None), join_request=bool(getattr(chat, "join_request", False)),
            queries=queries[:3],
        )
        try:
            messages = await client.get_messages(chat, limit=per_chat)
            item.readable = True
        except FloodWaitError as exc:
            print(f"FloodWait {exc.seconds}s на чтении @{chat.username}")
            if exc.seconds > 120:
                out.append(item)
                break
            await asyncio.sleep(exc.seconds + 2)
            out.append(item)
            continue
        except RPCError as exc:
            item.error = type(exc).__name__
            out.append(item)
            await asyncio.sleep(delay)
            continue
        item.sample = len(messages)
        if messages:
            dates = [m.date for m in messages]
            texts = [m.raw_text for m in messages]
            item.orders_per_day, item.orders_in_sample = estimate_orders_per_day(dates, texts, now)
            item.last_message = max(dates).isoformat()
            example = next((m.raw_text for m in messages if looks_like_order(m.raw_text)), "")
            item.example = re.sub(r"\s+", " ", example)[:160]
        out.append(item)
        await asyncio.sleep(delay)
    return out


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Лучшие сверху: читаются без вступления, больше заявок в сутки, свежее последнее сообщение."""
    return sorted(
        (c for c in candidates if c.readable and c.sample),
        key=lambda c: (c.orders_per_day, c.members or 0),
        reverse=True,
    )


#: Фразы, которыми диспетчеры пишут заявки: по ним ищем чаты, где заявки реально публикуют.
ORDER_PHRASES = [
    "пишите время подачи", "сейчас пишем время подачи", "водителю на руки", "на руки водителю",
    "время подачи откуда куда", "заказ межгород пассажиров на руки", "кто свободен заберёт заказ",
    "отдаю заказ межгород", "заявка трансфер аэропорт пассажир", "на сейчас откуда куда пассажиров",
    "срочный заказ межгород", "заказ на завтра межгород",
]


async def search_by_messages(client, phrases: list[str], known_ids: set[int], known_names: set[str], delay: float,
                             pages: int = 2):
    """Глобальный поиск по ТЕКСТУ сообщений: находит публичные чаты, где такие заявки действительно пишут.

    Чат попадает в результат, только если найденное сообщение само похоже на заявку."""
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import Channel, InputMessagesFilterEmpty, InputPeerEmpty
    from telethon.utils import get_peer_id

    found = {}
    for phrase in phrases:
        offset_rate, offset_peer, offset_id = 0, InputPeerEmpty(), 0
        for _ in range(pages):
            try:
                result = await client(SearchGlobalRequest(
                    q=phrase, filter=InputMessagesFilterEmpty(), min_date=None, max_date=None,
                    offset_rate=offset_rate, offset_peer=offset_peer, offset_id=offset_id, limit=100,
                ))
            except FloodWaitError as exc:
                print(f"FloodWait {exc.seconds}s на «{phrase}»")
                if exc.seconds > 120:
                    return found
                await asyncio.sleep(exc.seconds + 2)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"Поиск по тексту «{phrase}»: {type(exc).__name__}")
                break
            chats = {c.id: c for c in result.chats if isinstance(c, Channel)}
            for message in result.messages:
                chat = chats.get(getattr(message.peer_id, "channel_id", None))
                if chat is None or not chat.username or not looks_like_order(message.message):
                    continue
                peer_id = get_peer_id(chat)
                if peer_id in known_ids or chat.username.lower() in known_names:
                    continue
                found.setdefault(peer_id, (chat, []))[1].append(phrase)
            if not getattr(result, "next_rate", None) or not result.messages:
                break
            last = result.messages[-1]
            offset_rate, offset_id = result.next_rate, last.id
            offset_peer = await client.get_input_entity(last.peer_id)
            await asyncio.sleep(delay)
        await asyncio.sleep(delay)
    return found


async def entities_for(client, usernames: list[str], delay: float):
    """Находит чаты по username (для повторной проверки уже найденных): ``id -> (чат, [])``."""
    from telethon.errors import FloodWaitError
    from telethon.utils import get_peer_id

    found = {}
    for name in usernames:
        try:
            chat = await client.get_entity(name)
        except FloodWaitError as exc:
            print(f"FloodWait {exc.seconds}s на @{name}")
            if exc.seconds > 120:
                break
            await asyncio.sleep(exc.seconds + 2)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"@{name}: {type(exc).__name__}")
            continue
        found[get_peer_id(chat)] = (chat, [])
        await asyncio.sleep(delay)
    return found


async def main(args: argparse.Namespace) -> None:
    from telethon import TelegramClient

    source = PROJECT_ROOT / f"{args.session}.session"
    if not source.exists():
        raise SystemExit(f"Нет файла сессии {source.name}")
    known_ids, known_names = load_known(args.known)
    queries = all_queries([q.strip() for q in args.queries.split(",") if q.strip()] if args.queries else None)
    print(f"Известных групп: {len(known_ids)}; запросов поиска: {len(queries)}")

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "discover"
        shutil.copy(source, f"{copy}.session")
        client = TelegramClient(str(copy), settings.tg_api_id, settings.tg_api_hash, receive_updates=False)
        await client.connect()
        try:
            if args.phrases:
                found = await search_by_messages(client, ORDER_PHRASES, known_ids, known_names, args.delay)
            elif args.only:
                names = [c["username"] for c in json.loads(args.only.read_text(encoding="utf-8"))][: args.limit]
                found = await entities_for(client, names, args.delay)
            else:
                found = await search_candidates(client, queries, known_ids, known_names, args.delay)
            print(f"Кандидатов для проверки: {len(found)}")
            batch = dict(list(found.items())[: args.limit])
            candidates = await inspect_candidates(client, batch, args.delay, args.per_chat)
        finally:
            await client.disconnect()

    ranked = rank(candidates)
    args.out.write_text(
        json.dumps([asdict(c) for c in ranked], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Читаются без вступления: {len(ranked)} из {len(candidates)}. Результат: {args.out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True, help="имя сессии, чья копия используется (acc_4077 и т.п.)")
    parser.add_argument("--known", type=Path, help="JSON с уже известными группами (ids, usernames)")
    parser.add_argument("--out", type=Path, default=Path("found_groups.json"))
    parser.add_argument("--only", type=Path, help="не искать заново, а перепроверить чаты из этого JSON")
    parser.add_argument("--phrases", action="store_true", help="искать по тексту заявок, а не по названиям")
    parser.add_argument("--queries", default="", help="свои запросы через запятую (вместо стандартных)")
    parser.add_argument("--limit", type=int, default=150, help="сколько кандидатов проверять на чтение")
    parser.add_argument("--per-chat", type=int, default=100, help="сколько последних сообщений читать у каждого")
    parser.add_argument("--delay", type=float, default=3.0, help="пауза между запросами, секунд")
    asyncio.run(main(parser.parse_args()))
