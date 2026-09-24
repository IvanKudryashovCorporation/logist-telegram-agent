# Telegram-агент логиста

Юзербот на личном аккаунте логиста трансферной компании. Снимает механическую
работу: читает заявки диспетчеров, публикует заказы в водительские группы, ведёт
первичную переписку с водителями, следит за статусами и напоминаниями.
Решения о назначении водителя, проверке оплаты и завершении заказа остаются за логистом.

Требования собраны в опросе из 167 вопросов; ссылки на его пункты стоят
комментариями в коде (например, «вопрос 81» в карточке водителя).

## Статус

| Этап | Что | Состояние |
|---|---|---|
| 0 | Каркас, модели, миграции, Telethon-сессия | готов |
| 1 | Парсинг заявок из рабочей группы | готов |
| 2 | Публикация в водительские группы | готов |
| 3 | Обработка откликов водителей | готов |
| 4 | Назначение и завершение заказа | готов |
| 5 | Веб-панель логиста | готов |

Все этапы проверены живыми тестами на реальных Telegram-чатах.

## Стек

Python 3.10 · Telethon · SQLAlchemy 2 (async) + Alembic · SQLite локально
(PostgreSQL в проде — меняется одной строкой `DATABASE_URL`) · Claude API для
разбора текста · APScheduler для перепубликаций и напоминаний.

## Установка

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Заполнить `.env`:

- `TG_API_ID` / `TG_API_HASH` — получить на https://my.telegram.org → API development tools
- `TG_PHONE` — телефон аккаунта логиста
- `LLM_API_KEY` — ключ Claude API (имя намеренно не `ANTHROPIC_*`, см. комментарий в `app/config.py`)
- `LLM_BASE_URL` — заполнить, только если ключ выдан прокси-сервисом, а не напрямую console.anthropic.com
- `LOGIST_USER_ID` — заполняется после шага «Авторизация» ниже

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

Дальше можно найти id нужных чатов:

```bash
.venv\Scripts\python.exe -m scripts.list_chats
.venv\Scripts\python.exe -m scripts.list_chats водител
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

Слушает рабочую группу диспетчеров, водительские группы (из `scripts.manage_driver_groups`)
и личку логиста в его же «Избранном» — там же принимает команды `/assign`, `/complete`,
`/cancel`, `/paid` (см. `/help`).

## Веб-панель (Этап 5)

Отдельный процесс, общая БД с агентом:

```bash
.venv\Scripts\python.exe webapp.py
```

Открыть http://127.0.0.1:8000 — список заказов (сегодня/завтра/позже/просрочено),
поиск по телефону/маршруту/водителю, карточка заказа, `/reports` — сводка за период.

Без авторизации по решению из опроса. `WEB_HOST`/`WEB_PORT` в `.env` управляют,
на каком адресе слушать (по умолчанию `127.0.0.1` — недоступно снаружи).

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

Для внешнего доступа к веб-панели добавьте в `.env`:

```
WEB_HOST=0.0.0.0
```

⚠ Без авторизации в панели это открывает её всем, кто узнает `IP:8000` — включая
телефоны клиентов и цены в заказах. Если это не то, что нужно, ограничьте доступ
файрволом (например `ufw allow from <ваш_IP> to any port 8000`) или поставьте
Tailscale/VPN вместо публичного порта.

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
sudo ufw allow 8000/tcp    # только если WEB_HOST=0.0.0.0 и панель должна быть снаружи
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
  config.py          настройки из .env, бизнес-правила (проценты, лимиты)
  db/base.py         движок, сессии, Base
  models/            Order, Driver, DriverResponse, DriverGroup, Publication, ActionLog
  telegram/          Telethon-клиент и обработчики (рабочая группа, водительские группы, личка)
  parsing/           LLM-разбор заявок диспетчеров
  publishing/        подбор групп, формат объявления, публикация, автоудаление
  negotiation/       классификация откликов водителей, торг, сбор данных
  workflow/          назначение водителя, созвон, оплата, завершение
  reminders/          APScheduler — напоминание о комиссии через 24ч
  web/               FastAPI-панель (Этап 5)
scripts/             авторизация, список чатов, smoke-тест БД, управление группами
migrations/          Alembic
deploy/              systemd-юниты для VPS (agent + web)
main.py              точка входа агента
webapp.py            точка входа веб-панели
```

## Ключевые бизнес-правила (вынесены в `.env`)

- Оплата водителю по умолчанию — 80% от стоимости заказа
- Торг с водителем — не более +15%
- Срочный заказ без откликов перепубликуется каждые 10 минут с +10% к оплате
- Напоминание о комиссии — через 24 часа после подачи машины
- Ожидание — 500 ₽/час; багаж, животные, детское кресло — бесплатно
