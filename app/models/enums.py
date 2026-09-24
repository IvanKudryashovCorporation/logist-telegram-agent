"""Перечисления предметной области."""

import enum


class OrderStatus(str, enum.Enum):
    """Статусы заказа. Простой набор — агрегатор больше не ведёт свой флоу
    назначения, водитель сам договаривается с диспетчером."""

    NEW = "new"                                   # Новая, разобрана полностью
    NEEDS_CLARIFICATION = "needs_clarification"    # LLM не смог разобрать часть полей
    CANCELLED = "cancelled"                        # Скрыта вручную из веб-панели (неактуальна)


#: Человекочитаемые подписи статусов — для веб-панели.
ORDER_STATUS_LABELS = {
    OrderStatus.NEW: "Новая",
    OrderStatus.NEEDS_CLARIFICATION: "Нужно уточнить",
    OrderStatus.CANCELLED: "Скрыта",
}


class ActorType(str, enum.Enum):
    """Кто совершил действие — для истории."""

    AGENT = "agent"
    LOGIST = "logist"
    DISPATCHER = "dispatcher"
