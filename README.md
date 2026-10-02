# Агрегатор заказов для водителей

Юзербот на Telegram-аккаунте собирает заявки на перевозку из множества рабочих
групп диспетчеров и публикует их на публичном сайте. Цена и все параметры —
1 в 1 из заявки, без наценки. Водитель, найдя подходящий заказ, жмёт «Написать
диспетчеру» и договаривается напрямую в Telegram — сам агент в переписку не
вмешивается.

## Стек

Python 3.10+ · Telethon (юзербот-сессия, только чтение групп) · SQLAlchemy 2
(async) + Alembic · PostgreSQL в проде, SQLite локально и в тестах (переключается
строкой `DATABASE_URL`) · Claude API для разбора текста заявки · FastAPI —
публичный сайт со списком заказов.

## Установка

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Заполнить `.env`:

- `TG_API_ID` / `TG_API_HASH` — получить на https://my.telegram.org → API development tools
- `TG_PHONE` — телефон аккаунта, от лица которого агент читает группы
- `LLM_API_KEY` — ключ Claude API (имя намеренно не `ANTHROPIC_*`, см. комментарий в `app/config.py`)
- `LLM_BASE_URL` — заполнить, только если ключ выдан прокси-сервисом, а не напрямую console.anthropic.com

## Авторизация в Telegram (один раз)

```bash
.venv\Scripts\python.exe -m scripts.auth_telegram
```

Скрипт запросит код из Telegram и пароль 2FA, затем создаст `logist.session`.
Файл сессии — это доступ к аккаунту, он в `.gitignore`; не передавайте его.

## Рабочие группы диспетчеров (откуда парсятся заявки)

Список групп — в БД, можно добавить сколько угодно:

```bash
.venv\Scripts\python.exe -m scripts.manage_work_groups add @username_группы
.venv\Scripts\python.exe -m scripts.manage_work_groups add -1001234567890 "Название группы"
.venv\Scripts\python.exe -m scripts.manage_work_groups list
.venv\Scripts\python.exe -m scripts.manage_work_groups deactivate <id>
```

Добавление по `@username`/ссылке резолвит чат через живую Telethon-сессию — не
запускайте эту команду одновременно с работающим `main.py` (конфликт за файл
сессии), сначала остановите агента.

Найти id уже известных чатов:

```bash
.venv\Scripts\python.exe -m scripts.list_chats
.venv\Scripts\python.exe -m scripts.list_chats водител
```

Разобрать последние сообщения группы задним числом (агент в норме реагирует
только на новые события):

```bash
.venv\Scripts\python.exe -m scripts.backfill_orders --limit 5
```

## База данных

```bash
.venv\Scripts\python.exe -m alembic upgrade head    # применить миграции
.venv\Scripts\python.exe -m scripts.smoke_db        # проверить, что слой БД жив
```

После изменения моделей:

```bash
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "описание"
.venv\Scripts\python.exe -m alembic upgrade head
```

### PostgreSQL (прод)

С 2026-10-02 прод работает на PostgreSQL 16 на том же VPS (слушает только
`127.0.0.1`, база `logist`, `DATABASE_URL=postgresql+asyncpg://logist:...@127.0.0.1/logist`).
SQLite остаётся для разработки и тестов — код один и тот же.

* **Новая база с нуля:** `python -m scripts.init_postgres` строит схему из моделей и
  делает `alembic stamp head`. Цепочка старых миграций писалась под SQLite
  (batch-режим, `= 1` для булевых) и на PostgreSQL не воспроизводится.
* **Новые миграции** пишите переносимо (`sa.true()/sa.false()`, без SQLite-специфики) и
  проверяйте на обоих движках.
* **Перенос данных из SQLite** (агента и сайт остановить):
  `python -m scripts.sqlite_to_postgres --sqlite ./logist.db [--truncate]` — копирует
  все таблицы с теми же id, выставляет счётчики и сверяет количества строк.
* **Бэкап:** `deploy/backup-db.sh` делает `pg_dump | gzip` в `/root/backups` и хранит
  14 дней; cron `15 3 * * *`. Восстановление: `gunzip -c файл.sql.gz | psql -d база`.
* **Время** в БД везде наивное UTC: колонки `timestamp without time zone`, сессия
  с `timezone=UTC` (см. `app/db/base.py`).
* **Тесты на PostgreSQL:** `TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host/logist_test`
  `python -m pytest` (пул отключается автоматически; 11 тестов исторической
  SQLite-миграции пропускаются).

## Запуск агента

```bash
.venv\Scripts\python.exe main.py
```

Слушает рабочие группы диспетчеров и разбирает заявки в БД. Больше ничего не
делает — не публикует, не переписывается, не отвечает на сообщения.

## Веб-панель — сайт для водителей

Отдельный процесс, общая БД с агентом:

```bash
.venv\Scripts\python.exe webapp.py
```

Открыть http://127.0.0.1:8000 — список заказов (сегодня/завтра/позже/просрочено),
поиск по городу/телефону/диспетчеру, карточка заказа с кнопкой «Написать
диспетчеру» (открывает личку в Telegram с тем, кто прислал заявку и сразу
подставляет в поле ввода готовый текст: «Здравствуйте! Заказ A → B, 05.10 в
14:00, 3000 ₽ — актуально?»; для диспетчеров без @username текст копируется в
буфер — ссылка по числовому id параметр `text` не поддерживает).

На карточке две кнопки. «Написать диспетчеру» только открывает чат и заказ **не
берёт**. Заказ становится «моим» (уходит из общей ленты и появляется в «Моих
заказах») по кнопке «Договорился с диспетчером»; «Отменить» возвращает его в ленту.

### Радиус «Откуда/Куда»

В фильтрах под городом можно выбрать «+25/50/100/200 км»: в выдачу попадают и
заказы из посёлков вокруг города. Координаты проставляет фоновый геокодер агента
(Nominatim/OpenStreetMap, ~1 запрос/сек, результаты кэшируются в `geo_places`).
Сайт сам в сеть за координатами не ходит — только читает кэш. Заказ без
координат по-прежнему находится по названию, так что радиус ничего не отнимает.
Нашлось не всё (редкие названия, опечатки):

```bash
python -m scripts.geo_places missing                 # что не нашлось
python -m scripts.geo_places set "с. Титовка" 44.5 34.1   # задать вручную
```

Лента публична и без входа всегда. Карточка заказа и «Мои заказы» — тоже,
**пока не настроен вход через Telegram** (см. ниже); как только он настроен —
туда пускает только вошедших. `WEB_HOST`/`WEB_PORT` в `.env` управляют, на
каком адресе слушать (по умолчанию `127.0.0.1` — недоступно снаружи, для
реального сайта нужно `0.0.0.0`, см. деплой ниже).

## Фоновые сервисы агента

`main.py` кроме слушателя групп поднимает четыре фоновые задачи (останавливаются
общим `stop_event` по Ctrl+C):

| Задача | Зачем | Настройки |
|---|---|---|
| Воркер очереди разбора | Telethon **не переигрывает** доставленное событие. Если LLM ответила 429/таймаутом, сообщение уходило в `pending_messages`, а воркер повторяет разбор с экспоненциальной задержкой (потолок 6 ч). Без этого заявка терялась навсегда. | `QUEUE_ENABLED`, `QUEUE_MAX_ATTEMPTS`, `QUEUE_BASE_DELAY_SECONDS`, `QUEUE_POLL_SECONDS` |
| Обслуживание данных | Переводит заявки с прошедшим временем подачи в `EXPIRED` (иначе счётчики расходятся с лентой) и чистит старые `parse_stats`. Взятые водителем заказы не трогает. | `EXPIRE_GRACE_HOURS`, `EXPIRE_POLL_MINUTES`, `STATS_RETENTION_DAYS` |
| Геокодер | Проставляет заказам координаты для радиуса в фильтрах и догоняет старые заказы. Недоступность Nominatim заказ не ломает — он остаётся без координат. | `GEOCODING_ENABLED`, `GEOCODER_USER_AGENT`, `GEOCODE_POLL_SECONDS`, `GEOCODE_BATCH` |
| Уведомления владельцу | Алерт в Telegram после `NOTIFY_ERROR_THRESHOLD` ошибок разбора подряд и сообщение о старте агента. | `NOTIFY_CHAT_ID`, `NOTIFY_ON_START`, `NOTIFY_ERROR_THRESHOLD` |

## Сторож и алерты

`scripts/watchdog.py` запускается cron'ом раз в 5 минут **отдельно от агента** (иначе
при падении агента предупреждать было бы некому) и пишет вам в Telegram от имени бота.
Проверяет: службы (агент, сайт, Caddy, PostgreSQL), ответ сайта, что агент читает группы
(за 3 часа есть хотя бы одно сообщение), число ошибок LLM за час, «застрявшие» сообщения
очереди, свежесть бэкапа (младше 26 ч) и место на диске.

О проблеме сообщает после двух проверок подряд (деплой с перезапуском тревогу не
поднимает), напоминает раз в 6 часов и пишет «восстановлено», когда всё починилось.

```bash
python -m scripts.watchdog --check   # показать результаты проверок, ничего не отправляя
python -m scripts.watchdog --test    # отправить тестовое сообщение
# cron: */5 * * * * cd /root/logist-agent && .venv/bin/python -m scripts.watchdog >> /root/backups/watchdog.log 2>&1
```

Настройки — `ALERT_CHAT_ID` и `WATCHDOG_*` в `.env` (см. `.env.example`).

## Админка владельца — /admin

Включается, только если задан `ADMIN_PASSWORD`; иначе все её маршруты отдают 404
(«админки просто нет»). Вход — подписанная cookie (`SESSION_SECRET`,
`samesite=strict`, 12 часов).

Что видно:

* заказы по статусам, сколько взято водителями, сколько с жалобами;
* **качество разбора** за период: доля неполных полей, расход токенов, средняя
  задержка LLM, насколько предфильтр сократил обращения к модели;
* очередь повторного разбора (`pending` / `failed` / `done`) и кнопка «повторить»
  для сообщений, у которых попытки исчерпаны;
* жалобы водителей на конкретные заявки;
* служебное: попадания в кэш разбора, состояние ограничения частоты запросов.

Те же цифры доступны из консоли: `python -m scripts.parse_stats`.

## Вход через Telegram — /login

Пока `TELEGRAM_LOGIN_BOT_TOKEN`/`TELEGRAM_LOGIN_BOT_USERNAME` пусты — вход
выключен целиком, сайт ведёт себя как раньше (лента, карточка заказа и «Мои
заказы» доступны без входа, анонимной cookie). Включается заполнением этих
двух настроек и `SESSION_SECRET` — без него `telegram_login_enabled` останется
`False` даже с заданным токеном (см. предупреждение у `SESSION_SECRET` выше).

**Обязательные условия, без них виджет физически не заработает:**

1. **Отдельный обычный Telegram-бот**, НЕ userbot-аккаунт агента:
   1. Написать [@BotFather](https://t.me/BotFather) → `/newbot` → придумать
      имя и username (заканчивается на `bot`).
   2. Сохранить выданный токен → `TELEGRAM_LOGIN_BOT_TOKEN` в `.env`.
      Username бота (без `@`) → `TELEGRAM_LOGIN_BOT_USERNAME`.
2. **Домен с HTTPS**, привязанный к серверу — виджет входа Telegram работает
   только по HTTPS и только с доменом, а не с голым IP. Прокси (Caddy/nginx)
   перед `webapp.py` + сертификат Let's Encrypt.
3. Домен привязан к боту: `@BotFather` → `/setdomain` → выбрать бота → ввести
   домен (без `https://`, просто `example.com`).

После входа личность водителя — не анонимная cookie, а подписанная сессия
(`driver_session`), привязанная к его Telegram-аккаунту. Токен в
`orders.taken_by_token` при этом становится стабильным (`tg:<telegram_id>`)
вместо случайного UUID — «Мои заказы» переживают смену браузера/устройства.
На `/my` дополнительно показывается профиль: сколько заказов взял всего,
сколько довёл до «Договорились», сколько заработал (по ценам этих заказов —
агрегатор не участвует в оплате и наценок не делает, точнее оценки у нас нет).

Данные водителей, заходивших анонимно ДО включения входа, никуда не
переносятся — начинают копить статистику заново после первого входа.

## Служебные скрипты

```bash
python -m scripts.check_schema        # схема после миграций + целостность данных
python -m scripts.check_schema --fix  # починить нечитаемые значения orders.status
python -m scripts.parse_stats         # качество разбора, расходы LLM, очередь
python -m scripts.cleanup_orders --dry-run   # что протухнет и удалится
python -m scripts.retry_queue         # посмотреть застрявшие сообщения
python -m scripts.retry_queue --run   # сбросить попытки и разобрать заново
python -m scripts.smoke_db            # проверка слоя БД
python -m scripts.smoke_web           # живой smoke-тест сайта (поднимает uvicorn)
python -m scripts.backfill_orders     # разобрать последние сообщения задним числом
python -m scripts.geo_places missing  # места, которым геокодер не нашёл координат
python -m scripts.join_work_groups <файл> --limit 15   # вступить в новые группы (отдельная сессия)
```

`smoke_web` дополняет pytest: тесты гоняют приложение через `ASGITransport`,
то есть без сокета и без реального кодирования HTTP-заголовков. Живой прогон
показывает, что статика отдаётся с диска, `Set-Cookie` с русским текстом
проходит latin-1, а uvicorn способен импортировать `app.web.server:app`.
Проверить уже запущенный сервер: `python -m scripts.smoke_web --url http://127.0.0.1:8000`.

`check_schema` стоит запускать после каждого `alembic upgrade head`: на SQLite
миграция идёт через batch-режим (пересоздание таблицы), и «успешный» выход не
гарантирует, что уникальное ограничение и индексы действительно на месте.

## Тесты и линтер

```bash
python -m pytest          # отдельная БД tests/_test_logist.db (SQLite)
python -m ruff check .    # правила — в ruff.toml
```

Тесты не требуют Telegram и LLM: Telethon-клиент и разбор подменяются
заглушками, а веб-слой проверяется через `httpx.ASGITransport` без поднятия
порта. Покрываются эквивалентность SQL- и Python-фильтров, пагинация,
атомарность «взять/отпустить», маскирование контактов, очередь повторов,
предфильтр, дедупликация, админка и бэкфилл миграции.

## Деплой на VPS (Ubuntu/Debian)

Агент и веб-панель — два независимых процесса на одной БД. На сервере держим их
через systemd (автозапуск, автоперезапуск при падении).

**1. Подготовка сервера**

```bash
sudo adduser --system --group --home /opt/logist-agent logist
sudo -u logist -H bash -c '
  git clone <URL_вашего_репозитория> /opt/logist-agent/src &&
  cd /opt/logist-agent/src &&
  python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt
'
```

(Пути в `deploy/*.service` указывают на `/opt/logist-agent` — либо клонируйте
прямо туда, либо поправьте `WorkingDirectory`/`ExecStart` в юнитах под свой путь.)

**2. `.env` и авторизация**

```bash
sudo -u logist cp /opt/logist-agent/src/.env.example /opt/logist-agent/src/.env
sudo -u logist nano /opt/logist-agent/src/.env   # заполнить как локально
cd /opt/logist-agent/src
sudo -u logist .venv/bin/python -m scripts.auth_telegram              # запросит код
sudo -u logist .venv/bin/python -m scripts.auth_telegram --code XXXXX # ввести код
sudo -u logist .venv/bin/python -m alembic upgrade head
```

Для внешнего доступа к сайту добавьте в `.env`:

```
WEB_HOST=0.0.0.0
```

**3. systemd**

```bash
sudo cp deploy/logist-agent.service deploy/logist-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now logist-agent logist-web
sudo systemctl status logist-agent logist-web    # проверить, что оба Active
sudo journalctl -u logist-agent -f               # живые логи агента
```

Если сервис не стартует — почти всегда дело в путях: проверьте, что
`WorkingDirectory`/`ExecStart` в юнитах совпадают с реальным расположением
проекта на сервере, и что `python3 -m venv` создал `.venv/bin/python` (не
`Scripts/`, это Windows-путь).

**4. Файрвол**

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8000/tcp    # только если WEB_HOST=0.0.0.0
sudo ufw enable
```

**5. Обновление кода**

```bash
cd /opt/logist-agent/src
sudo -u logist git pull
sudo -u logist .venv/bin/pip install -r requirements.txt
sudo -u logist .venv/bin/python -m alembic upgrade head
sudo systemctl restart logist-agent logist-web
```

## Структура

```
app/
  config.py          настройки из .env
  db/base.py         движок, сессии, Base
  timeutil.py        наивный UTC и наивный МСК — единственная работа с «сейчас»
  search.py          search_text и нормализованные ключи для поиска/фильтров в SQL
  city_aliases.py    справочник городов и сокращений
  models/            Order, WorkGroup, ActionLog, ParseStat, PendingMessage
  telegram/          Telethon-клиент, разбор рабочих групп, очередь повторов, дедупликация
  parsing/           предфильтр, LLM-разбор заявок, метрики качества
  services/          фоновые задачи: протухание заявок, чистка метрик, отчёты, алерты
  web/               FastAPI — публичный сайт и админка владельца
    server.py          сборка приложения: middleware, статика, /healthz, /robots.txt
    routes.py          тонкие роуты витрины
    admin.py           роуты /admin
    queries.py         SQL ленты, счётчиков и действий водителя
    filters.py         структурные фильтры (SQL + эталонная Python-проверка)
    presenters.py      форматирование данных и маскирование контактов
    deps.py            cookie водителя, подписанная cookie админки, flash-сообщения
    rate_limit.py      скользящее окно ограничения частоты запросов
    static/            style.css, app.js, admin.css (вынесены из шаблонов ради кэша)
    templates/         Jinja2-шаблоны
scripts/             авторизация, список чатов, управление группами, бэкфил,
                     отчёты по разбору, обслуживание данных, повтор очереди,
                     проверка схемы, smoke-тест БД
migrations/          Alembic
tests/               pytest: фильтры, действия водителя, HTTP, админка, очередь,
                     предфильтр, дедупликация, бэкфилл миграции
deploy/              systemd-юниты для VPS (agent + web)
main.py              точка входа агента (парсинг + фоновые сервисы)
webapp.py            точка входа сайта
ruff.toml            правила линтера и пояснения, что и почему отключено
pytest.ini           asyncio_mode=auto для асинхронных тестов
```

## Группы второго Telegram-аккаунта

Один агент может читать группы нескольких аккаунтов в одну ленту. У каждой
группы в `work_groups.session_name` хранится файл сессии, которым она читается
(пусто — основная сессия). Подключение:

```bash
python -m scripts.auth_account --phone +7... --session acc_second            # запросить код
python -m scripts.auth_account --phone +7... --session acc_second     --code 12345 --phone-code-hash <hash> [--password "2fa"]                 # войти
python -m scripts.manage_work_groups add -1001234567890 "Название" --session acc_second
```

Файл `acc_second.session` кладётся рядом с проектом на сервере (`chmod 600`) и
**не используется больше нигде**: одна сессия с двух IP одновременно — это
`AuthKeyDuplicatedError`, и Telegram обнуляет ключ. Если сессия не
авторизована, агент пропускает её группы и пишет об этом в лог.

## Несколько независимых экземпляров

Можно развернуть несколько независимых экземпляров (свой Telegram-аккаунт,
своя БД, свой порт) — например, разные регионы или разные владельцы. Каждый
экземпляр — отдельная копия проекта с собственным `.env` (`TG_PHONE`,
`TG_SESSION_NAME`, `DATABASE_URL`, `WEB_PORT`) и своей парой systemd-юнитов.
