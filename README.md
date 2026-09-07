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
| 1 | Парсинг заявок из рабочей группы | не начат |
| 2 | Публикация в водительские группы | не начат |
| 3 | Обработка откликов водителей | не начат |
| 4 | Назначение и завершение заказа | не начат |
| 5 | Веб-панель логиста | не начат |

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
- `WORK_GROUP_CHAT_ID`, `LOGIST_USER_ID` — заполняются после шага «Авторизация» ниже

## Авторизация в Telegram (один раз)

```bash
.venv\Scripts\python.exe -m scripts.auth_telegram
```

Скрипт запросит код из Telegram и пароль 2FA, затем создаст `logist.session`.
Файл сессии — это доступ к аккаунту, он в `.gitignore`; не передавайте его.

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

## Запуск

```bash
.venv\Scripts\python.exe main.py
```

## Структура

```
app/
  config.py          настройки из .env, бизнес-правила (проценты, лимиты)
  db/base.py         движок, сессии, Base
  models/            Order, Driver, DriverResponse, DriverGroup, Publication, ActionLog
  telegram/client.py Telethon-клиент на сессии логиста
scripts/             авторизация, список чатов, smoke-тест БД
migrations/          Alembic
main.py              точка входа агента
```

## Ключевые бизнес-правила (вынесены в `.env`)

- Оплата водителю по умолчанию — 80% от стоимости заказа
- Торг с водителем — не более +15%
- Срочный заказ без откликов перепубликуется каждые 10 минут с +10% к оплате
- Напоминание о комиссии — через 24 часа после подачи машины
- Ожидание — 500 ₽/час; багаж, животные, детское кресло — бесплатно
