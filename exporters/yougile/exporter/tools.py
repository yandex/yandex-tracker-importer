import yaml
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .transliterate import translit


# the resolution available in the queues: the export does not resolve tasks
RESOLUTION = {"key": "fixed", "name": {"en": "Fixed", "ru": "Решён"}}
# the tag of an archived task: it is not on a board
ARCHIVE_TAG = "Архив"
# states of the standard YouGile priority sticker and their Tracker priorities
PRIORITIES = {"low": "minor", "normal": "normal", "major": "critical", "critical": "blocker"}
# the category of the fields made from YouGile data: the standard category of every Tracker organization
CATEGORY_NAME = "Системные"
# the task number in YouGile, kept for every task: a renumbered task is found by its YouGile number
YOUGILE_KEY_FIELD = {
    "key": "yougileKey",
    "name": {"en": "YouGile key", "ru": "Номер в YouGile"},
    "category": CATEGORY_NAME,
    "schema": {"type": "string"},
}


def dump_to_yaml(object: Dict[str, Any], file_path: str) -> None:
    with open(file_path, "w") as f:
        yaml.dump(object, f, allow_unicode=True)


def clear_data(object_data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(object_data, dict):
        return {key: clear_data(value) for key, value in object_data.items() if value}
    elif isinstance(object_data, list):
        return [clear_data(i) for i in object_data if i]
    return object_data


def title_to_key(title: str) -> str:
    # construct camelCase key from russian title, for example: "Тестовая строка 123" -> "testovayaStroka"
    no_decimals = "".join(c for c in title if c.isalpha() or c.isspace())
    translitted_title = translit(no_decimals)
    words = translitted_title.split()
    if not words:
        return ""

    camel_case_key = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    return camel_case_key


def unixtime_to_iso_str(unixtime: int) -> Optional[str]:
    if unixtime is None:
        return None
    dt = datetime.fromtimestamp(unixtime / 1000, tz=timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + dt.strftime('%z')


def unixtime_to_date_str(unixtime: int, time_zone: str = "UTC") -> Optional[str]:
    if unixtime is None:
        return None
    return datetime.fromtimestamp(unixtime / 1000, tz=ZoneInfo(time_zone)).strftime("%Y-%m-%d")


def hours_to_ms(hours: Optional[float]) -> Optional[int]:
    if hours is None:
        return None
    return round(hours * 60 * 60 * 1000)
