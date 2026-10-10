"""Заказ — заявка на перевозку, разобранная из сообщения в рабочей группе.

Агрегатор для водителей: показываем заявку как есть, без наценки и без
собственного флоу назначения — водитель сам пишет диспетчеру.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import OrderStatus


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        # Одна заявка из одного сообщения ровно одна — железная защита от дублей
        # на уровне БД (in-memory дедупликация Telethon-событий переживает не
        # каждый рестарт агента).
        UniqueConstraint(
            "source_chat_id", "source_message_id", "source_sub_index", name="uq_orders_source"
        ),
        # Основной запрос ленты и счётчика в шапке: status + taken_by_token + pickup_at.
        Index("ix_orders_feed", "status", "taken_by_token", "pickup_at"),
        # Поиск дублей при разборе: цена + окно по дате подачи.
        Index("ix_orders_dedup", "client_price", "pickup_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Источник: сообщение диспетчера в рабочей группе ---
    # Храним id сообщения, чтобы ловить его редактирование. Одно сообщение
    # может содержать НЕСКОЛЬКО заявок сразу (диспетчер скидывает список) —
    # source_sub_index различает их (0, 1, 2...) в пределах одного сообщения;
    # для обычного сообщения с одной заявкой это всегда 0.
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sub_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Диспетчер определяется по аккаунту отправителя — на него ведёт кнопка
    # "Написать диспетчеру" на сайте.
    dispatcher_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    dispatcher_username: Mapped[Optional[str]] = mapped_column(String(64))
    # Если в тексте заявки явно указано "писать @аккаунт" — это и есть
    # реальный диспетчер по этому заказу, даже если сообщение отправил
    # кто-то другой (пересылка/публикация от имени группы и т.п.).
    # Приоритетнее dispatcher_username в dispatcher_link().
    contact_username: Mapped[Optional[str]] = mapped_column(String(64))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Маршрут и время ---
    pickup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    # «Сейчас / в течение часа / в ближайшее время»: конкретного времени нет, но
    # это не «время не указано». pickup_at остаётся NULL — поэтому такая заявка
    # не протухает по времени и висит в ленте, пока её не возьмут.
    pickup_asap: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    from_address: Mapped[Optional[str]] = mapped_column(String(512))
    to_address: Mapped[Optional[str]] = mapped_column(String(512))
    from_city: Mapped[Optional[str]] = mapped_column(String(128))
    to_city: Mapped[Optional[str]] = mapped_column(String(128))
    flight_or_train: Mapped[Optional[str]] = mapped_column(String(128))
    #: Промежуточные пункты между «откуда» и «куда» по порядку: «Пермь — Соликамск — Пермь Аэропорт».
    #: ``[{"name": "Соликамск", "lat": 59.6, "lon": 56.8}]``; координаты проставляет геокодер,
    #: расстояние считается по всему пути через эти точки. NULL — остановок нет.
    via_points: Mapped[Optional[list]] = mapped_column(JSON(none_as_null=True))

    # --- Параметры поездки ---
    car_class: Mapped[Optional[str]] = mapped_column(String(64))
    passengers: Mapped[Optional[int]] = mapped_column(Integer)
    luggage: Mapped[Optional[str]] = mapped_column(String(128))
    has_pets: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_child_seat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Клиент ---
    client_name: Mapped[Optional[str]] = mapped_column(String(128))
    client_phone: Mapped[Optional[str]] = mapped_column(String(64))

    # --- Деньги: 1 в 1 из заявки, без наценки ---
    client_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    # Ключ нормализованного текста сообщения, из которого создан заказ: по нему точные копии
    # заявки (из других групп, повторные публикации) находятся без вызова LLM.
    text_key: Mapped[Optional[str]] = mapped_column(String(32), index=True)

    # --- Поисковый индекс ---
    # Денормализованная строка для полнотекстового поиска на сайте: id, телефон,
    # имя, города, адреса, @username — всё в нижнем регистре, через пробел.
    # Нужна именно отдельная колонка, а не LOWER(...) в запросе: lower() в SQLite
    # работает только для ASCII и «СИМФЕРОПОЛЬ» != «симферополь», а Python-овский
    # .lower() при записи обрабатывает кириллицу корректно.
    # Пересчитывается в app/search.py при каждом сохранении заказа.
    search_text: Mapped[Optional[str]] = mapped_column(Text)
    # Нормализованные (lower + без «г.») города — чтобы фильтр «откуда/куда»
    # работал в SQL через LIKE, а не перебором всех строк в Python.
    # Пересчитываются тем же app/search.py.
    from_city_key: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    to_city_key: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    # Час подачи строкой 'HH:MM' — нужен, чтобы фильтр «время подачи» работал
    # в SQL. Извлекать время из datetime переносимо не получается: в SQLite
    # CAST(x AS TIME) приводит к NUMERIC, в PostgreSQL — к TIME, а единого
    # выражения нет. Сравнение строк 'HH:MM' работает одинаково везде.
    pickup_time_key: Mapped[Optional[str]] = mapped_column(String(5), index=True)
    # Координаты концов маршрута для фильтра радиуса. Заполняет фоновый
    # геокодер (app/geo.py), а не парсер заявок: если геокодер недоступен,
    # заказ всё равно создаётся — просто без координат и ищется по названию.
    from_lat: Mapped[Optional[float]] = mapped_column(Float)
    from_lon: Mapped[Optional[float]] = mapped_column(Float)
    to_lat: Mapped[Optional[float]] = mapped_column(Float)
    to_lon: Mapped[Optional[float]] = mapped_column(Float)
    #: NULL — геокодер этим заказом ещё не занимался (или города поменялись).
    geo_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))
    #: ``car`` / ``minivan`` — какой автомобиль нужен (app.search.vehicle_type_of).
    #: Считается при каждом сохранении вместе с остальными производными полями.
    vehicle_type: Mapped[Optional[str]] = mapped_column(String(8))
    #: Версия логики геокодера (app.geo.GEO_VERSION), которой посчитаны координаты.
    #: NULL или меньше текущей — воркер пересчитает заказ.
    geo_version: Mapped[Optional[int]] = mapped_column(Integer)
    #: Расстояние по дорогам между концами маршрута, км (маршрутизатор OSRM,
    #: app/routing.py). Считает фоновый воркер после геокодера; NULL — ещё не
    #: посчитано или посчитать не вышло (тогда сайт просто не показывает цифру).
    distance_km: Mapped[Optional[float]] = mapped_column(Float)
    #: NULL — воркер маршрутов этим заказом ещё не занимался (или города поменялись).
    route_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))

    # --- Состояние ---
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32),
        default=OrderStatus.NEW,
        nullable=False,
    )
    # Отметка «с заявкой что-то не так» — ставится в т.ч. водителями через
    # кнопку «Цена неактуальна» на карточке заказа (см. /orders/{id}/feedback).
    has_problem: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    problem_note: Mapped[Optional[str]] = mapped_column(Text)
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- "Мои заказы" ---
    # Сайт без логина: водителя различаем по анонимной cookie в браузере
    # (см. app/web/deps.py). Кто взял заказ первым — тот и взял; само взятие
    # делается атомарным UPDATE ... WHERE taken_by_token IS NULL, поэтому
    # гонки нет ни на SQLite, ни на PostgreSQL.
    taken_by_token: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    #: Наивный UTC (см. app/timeutil.py) — как и created_at.
    taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))

    def __repr__(self) -> str:
        return f"<Order #{self.id} {self.from_city}->{self.to_city} {self.status.value}>"
