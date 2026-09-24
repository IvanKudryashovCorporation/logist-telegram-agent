# Агрегатор заказов для водителей

Юзербот на Telegram-аккаунте собирает заявки на перевозку из множества рабочих
групп диспетчеров и публикует их на публичном сайте. Цена и все параметры —
1 в 1 из заявки, без наценки. Водитель, найдя подходящий заказ, жмёт «Написать
диспетчеру» и договаривается напрямую в Telegram — сам агент в переписку не
вмешивается.

## Стек

Python 3.10+ · Telethon (юзербот-сессия, только чтение групп) · SQLAlchemy 2
(async) + Alembic · SQLite локально (PostgreSQL в проде — меняется одной
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
диспетчеру» (открывает личку в Telegram с тем, кто прислал заявку).

Без авторизации, публичный доступ по ссылке. `WEB_HOST`/`WEB_PORT` в `.env`
управляют, на каком адресе слушать (по умолчанию `127.0.0.1` — недоступно
снаружи, для реального сайта нужно `0.0.0.0`, см. деплой ниже).

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
  models/            Order, WorkGroup, ActionLog
  telegram/          Telethon-клиент и обработчик рабочих групп (парсинг заявок)
  parsing/           LLM-разбор заявок диспетчеров (1 в 1, без наценки)
  web/               FastAPI — публичный сайт со списком заказов
scripts/             авторизация, список чатов, smoke-тест БД, управление группами, бэкфил
migrations/          Alembic
deploy/              systemd-юниты для VPS (agent + web)
main.py              точка входа агента (только парсинг)
webapp.py            точка входа сайта
```

## Несколько аккаунтов

Можно развернуть несколько независимых экземпляров (свой Telegram-аккаунт,
своя БД, свой порт) — например, разные регионы или разные владельцы. Каждый
экземпляр — отдельная копия проекта с собственным `.env` (`TG_PHONE`,
`TG_SESSION_NAME`, `DATABASE_URL`, `WEB_PORT`) и своей парой systemd-юнитов.
