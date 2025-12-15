import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource
from yandex_tracker_client.exceptions import NotFound

from ..exceptions import ObjectNotCreatedError
from ..mapper import EntityMapper
from ..utils import load_yaml_file


logger = logging.getLogger(__name__)


class GetOrCreateImporter(ABC):
    def __init__(self, client: TrackerClient, mapper: EntityMapper):
        self._client = client
        self._mapper = mapper

    def get_or_create(self, input_data: Any) -> Resource:
        obj = self._try_getting_object(input_data)
        if obj is not None:
            return obj

        return self._try_creating_object(input_data)

    def _try_getting_object(self, input_data: Any) -> Optional[Resource]:
        try:
            obj = self._get_object(input_data)
            if obj is not None:
                logger.info(f"The object {self.object_name} with data {input_data} exists")
                return obj
        except NotFound:
            logger.info(f"The {self.object_name} with data {input_data} was not found, trying to create")
        except Exception:
            logger.exception(f"Error searching {self.object_name} with data: {input_data}")
        return None

    def _try_creating_object(self, input_data: Any) -> Optional[Resource]:
        try:
            prepared_data = self._prepare_data(input_data)
        except Exception:
            logger.exception(f"Error preparing data for {self.object_name} with data: {input_data}")
            return None

        try:
            return self._create_object(prepared_data)
        except Exception:
            logger.exception(f"Error creating {self.object_name} with data: {prepared_data}")
            return None

    @abstractmethod
    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        pass

    @abstractmethod
    def _get_object(self, input_data: Any) -> Optional[Resource]:
        pass

    @property
    @abstractmethod
    def object_name(self) -> str:
        pass


class CacheMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache = {}

    def get_from_cache(self, key: str) -> Any:
        return self._cache.get(key)

    def _put_to_cache(self, key: str, value: Any) -> None:
        self._cache[key] = value


class CachedImporter(CacheMixin, GetOrCreateImporter, ABC):
    def __init__(self, client: TrackerClient, mapper: EntityMapper):
        super().__init__(client, mapper)

    def get_or_create(self, input_data: Any) -> Optional[Resource]:
        obj = super().get_or_create(input_data)

        if obj is not None:
            self._put_to_cache(self._get_key(input_data), obj)
            return obj

        return None

    def _get_object(self, input_data: Any) -> Optional[Resource]:
        return self.get_from_cache(self._get_key(input_data))

    @abstractmethod
    def _get_key(self, input_data: Any) -> Optional[str]:
        pass


class GlobalObjectImporter(CachedImporter, ABC):
    error_class = ObjectNotCreatedError

    def __init__(self, client: TrackerClient, mapper: EntityMapper, data_path: str):
        super().__init__(client, mapper)
        global_object_path = os.path.join(data_path, self._filename)
        self._objects_data = (
            self._load_objects_data(global_object_path)
            if os.path.exists(global_object_path)
            else {}
        )

    def _load_objects_data(self, global_object_path: str) -> Dict[str, Dict[str, Any]]:
        return {
            obj_data["key"]: obj_data
            for obj_data in load_yaml_file(global_object_path) or []
            if obj_data.get("key")
        }

    def get_or_create(self, key: str) -> Optional[Resource]:
        obj = self._try_getting_object(key)
        if obj is not None:
            self._put_to_cache(self._get_key(key), obj)
            return obj

        if key not in self._objects_data:
            logger.error(f"{self.object_name} with key {key} not found in {self._filename}")
            raise self.error_class(key)

        obj = self._try_creating_object(self._objects_data[key])
        if obj is None:
            raise self.error_class(key)

        self._put_to_cache(self._get_key(key), obj)
        return obj

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass
