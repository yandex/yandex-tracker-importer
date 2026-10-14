import logging
import os
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import Conflict
from yandex_tracker_client.objects import Resource

from .base import CachedImporter
from .fields import GlobalFieldImporter
from ..exceptions import FilterNameConflictError, LocalFieldNotSupportedError
from ..mapper import EntityMapper
from ..utils import prepare_field_value


logger = logging.getLogger(__name__)

STATE_DIR = "state"
FILTERS_STATE_FILE = "filters.cfg"


class FilterImporter(CachedImporter):
    """Переносит сохранённые фильтры.

    Перечисления фильтров в API нет, поэтому найти ранее созданный фильтр в целевой организации
    нечем: соответствие «исходный идентификатор — созданный» ведётся файлом в каталоге `state`.
    Запись добавляется сразу после создания фильтра, чтобы прерванный прогон не оставил фильтр,
    о котором импортёр забудет, и не создал его повторно."""

    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        global_field_importer: GlobalFieldImporter,
        state_path: str,
    ):
        super().__init__(client, mapper)
        self._global_field_importer = global_field_importer
        self._state_file = os.path.join(state_path, STATE_DIR, FILTERS_STATE_FILE)
        self._imported_ids = self._load_state()

    @property
    def object_name(self) -> str:
        return "Filter"

    def _load_state(self) -> Dict[str, str]:
        if not os.path.exists(self._state_file):
            return {}

        imported_ids = {}
        with open(self._state_file) as state:
            for line in state:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                source_id, _, created_id = line.partition(":")
                imported_ids[source_id.strip()] = created_id.strip()

        return imported_ids

    def _save_state(self, source_id: str, created_id: str) -> None:
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        with open(self._state_file, "a") as state:
            state.write(f"{source_id}:{created_id}\n")
            state.flush()
            os.fsync(state.fileno())

        self._imported_ids[source_id] = created_id

    def _get_key(self, input_data: Dict[str, Any]) -> str:
        return str(input_data["id"])

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        created_id = self._imported_ids.get(self._get_key(input_data))
        if created_id is None:
            return None

        return self._client.filters.get(created_id)

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        source_id = prepared_data.pop("source_id")
        try:
            created = self._client.filters.create(**prepared_data)
        except Conflict:
            # названия фильтров уникальны у владельца: обычно это фильтр прошлого прогона, но
            # найти его в целевой организации нечем — перечисления фильтров в API нет
            raise FilterNameConflictError(prepared_data["name"]) from None

        self._save_state(source_id, str(created.id))

        return created

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source_id": self._get_key(input_data),
            "name": input_data["name"],
            "query": input_data.get("query"),
            "filter": self._prepare_filter_data(input_data.get("filter")),
            "sorts": self._prepare_sorts_data(input_data.get("sorts")),
            "fields": [self._get_field_id(key) for key in input_data.get("fields") or []],
            "groupBy": self._get_field_id(input_data.get("groupBy")),
            "filterType": input_data.get("filterType"),
        }

    def _prepare_filter_data(self, filter_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not filter_data:
            return None

        prepared_filter_data = {}
        for field_key, value in filter_data.items():
            field_obj = self._get_field(field_key)
            prepared_filter_data[field_obj.id] = prepare_field_value(field_obj, value, self._mapper)

        return prepared_filter_data

    def _prepare_sorts_data(self, sorts: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        """В выгрузке направление записано признаком `ascending`, а создание фильтра принимает
        его под именем `isAscending`."""
        if not sorts:
            return None

        return [
            {
                "field": self._get_field_id(sort["field"]),
                "isAscending": sort.get("ascending", True),
            }
            for sort in sorts
        ]

    def _get_field_id(self, field_key: Optional[str]) -> Optional[str]:
        if not field_key:
            return None

        return self._get_field(field_key).id

    def _get_field(self, field_key: str) -> Resource:
        if field_key.startswith("local_"):
            # локальное поле принадлежит очереди, а фильтр и виджет к очереди не привязаны
            raise LocalFieldNotSupportedError(field_key)

        return self._global_field_importer.get_or_create(field_key)
