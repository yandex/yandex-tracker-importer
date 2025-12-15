import logging
import time
import yaml
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, Type, Tuple, Union

from yandex_tracker_client.objects import Resource

from .exceptions import MissingFieldError
from .mapper import EntityMapper

logger = logging.getLogger(__name__)


def load_yaml_file(file_path: str) -> Any:
    with open(file_path, "r") as f:
        return yaml.load(f, Loader=yaml.CLoader)


def get_required_field(data: Dict[str, Any], field: str) -> Any:
    if field not in data:
        raise MissingFieldError(data, field)
    return data[field]


def prepare_checklist_item_data(checklist_item: Dict[str, Any], mapper: EntityMapper):
    return {
        "text": checklist_item["text"],
        "checked": checklist_item.get("checked"),
        "assignee": mapper.get_user(checklist_item.get("assignee")),
        "deadline": checklist_item.get("deadline"),
    }


DATE_PATTERN = "%Y-%m-%dT%H:%M:%S.%f%z"


def str_to_datetime(date_str: str) -> datetime:
    if date_str is None:
        return None
    return datetime.strptime(date_str, DATE_PATTERN)


def datetime_to_str(datetime_obj: datetime) -> str:
    if datetime_obj is None:
        return None
    return datetime_obj.strftime(DATE_PATTERN)


def map_custom_user_field_value(field_obj: Resource, value: Any, mapper: EntityMapper) -> Any:
    if (
        field_obj.schema["type"] == "user"
        or field_obj.schema.get("items") == "user"
    ):
        if isinstance(value, list):
            return [mapper.get_user(u) for u in value]
        else:
            return mapper.get_user(value)
    else:
        return value


def clean_empty(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            cleaned_value = clean_empty(value)
            if cleaned_value not in (None, "", [], {}):
                result[key] = cleaned_value
        return result
    elif isinstance(data, list):
        result = []
        for item in data:
            cleaned_item = clean_empty(item)
            if cleaned_item not in (None, "", [], {}):
                result.append(cleaned_item)
        return result
    else:
        return data


def retry(
    max_attempts: int = 2,
    delay: float = 1,
    backoff: float = 1,
    exceptions: Union[Type[Exception], Tuple[Type[Exception], ...]] = Exception,
) -> Callable[[Callable], Callable]:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == max_attempts:
                        raise

                    if current_delay > 0:
                        time.sleep(current_delay)
                        current_delay *= backoff

        return wrapper
    return decorator
