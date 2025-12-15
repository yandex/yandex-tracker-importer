from typing import Any, Dict, Optional

from yandex_tracker_client.objects import Resource

from .base import GlobalObjectImporter
from ..exceptions import (
    StatusNotCreatedError,
    IssueTypeNotCreatedError,
    ResolutionNotCreatedError
)


class StatusImporter(GlobalObjectImporter):
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


class IssueTypeImporter(GlobalObjectImporter):
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
