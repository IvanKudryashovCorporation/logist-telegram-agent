"""Перечисления предметной области (см. опрос, вопросы 31-40)."""

import enum


class OrderStatus(str, enum.Enum):
    """Статусы заказа.

    Агент вправе ставить сам: NEW, NEEDS_CLARIFICATION, SEARCHING,
    HAS_RESPONSES, IN_PROGRESS (вопрос 40).
    Только логист: DRIVER_ASSIGNED (утверждение) и COMPLETED (вопросы 35, 37, 122).
    """

    NEW = "new"                                # Новая
    NEEDS_CLARIFICATION = "needs_clarification"  # Нужно уточнить
    SEARCHING = "searching"                    # В поиске
    HAS_RESPONSES = "has_responses"            # Есть отклики
    DRIVER_ASSIGNED = "driver_assigned"        # Водитель назначен
    IN_PROGRESS = "in_progress"                # В работе
    COMPLETED = "completed"                    # Завершено
    CANCELLED = "cancelled"                    # Отмена


#: Человекочитаемые подписи статусов — для веб-панели (Этап 5).
ORDER_STATUS_LABELS = {
    OrderStatus.NEW: "Новая",
    OrderStatus.NEEDS_CLARIFICATION: "Нужно уточнить",
    OrderStatus.SEARCHING: "В поиске",
    OrderStatus.HAS_RESPONSES: "Есть отклики",
    OrderStatus.DRIVER_ASSIGNED: "Водитель назначен",
    OrderStatus.IN_PROGRESS: "В работе",
    OrderStatus.COMPLETED: "Завершено",
    OrderStatus.CANCELLED: "Отмена",
}

#: Статусы, которые агент может выставлять без подтверждения логиста.
AGENT_ALLOWED_STATUSES = {
    OrderStatus.NEW,
    OrderStatus.NEEDS_CLARIFICATION,
    OrderStatus.SEARCHING,
    OrderStatus.HAS_RESPONSES,
    OrderStatus.IN_PROGRESS,
    OrderStatus.CANCELLED,
}


class ResponseKind(str, enum.Enum):
    """Тип сообщения водителя в ответ на публикацию (вопросы 68, 71)."""

    ACCEPT = "accept"          # «я», «беру», «готов»
    BARGAIN = "bargain"        # «торг есть?», «за 15 поеду»
    QUESTION = "question"      # «Севас актуален?», «еще есть заказ?»
    HOLD = "hold"              # «придержите»
    UNCLEAR = "unclear"        # «+» и прочее, требующее уточнения


class ResponseStatus(str, enum.Enum):
    """Состояние работы агента с откликом."""

    NEW = "new"
    NEGOTIATING = "negotiating"          # агент уточняет данные / торгуется
    FORWARDED = "forwarded"              # кандидат передан логисту
    REJECTED = "rejected"
    ASSIGNED = "assigned"                # этот отклик стал назначенным водителем


class ActorType(str, enum.Enum):
    """Кто совершил действие — для истории (вопрос 8)."""

    AGENT = "agent"
    LOGIST = "logist"
    DISPATCHER = "dispatcher"
    DRIVER = "driver"


class PendingActionType(str, enum.Enum):
    """Действие с side-эффектами в Telegram, поставленное из веб-панели.

    Веб-панель и агент — разные процессы с одним Telethon-сессией на аккаунте
    логиста; параллельное подключение к ней ломает файл сессии (SQLite lock).
    Поэтому веб-панель не шлёт сообщения сама, а кладёт задачу в очередь —
    её забирает и выполняет уже работающий агент (app/workflow/queue.py).
    """

    PUBLISH = "publish"
    ASSIGN = "assign"
    COMPLETE = "complete"
    CANCEL = "cancel"
    CONFIRM_PAYMENT = "confirm_payment"


class PendingActionStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
