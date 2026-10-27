import logging
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from yandex_tracker_client.objects import Resource

from .base import GlobalObjectImporter
from ..exceptions import (
    StatusNotCreatedError,
    IssueTypeNotCreatedError,
    ResolutionNotCreatedError
)


logger = logging.getLogger(__name__)


class NamedGlobalObjectImporter(GlobalObjectImporter, ABC):
    """Статус или тип задачи. API не создаёт объект, название которого совпадает с названием
    существующего на том же языке: ключ выгрузки, которого нет ни в целевой организации, ни в файле
    сопоставлений, сопоставляется с таким объектом."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._existing: Optional[List[Tuple[Any, Set[Tuple[str, str]]]]] = None
        self._existing_lock = threading.Lock()

    def _find_by_name(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        key = input_data["key"]
        if self._is_mapped(key):
            return None

        names = self._names(input_data.get("name"))
        matches = [(obj, obj_names) for obj, obj_names in self._existing_objects() if names & obj_names]
        if not matches:
            return None
        if len(matches) > 1:
            logger.error(
                f"{self.object_name} {key} has names of several existing objects "
                f"{[obj.key for obj, _ in matches]}, map it in mapping/{self._mapping_file}"
            )
            return None

        obj, obj_names = matches[0]
        self._map(key, obj.key)
        same = ", ".join(f"{language}: {name}" for language, name in sorted(names & obj_names))
        logger.warning(
            f"{self.object_name} {key} {input_data.get('name')} is mapped to the existing {obj.key} by the same name "
            f"({same}); map it in mapping/{self._mapping_file} to change this"
        )
        self._check_existing(input_data, obj)
        return obj

    def _try_creating_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        obj = super()._try_creating_object(input_data)
        if obj is not None:
            with self._existing_lock:
                if self._existing is not None:
                    self._existing.append((obj, self._names(input_data.get("name"))))
        return obj

    def _existing_objects(self) -> List[Tuple[Any, Set[Tuple[str, str]]]]:
        with self._existing_lock:
            if self._existing is None:
                try:
                    self._existing = [(obj, self._names(obj.name)) for obj in self._list_existing()]
                except Exception:
                    logger.exception(f"Error loading {self.object_name} objects, they are not looked up by name")
                    self._existing = []
            return self._existing

    def _names(self, name: Any) -> Set[Tuple[str, str]]:
        if not isinstance(name, dict):
            return set()
        return {(language, self._comparable(value)) for language, value in name.items() if value}

    @staticmethod
    def _comparable(name: str) -> str:
        """Название в том виде, в каком API сравнивает его с занятыми."""
        return name

    def _check_existing(self, input_data: Dict[str, Any], obj: Resource) -> None:
        pass

    @property
    @abstractmethod
    def _mapping_file(self) -> str:
        pass

    @abstractmethod
    def _list_existing(self) -> Iterable[Resource]:
        """Все объекты организации с названиями на обоих языках."""

    @abstractmethod
    def _is_mapped(self, key: str) -> bool:
        pass

    @abstractmethod
    def _map(self, key: str, target_key: str) -> None:
        pass


class StatusImporter(NamedGlobalObjectImporter):
    error_class = StatusNotCreatedError

    @property
    def object_name(self) -> str:
        return "Status"

    @property
    def _filename(self) -> str:
        return "statuses.yaml"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": {
                "ru": input_data["name"]["ru"],
                "en": input_data["name"]["en"],
            },
            "key": self._mapper.get_status(input_data["key"]),
            "type": input_data.get("type"),
            "description": input_data.get("description"),
        }

    def _get_object(self, key: str) -> Optional[Resource]:
        obj = super()._get_object(key)
        if obj is not None:
            return obj

        return self._client.statuses.get(self._get_key(key))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.statuses.create(**prepared_data)

    def _get_key(self, key: str) -> str:
        return self._mapper.get_status(key)

    @property
    def _mapping_file(self) -> str:
        return "statuses.cfg"

    def _list_existing(self) -> Iterable[Resource]:
        return self._client.statuses.get_all(localized=False)

    @staticmethod
    def _comparable(name: str) -> str:
        # названия статусов API сравнивает без учёта регистра и лишних пробелов, названия типов задач — точно
        return re.sub(r"[ \t\n\x0b\f\r]+", " ", name.strip(" \t\n\x0b\f\r")).lower()

    def _is_mapped(self, key: str) -> bool:
        return self._mapper.is_status_mapped(key)

    def _map(self, key: str, target_key: str) -> None:
        self._mapper.map_status(key, target_key)

    def _check_existing(self, input_data: Dict[str, Any], obj: Resource) -> None:
        existing_type = getattr(obj, "type", None)
        if input_data.get("type") and existing_type and input_data["type"] != existing_type:
            logger.warning(
                f"Status {input_data['key']} has type {input_data['type']}, the existing {obj.key} has type {existing_type}"
            )


class IssueTypeImporter(NamedGlobalObjectImporter):
    error_class = IssueTypeNotCreatedError

    @property
    def object_name(self) -> str:
        return "Issue Type"

    @property
    def _filename(self) -> str:
        return "issue_types.yaml"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": {
                "ru": input_data["name"]["ru"],
                "en": input_data["name"]["en"],
            },
            "key": self._mapper.get_issue_type(input_data["key"]),
            "description": input_data.get("description"),
        }

    def _get_object(self, key: str) -> Optional[Resource]:
        obj = super()._get_object(key)
        if obj is not None:
            return obj

        return self._client.issue_types.get(self._get_key(key))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.issue_types.create(**prepared_data)

    def _get_key(self, key: str) -> str:
        return self._mapper.get_issue_type(key)

    @property
    def _mapping_file(self) -> str:
        return "issue_types.cfg"

    def _list_existing(self) -> Iterable[Resource]:
        return self._client.issue_types.get_all(localized=False)

    def _is_mapped(self, key: str) -> bool:
        return self._mapper.is_issue_type_mapped(key)

    def _map(self, key: str, target_key: str) -> None:
        self._mapper.map_issue_type(key, target_key)


class ResolutionImporter(GlobalObjectImporter):
    error_class = ResolutionNotCreatedError

    @property
    def object_name(self) -> str:
        return "Resolution"

    @property
    def _filename(self) -> str:
        return "resolutions.yaml"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": self._mapper.get_resolution(input_data["key"]),
            "name": {
                "ru": input_data["name"]["ru"],
                "en": input_data["name"]["en"],
            },
            "description": input_data.get("description"),
        }

    def _get_object(self, key: str) -> Optional[Resource]:
        obj = super()._get_object(key)
        if obj is not None:
            return obj

        return self._client.resolutions.get(self._get_key(key))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.resolutions.create(**prepared_data)

    def _get_key(self, key: str) -> str:
        return self._mapper.get_resolution(key)
