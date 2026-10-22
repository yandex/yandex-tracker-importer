import logging
import time
import yaml
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, Type, Tuple, Union

from yandex_tracker_client.objects import Resource

from .exceptions import BoardNotInDumpError, MissingFieldError
from .mapper import EntityMapper

logger = logging.getLogger(__name__)


def load_yaml_file(file_path: str) -> Any:
    """Выгрузка читается безопасным загрузчиком: он строит только простые данные и не создаёт
    объекты по служебным тегам вида `!!python/...` — выгрузке они не нужны."""
    with open(file_path, "r") as f:
        return yaml.load(f, Loader=yaml.CSafeLoader)


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
        # пункт чеклиста может быть не только текстовым, но и метрикой, критерием или фильтром
        "checklistItemType": checklist_item.get("checklistItemType"),
        "url": checklist_item.get("url"),
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


def prepare_field_value(
    field_obj: Resource,
    value: Any,
    mapper: "EntityMapper",
    project_importer: Any = None,
    component_importer: Any = None,
    version_importer: Any = None,
    board_importer: Any = None,
) -> Any:
    """Приводит значение поля к целевой организации.

    Компоненты, версии и проекты существуют в рамках очереди, поэтому их импортёры передаются не
    всегда: у фильтра очереди нет, и такие значения остаются как есть."""

    def process_value(val: Any, processor_func: Callable) -> Any:
        if val is None:
            return None
        if isinstance(val, list):
            return [processor_func(v) for v in val]
        return processor_func(val)

    match field_obj.key:
        case "status":
            return process_value(value, mapper.get_status)
        case "priority":
            return process_value(value, mapper.get_priority)
        case "type":
            return process_value(value, mapper.get_issue_type)
        case "resolution":
            return process_value(value, mapper.get_resolution)
        case "parent":
            return process_value(value, mapper.get_issue_key)
        case "queue" | "lastQueue" | "previousQueue":
            return process_value(value, mapper.get_queue)
        case "project" if project_importer is not None:
            return process_value(value, lambda v: project_importer.get_project_by_id(str(v)).shortId)
        case "components" if component_importer is not None:
            return process_value(value, lambda v: component_importer.get_component_by_name(v).id)
        case "fixVersions" | "affectedVersions" if version_importer is not None:
            return process_value(value, lambda v: version_importer.get_version_by_name(v).id)
        case "boards" | "sprint" if board_importer is not None:
            # номер перенесённой доски или спринта; без доски из выгрузки значение работало бы иначе
            getter = board_importer.get_board_id if field_obj.key == "boards" else board_importer.get_sprint_id

            def map_board(item: Any) -> Any:
                source_id = item.get("id") if isinstance(item, dict) else item
                target_id = getter(source_id)
                if target_id is None:
                    raise BoardNotInDumpError(f"Board or sprint {source_id}")
                return target_id

            return process_value(value, map_board)
        case "project" | "components" | "fixVersions" | "affectedVersions":
            logger.warning(
                f"Field {field_obj.key} is bound to a queue and is left as is: {value}"
            )
            return value
        case _:
            return map_custom_user_field_value(field_obj, value, mapper)


def get_optional(obj: Any, key: str, default: Any = None) -> Any:
    """Незаполненные поля API не возвращает вовсе, а обращение по ключу к отсутствующему полю
    ресурса завершается ошибкой, поэтому необязательные поля читаются через эту функцию."""
    if isinstance(obj, dict):
        return obj.get(key, default)

    return getattr(obj, key, default)
