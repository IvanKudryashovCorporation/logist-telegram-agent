"""Перечисления предметной области."""

import enum


class OrderStatus(str, enum.Enum):
    """Статусы заказа. Простой набор — агрегатор больше не ведёт свой флоу
    назначения, водитель сам договаривается с диспетчером.

    Хранится как VARCHAR(32) (``Enum(native_enum=False)``), CHECK-констрейнта
    нет — поэтому добавление нового значения НЕ требует миграции.
    """

    NEW = "new"                                   # Новая, разобрана полностью
    NEEDS_CLARIFICATION = "needs_clarification"    # LLM не смог разобрать часть полей
    CANCELLED = "cancelled"                        # Скрыта вручную/удалена в TG (неактуальна)
    AGREED = "agreed"                              # Водитель договорился с диспетчером — заказ закрыт
    EXPIRED = "expired"                            # Время подачи прошло — заявка протухла


#: Статусы, которые НЕ показываются в общей ленте.
HIDDEN_STATUSES = frozenset(
    {OrderStatus.CANCELLED, OrderStatus.AGREED, OrderStatus.EXPIRED}
)


#: Статусы «живой» заявки, которую ещё может разобрать фоновая очистка.
OPEN_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.NEEDS_CLARIFICATION})


#: Человекочитаемые подписи статусов — для веб-панели.
ORDER_STATUS_LABELS = {
    OrderStatus.NEW: "Новая",
    OrderStatus.NEEDS_CLARIFICATION: "Нужно уточнить",
    OrderStatus.CANCELLED: "Скрыта",
    OrderStatus.AGREED: "Договорились",
    OrderStatus.EXPIRED: "Просрочена",
}


class ParseOutcome(str, enum.Enum):
    """Итог разбора одного сообщения — для метрик качества (см. ParseStat)."""

    ORDER = "order"                # нашли хотя бы одну заявку
    NOT_ORDER = "not_order"        # LLM сказал, что это не заявка
    PREFILTERED = "prefiltered"    # даже не отправляли в LLM — явно не заявка
    DUPLICATE = "duplicate"        # заявка уже есть в ленте
    ERROR = "error"                # разбор не удался (LLM/сеть/валидация)
    QUEUED = "queued"              # отложен в очередь на повтор


class PendingStatus(str, enum.Enum):
    """Состояние сообщения в очереди повторного разбора."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"   # исчерпаны попытки — нужен человек (scripts/retry_queue.py)


class ActorType(str, enum.Enum):
    """Кто совершил действие — для истории."""

    AGENT = "agent"
    LOGIST = "logist"
    DISPATCHER = "dispatcher"
    #: Фоновые задачи (автопротухание заявок, воркер очереди) — не человек.
    SYSTEM = "system"
    #: Водитель на сайте (взял заказ, договорился, пожаловался).
    DRIVER = "driver"
