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

    # --- Админка (/admin) ---
    #: Пустая строка — админка полностью отключена (404 на всех её маршрутах).
    admin_password: str = ""
    #: Секрет подписи cookie админки. Если не задан, выводится из пароля,
    #: поэтому смена пароля автоматически «разлогинивает» все сессии.
    session_secret: str = ""

    # --- Вход через Telegram (Login Widget) ---
    #: Токен ОБЫЧНОГО Telegram-бота из @BotFather (НЕ userbot-аккаунт агента!) —
    #: нужен только для проверки подписи данных от виджета входа. Пусто —
    #: вход выключен целиком: лента видна всем, но карточка заказа и "Мои
    #: заказы" недоступны (как будто виджета никогда не было).
    telegram_login_bot_token: str = ""
    #: Username бота без @ — виджету нужно знать, через какого бота логинить.
    telegram_login_bot_username: str = ""

    @property
    def telegram_login_enabled(self) -> bool:
        # SESSION_SECRET обязателен явно: без него подпись сессии водителя
        # уходила бы в f"admin::{admin_password}" — при выключенной админке
        # (admin_password пуст) это предсказуемый секрет "admin::", и сессию
        # правда важно, а не только админ-cookie на 12 часов.
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
    #: Как часто воркер просматривает заказы без координат, секунд.
    geocode_poll_seconds: int = 30
    #: Сколько заказов обрабатывает за один проход.
    geocode_batch: int = 20

    # --- Фоновая очистка протухших заявок ---
    #: Через сколько часов после времени подачи заявка становится EXPIRED.
    expire_grace_hours: int = 6
    #: Периодичность фоновой очистки, минут.
    expire_poll_minutes: int = 15
    #: Через сколько часов после публикации закрывается заявка «в ближайшее
    #: время» (у неё нет времени подачи, и без этого срока она копилась бы вечно).
    asap_expire_hours: int = 48
    #: «Договорился» -> «В работе»: у заявки без точного времени подачи (дедлайна
    #: нет) это наступает через столько часов после «Договорился».
    asap_work_start_hours: int = 3
    #: Через сколько часов после дедлайна заказ «в работе» сам становится
    #: «Выполнен», если водитель не нажал «Выполнил» и не отменил.
    auto_complete_hours: int = 72
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
    def admin_enabled(self) -> bool:
        return bool(self.admin_password.strip())

    @property
    def signing_secret(self) -> str:
        """Ключ для подписи cookie админки."""
        return self.session_secret.strip() or f"admin::{self.admin_password}"


settings = Settings()
