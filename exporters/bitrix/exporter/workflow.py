from typing import Any, Dict, List

# Bitrix24 task STATUS -> Tracker status key; 1 is the legacy "new" status.
STATUS_BY_BITRIX_STATUS = {
    "1": "open",
    "2": "open",
    "3": "inProgress",
    "4": "inReview",
    "5": "resolved",
    "6": "onHold",
    "7": "cancelled",
}

# Standard Tracker statuses with their standard names and types. Order defines the workflow steps.
STATUSES = [
    {"key": "open", "name": {"ru": "Открыт", "en": "Open"}, "type": "new", "action": {"ru": "Открыть", "en": "Open"}},
    {
        "key": "inProgress",
        "name": {"ru": "В работе", "en": "In Progress"},
        "type": "inProgress",
        "action": {"ru": "В работу", "en": "Start progress"},
    },
    {
        "key": "inReview",
        "name": {"ru": "Ревью", "en": "In Review"},
        "type": "paused",
        "action": {"ru": "На ревью", "en": "To review"},
    },
    {
        "key": "onHold",
        "name": {"ru": "Приостановлено", "en": "On Hold"},
        "type": "paused",
        "action": {"ru": "Приостановить", "en": "Hold"},
    },
    {
        "key": "resolved",
        "name": {"ru": "Решен", "en": "Resolved"},
        "type": "done",
        "action": {"ru": "Завершить", "en": "Resolve"},
    },
    {
        "key": "cancelled",
        "name": {"ru": "Отменено", "en": "Cancelled"},
        "type": "cancelled",
        "action": {"ru": "Отменить", "en": "Cancel"},
    },
]
INITIAL_STATUS = "open"

# Bitrix24 task STATUS -> standard Tracker resolution key for completed and declined tasks
RESOLUTION_BY_BITRIX_STATUS = {"5": "fixed", "7": "wontFix"}
RESOLUTIONS = [
    {"key": "fixed", "name": {"ru": "Решен", "en": "Fixed"}},
    {"key": "wontFix", "name": {"ru": "Не будет исправлено", "en": "Won't fix"}},
]


def statuses_data() -> List[Dict[str, Any]]:
    """Content of ``statuses.yaml``."""
    return [{"key": status["key"], "name": status["name"], "type": status["type"]} for status in STATUSES]


def resolutions_data() -> List[Dict[str, Any]]:
    """Content of ``resolutions.yaml``."""
    return [dict(resolution) for resolution in RESOLUTIONS]


def workflow_steps() -> List[Dict[str, Any]]:
    return [
        {"status": status["key"], "metaAction": {"name": status["action"], "target": status["key"]}}
        for status in STATUSES
    ]


# Bitrix24 sprint status -> Tracker sprint status
SPRINT_STATUS_BY_BITRIX_STATUS = {"planned": "draft", "active": "in_progress", "completed": "released"}


def board_columns() -> List[Dict[str, Any]]:
    """Columns of a board: one per status of the workflow."""
    return [
        {"id": number, "name": status["name"]["ru"], "statuses": [status["key"]]}
        for number, status in enumerate(STATUSES, start=1)
    ]
