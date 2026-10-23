import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Dict, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import NotFound
from yandex_tracker_client.objects import Resource

from .base import CachedImporter, GlobalObjectImporter
from ..exceptions import (
    LocalFieldNotExistsError,
    GlobalFieldNotCreatedError
)
from ..mapper import EntityMapper
from ..utils import clean_empty, get_optional, load_yaml_file


logger = logging.getLogger(__name__)

# категория системных полей Трекера
SYSTEM_FIELDS_CATEGORY_ID = "000000000000000000000001"


class CategoryImporter(GlobalObjectImporter):
    def __init__(self, client: TrackerClient, mapper: EntityMapper, data_path: str):
        super().__init__(client, mapper, data_path)
        for category in self._client.field_categories.get_all():
            self._put_to_cache(category.name, category)

    def _load_objects_data(self, global_object_path: str) -> Dict[str, Dict[str, Any]]:
        return {
            obj_data["name"]["ru"]: obj_data
            for obj_data in load_yaml_file(global_object_path) or []
            if obj_data.get("name", {}).get("ru")
        }

    @property
    def object_name(self) -> str:
        return "Category"

    @property
    def _filename(self) -> str:
        return "categories.yaml"

    def _prepare_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return clean_empty({
            "name": {
                "en": data["name"]["en"],
                "ru": data["name"]["ru"],
            },
            "order": data["order"],
            "description": data.get("description")
        })

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.field_categories.create(**prepared_data)

    def _get_key(self, key: str) -> Optional[str]:
        return key


class FieldImporter(CachedImporter, ABC):
    _type_relation_map = {
        "date": "ru.yandex.startrek.core.fields.DateFieldType",
        "datetime": "ru.yandex.startrek.core.fields.DateTimeFieldType",
        "string": "ru.yandex.startrek.core.fields.StringFieldType",
        "text": "ru.yandex.startrek.core.fields.TextFieldType",
        "float": "ru.yandex.startrek.core.fields.FloatFieldType",
        "integer": "ru.yandex.startrek.core.fields.IntegerFieldType",
        "user": "ru.yandex.startrek.core.fields.UserFieldType",
        "uri": "ru.yandex.startrek.core.fields.UriFieldType",
    }

    def __init__(self, client: TrackerClient, mapper: EntityMapper, category_importer: CategoryImporter):
        CachedImporter.__init__(self, client, mapper)
        self._category_importer = category_importer

    def _try_creating_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        category = self._category_importer.get_or_create(input_data.get("category"))
        if category is None:
            return None

        return super()._try_creating_object(input_data)

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return clean_empty({
            "id": self._mapper.get_field(input_data["key"]),
            "name": {
                "en": input_data["name"]["en"],
                "ru": input_data["name"]["ru"],
            },
            "category": self._category_importer.get_from_cache(input_data["category"]).id,
            "type": self._get_field_type(input_data),
            "container": input_data["schema"]["type"] == "array",
            "optionsProvider": input_data.get("optionsProvider"),
            "description": input_data.get("description"),
            "readonly": input_data.get("readonly"),
            "hidden": input_data.get("hidden"),
        })

    @contextmanager
    def writable(self, field_obj: Resource, purpose: str):
        """Снимает запрет редактирования на время одной операции и сразу возвращает его.

        Поле с запретом API не принимает в действии «Вычислить по формуле», а глобальное поле —
        ещё и в действиях «Изменить значения полей» и «Создать задачу» и в макросе. Запрет снимается только у поля,
        которое его сейчас имеет, и возвращается, даже если операция завершилась ошибкой."""
        if not self._is_readonly(field_obj):
            yield
            return

        try:
            self._set_readonly(field_obj, False)
        except NotFound:
            # у системного поля, например статуса, запрет редактирования не меняется
            if get_optional(get_optional(field_obj, "category"), "id") != SYSTEM_FIELDS_CATEGORY_ID:
                raise
            yield
            return
        logger.info(f"Field {field_obj.id} is made editable while {purpose}")
        try:
            yield
        finally:
            try:
                self._set_readonly(field_obj, True)
            except Exception:
                logger.exception(f"Error restoring readonly of field {field_obj.id}, set it manually")

    def _is_readonly(self, field_obj: Resource) -> bool:
        return bool(get_optional(field_obj, "readonly"))

    def _set_readonly(self, field_obj: Resource, readonly: bool) -> None:
        raise NotImplementedError

    def _get_field_type(self, data: Dict[str, Any]) -> str:
        if data["schema"]["type"] == "array":
            return self._type_relation_map.get(data["schema"]["items"])
        return self._type_relation_map.get(data["schema"]["type"])


class LocalFieldImporter(FieldImporter):
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        queue_obj: Resource,
        category_importer: CategoryImporter
    ):
        super().__init__(client, mapper, category_importer)
        self._queue_obj = queue_obj
        for field in self._queue_obj.local_fields:
            self._put_to_cache("{}-{}".format(field.queue.key, field.key), field)

    @property
    def object_name(self) -> str:
        return "Local Field"

    def get_local_field(self, key: str) -> Resource:
        field_obj = self.get_from_cache(key.replace("local_", f"{self._queue_obj.key}-"))
        if field_obj is None:
            raise LocalFieldNotExistsError(key, self._queue_obj.key)
        else:
            return field_obj

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._queue_obj.collection.local_fields.create(**prepared_data)

    def _set_readonly(self, field_obj: Resource, readonly: bool) -> None:
        # локальное поле изменяется по ключу в очереди
        self._queue_obj.collection.local_fields.update_field(field_obj.key, readonly=readonly)

    def _get_key(self, input_data: Dict[str, Any]) -> Optional[str]:
        if "key" not in input_data:
            logger.error(
                f"Missing key in {self.object_name}. "
                f"Queue key: {self._queue_obj.key}, "
                f"Input data: {input_data}"
            )
            return None

        return "{}-{}".format(self._queue_obj.key, input_data["key"])


class GlobalFieldImporter(FieldImporter, GlobalObjectImporter):
    error_class = GlobalFieldNotCreatedError

    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        data_path: str,
        category_importer: CategoryImporter
    ):
        GlobalObjectImporter.__init__(self, client, mapper, data_path)
        FieldImporter.__init__(self, client, mapper, category_importer)

    @property
    def object_name(self) -> str:
        return "GlobalField"

    def _set_readonly(self, field_obj: Resource, readonly: bool) -> None:
        # изменение глобального поля требует его текущей версии
        self._client.fields.get(field_obj.id).update(readonly=readonly)

    @property
    def _filename(self) -> str:
        return "global_fields.yaml"

    def get_or_create(self, key: str) -> Optional[Resource]:
        return GlobalObjectImporter.get_or_create(self, key)

    def _get_object(self, key: str) -> Optional[Resource]:
        obj = GlobalObjectImporter._get_object(self, key)
        if obj is not None:
            return obj
        return self._client.fields.get(self._get_key(key))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.fields.create(**prepared_data)

    def _get_key(self, key: str) -> str:
        return self._mapper.get_field(key)
