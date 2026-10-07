"""Конфигурация приложения: читается из .env."""

from pathlib import Path
from typing import Annotated, Optional

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _empty_to_none(value: object) -> object:
    """Незаполненная строка в .env означает «не задано», а не ошибку."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalInt = Annotated[Optional[int], BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    tg_api_id: OptionalInt = None
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_session_name: str = "logist"

    # Чаты. Список рабочих групп живёт в БД (scripts.manage_work_groups);
    # это поле — только для разового переноса старой настройки из .env.
    work_group_chat_id: OptionalInt = None

    # LLM — любой OpenAI-совместимый chat/completions API (сейчас DashScope).
    # Внимание: НЕ называть ANTHROPIC_*/OPENAI_*  — такие имена зарезервированы
    # окружением песочницы разработки и имеют приоритет над .env, значения
    # будут подменены.
    llm_api_key: str = ""
    llm_model: str = "qwen3.8-max-0902"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    #: Таймаут одного запроса к LLM, секунд.
    llm_timeout_seconds: float = 30.0
    #: Сколько раз повторяем запрос при таймауте/429/5xx, прежде чем отложить
    #: сообщение в очередь (app/models/pending_message.py).
    llm_max_retries: int = 3
    #: База экспоненциальной задержки между повторами, секунд.
    llm_retry_backoff_seconds: float = 1.0

    # --- Разбор заявок ---
    #: Не отправлять в LLM сообщения, которые заведомо не являются заявкой
    #: («ок», «принял», короткие реплики без маршрута/цены). Экономит деньги и
    #: снимает риск упереться в rate-limit провайдера.
    prefilter_enabled: bool = True
    #: Кэш результатов разбора по хэшу текста (диспетчеры копируют заявки).
    parse_cache_size: int = 1024
    #: Писать в parse_stats метрики каждого разбора.
    parse_stats_enabled: bool = True

    # Очередь повторного разбора (сообщения, где LLM/сеть отказали).
    queue_enabled: bool = True
    queue_max_attempts: int = 6
    #: Задержка первой попытки, секунд; дальше растёт экспоненциально.
    queue_base_delay_seconds: int = 60
    #: Как часто воркер просматривает очередь, секунд.
    queue_poll_seconds: int = 60

    # БД
    database_url: str = "sqlite+aiosqlite:///./logist.db"
    #: Без пула соединений (NullPool). Нужен тестам на PostgreSQL: у каждого
    #: теста свой event loop, а соединение asyncpg привязано к своему loop.
    db_null_pool: bool = False

    # Веб-панель. Локально — 127.0.0.1, чтобы не торчать наружу.
    # На VPS для внешнего доступа задайте WEB_HOST=0.0.0.0 в .env.
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    #: Заказов на одной странице ленты.
    web_page_size: int = 60
    #: Сколько секунд держать в памяти справочник городов для автодополнения.
    city_cache_ttl_seconds: int = 120
    #: Показывать телефон/имя клиента только водителю, взявшему заказ.
    #: Сайт публичный и без логина — открытые телефоны собирают парсерами,
    #: а это персональные данные. Диспетчер (кому писать) остаётся видимым всем.
    mask_client_contacts: bool = True
    #: Слать алерты владельцу в Telegram при проблемах с разбором.
    web_notify_enabled: bool = False

    # --- Ограничение частоты запросов (анти-грифинг и анти-скрейпинг) ---
    rate_limit_enabled: bool = True
    rate_limit_get_per_minute: int = 120
    rate_limit_post_per_minute: int = 30
    #: Сайт за nginx/Caddy — реальный IP клиента лежит в X-Forwarded-For.
    #: Ставить False, если сайт доступен напрямую без доверенного прокси.
    trust_proxy_headers: bool = True

    # --- Контакт владельца ---
    #: Telegram username (без @) для кнопки «Обратная связь» вверху ленты.
    #: Пусто — кнопки нет.
    owner_contact_username: str = "lmaosrk"

    # --- Админка (/admin) ---
    #: Telegram id владельцев через запятую. Админка открывается ТОЛЬКО вошедшему
    #: через Telegram с одним из этих id; всем остальным (и гостям) её нет — 404.
    #: Пусто — админки нет ни у кого. Пароля у неё больше нет.
    admin_telegram_ids: str = ""
    #: Секрет подписи сессий (cookie входа через Telegram).
    session_secret: str = ""

    # --- Вход через Telegram (Login Widget) ---
    #: Токен ОБЫЧНОГО Telegram-бота из @BotFather (НЕ userbot-аккаунт агента!) —
    #: нужен только для проверки подписи данных от виджета входа. Пусто —
    #: вход выключен целиком: лента видна всем, но карточка заказа и "Мои
    #: заказы" недоступны (как будто виджета никогда не было).
    telegram_login_bot_token: str = ""
    #: Username бота без @ — виджету нужно знать, через какого бота логинить.
    telegram_login_bot_username: str = ""

    #: Принимать сообщения бота входа (опрос getUpdates) в процессе сайта. Выключите,
    #: если бота уже читает другая программа или на него настроен вебхук.
    bot_login_polling: bool = True

    @property
    def telegram_login_enabled(self) -> bool:
        # SESSION_SECRET обязателен явно: подпись сессии водителя (и доступ в
        # админку) не должна держаться на пустом или предсказуемом ключе.
        return bool(
            self.telegram_login_bot_token.strip()
            and self.telegram_login_bot_username.strip()
            and self.session_secret.strip()
        )

    # --- Геокодирование (фильтр радиуса «откуда/куда») ---
    #: Фоновый воркер агента проставляет заказам координаты через Nominatim
    #: (OpenStreetMap). Выключено — радиус не работает, фильтр по названию как раньше.
    geocoding_enabled: bool = True
    #: Nominatim требует User-Agent с контактом — анонимные клиенты банят.
    geocoder_user_agent: str = "podacha-geocoder/1.0 (skoottv9@gmail.com)"
    #: Нечёткий поиск (Photon) для опечаток в названиях и центр района для деревень, которых нет в картах.
    geocoding_fuzzy_enabled: bool = True
    photon_url: str = "https://photon.komoot.io/api/"
    #: Как часто воркер просматривает заказы без координат, секунд.
    geocode_poll_seconds: int = 30
    #: Сколько заказов обрабатывает за один проход.
    geocode_batch: int = 20

    # --- Расстояние по дорогам ---
    #: Фоновый воркер считает длину маршрута A→B по дорогам через OSRM.
    #: Выключено — километры на сайте просто не показываются.
    routing_enabled: bool = True
    #: Публичный демо-сервер OSRM: бесплатный, без ключа и без гарантий,
    #: поэтому запросы редкие (≤1/с), а результат хранится в заказе.
    routing_url: str = "https://router.project-osrm.org"
    #: Как часто воркер просматривает заказы без расстояния, секунд.
    routing_poll_seconds: int = 30
    #: Сколько заказов обрабатывает за один проход.
    routing_batch: int = 20

    # --- Наблюдение за публичными группами без вступления ---
    #: Как часто опрашивается история таких групп, секунд.
    watch_poll_seconds: int = 60
    #: За сколько последних часов читаем группу при первом опросе (потом — только новое).
    #: Не больше: при сотне групп стартовая волна разбора иначе слишком велика.
    watch_initial_hours: int = 6
    #: Правки и удаления проверяются раз в столько кругов опроса (новые сообщения — каждый круг).
    watch_slow_every: int = 5

    # --- Уведомления водителям о новых заказах по фильтру ---
    #: Публичный адрес сайта — для ссылок на заказы в сообщениях бота.
    public_base_url: str = "https://lentazakazov.ru"
    #: Как часто воркер сверяет свежие заказы с подписками, секунд.
    subscription_poll_seconds: int = 30
    #: Сколько заказов перечислять в одном сообщении (остальные — «и ещё N»).
    subscription_max_lines: int = 8

    # --- Сторож (app/services/watchdog.py, scripts/watchdog.py) ---
    #: Куда писать алерты: id вашего чата с ботом (бот может писать только тому,
    #: кто нажал в нём «Старт» или вошёл через виджет). Пусто — сторож молчит.
    alert_chat_id: str = ""
    #: Токен бота для алертов; пусто — используется бот входа через Telegram.
    alert_bot_token: str = ""
    #: Тревога, если за столько часов агент не обработал ни одного сообщения.
    watchdog_agent_silence_hours: int = 3
    #: Тревога, если из групп одного Telegram-аккаунта за столько часов ничего не пришло
    #: (при этом другие аккаунты могут работать — общая проверка этого не видит).
    watchdog_account_silence_hours: int = 6
    #: Тревога, если за столько часов не создано ни одной новой заявки.
    watchdog_orders_silence_hours: int = 6
    #: Тревога, если ошибок разбора заявок за последний час не меньше этого числа.
    watchdog_llm_errors_per_hour: int = 10
    #: Тревога, если диск занят больше чем на столько процентов.
    watchdog_disk_percent: int = 85
    #: Как часто напоминать о неисправленной проблеме, часов.
    watchdog_reminder_hours: int = 6
    watchdog_backup_dir: str = "/root/backups"
    watchdog_state_path: str = ".watchdog_state.json"
    #: Пароль шифрования внешних копий базы (app/services/offsite_backup.py). Создаётся
    #: командой ``scripts.offsite_backup --init`` и присылается владельцу; файла нет — копия
    #: вне сервера не настроена.
    offsite_backup_passphrase_path: str = ".offsite_backup_passphrase"

    # --- Фоновая очистка протухших заявок ---
    #: Через сколько часов после времени подачи заявка становится EXPIRED.
    expire_grace_hours: int = 6
    #: Периодичность фоновой очистки, минут.
    expire_poll_minutes: int = 15
    #: Через сколько часов после публикации закрывается заявка «в ближайшее
    #: время» (у неё нет времени подачи, и без этого срока она копилась бы вечно).
    asap_expire_hours: int = 12
    #: «Договорился» -> «В работе»: у заявки без точного времени подачи (дедлайна
    #: нет) это наступает через столько часов после «Договорился».
    asap_work_start_hours: int = 3
    #: Через сколько часов после дедлайна заказ «в работе» сам становится
    #: «Выполнен», если водитель не нажал «Выполнил» и не отменил.
    auto_complete_hours: int = 72
    #: Через сколько дней после закрытия удалять насовсем заказы «просрочен» и
    #: «снят с ленты», которые никто не брал (0 — не удалять). Заказы водителей
    #: (договорился / в работе / выполнен) не удаляются никогда: это их история
    #: и заработок в профиле.
    closed_orders_retention_days: int = 30
    #: Сколько дней хранить строки parse_stats (0 — не удалять).
    stats_retention_days: int = 30

    # --- Уведомления владельцу в Telegram ---
    #: Куда слать алерты: @username, числовой id чата или «me» (Избранное).
    #: Пусто — уведомления выключены, всё уходит только в лог.
    notify_chat_id: str = ""
    notify_on_start: bool = True
    #: Сколько подряд ошибок разбора до алерта.
    notify_error_threshold: int = 5

    @property
    def session_path(self) -> Path:
        return PROJECT_ROOT / f"{self.tg_session_name}.session"

    @property
    def admin_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.admin_telegram_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return ids

    @property
    def admin_enabled(self) -> bool:
        # Без входа через Telegram админку открывать нечем — значит, её нет.
        return bool(self.admin_ids) and self.telegram_login_enabled

    @property
    def signing_secret(self) -> str:
        """Ключ для подписи сессий входа через Telegram."""
        return self.session_secret.strip()


settings = Settings()
