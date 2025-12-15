import logging
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .base import CachedImporter, GetOrCreateImporter
from ..constants import ALL_PARTICIPANTS_GROUP_ID
from ..exceptions import (
    ComponentNotExistsError,
    VersionNotExistsError
)
from ..mapper import EntityMapper
from ..utils import clean_empty


logger = logging.getLogger(__name__)


class WorkflowImporter(GetOrCreateImporter):
    DEFAULT_WORKFLOW_ID = "WDEFAULT"
    DEFAULT_WORKFLOW_DATA = {
        "key": DEFAULT_WORKFLOW_ID,
        "name": "Default Workflow",
        "initialAction": {
            "name": {
                "en": "Create",
                "ru": "Создать",
            },
            "target": "open",
        },
        "steps": [
            {
                "status": "open",
                "actions": [
                    {
                        "name": {
                            "en": "Create",
                            "ru": "Создать",
                        },
                        "target": "open",
                    },
                ],
            },
        ],
    }

    def __init__(self, client: TrackerClient, mapper: EntityMapper) -> None:
        super().__init__(client, mapper)
        self._default_workflow = self.get_or_create(self.DEFAULT_WORKFLOW_DATA)

    @property
    def object_name(self) -> str:
        return "Workflow"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        mapped_queue_key = self._mapper.get_queue(input_data.get("queue"))
        return clean_empty({
            "id": _get_workflow_id(mapped_queue_key, input_data["key"]),
            "name": input_data["name"],
            "queue": mapped_queue_key,
            "initialAction": self._prepare_action_data(input_data["initialAction"]),
            "steps": self._prepare_steps_data(input_data.get("steps", [])),
            "type": input_data.get("type")
        })

    def _prepare_action_data(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        if not action_data:
            return action_data

        return {
            "name": {
                "en": action_data["name"]["en"],
                "ru": action_data["name"]["ru"],
            },
            "target": self._mapper.get_status(action_data["target"])
        }

    def _prepare_steps_data(self, steps_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "status": self._mapper.get_status(step["status"]),
                "actions": [self._prepare_action_data(action) for action in step.get("actions", [])],
                "metaAction": self._prepare_action_data(step.get("metaAction", {}))
            }
            for step in steps_data
        ]

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        mapped_queue_key = self._mapper.get_queue(input_data.get("queue"))
        workflow_id = _get_workflow_id(mapped_queue_key, input_data["key"])
        return self._client.workflows.get(workflow_id)

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.workflows.create(**prepared_data)


class QueueImporter(GetOrCreateImporter):
    _group_name_to_id_map = dict()

    def __init__(self, client: TrackerClient, mapper: EntityMapper) -> None:
        super().__init__(client, mapper)

        for group in self._client.groups:
            self._group_name_to_id_map[group.name] = group.id

    @property
    def object_name(self) -> str:
        return "Queue"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return clean_empty({
            "key": self._mapper.get_queue(input_data["key"]),
            "name": input_data["name"],
            "defaultType": self._mapper.get_issue_type(input_data["defaultType"]),
            "defaultPriority": self._mapper.get_priority(input_data["defaultPriority"]),
            "lead": self._mapper.get_user(input_data["lead"]),
            "teamUsers": [self._mapper.get_user(u) for u in input_data.get("teamUsers", [])],
            "issueTypesConfig": [  # default issue type config to be replaced later
                {
                    "issueType": "task",
                    "workflow": WorkflowImporter.DEFAULT_WORKFLOW_ID,
                },
            ],
            "description": input_data.get("description"),
            "assignAuto": input_data.get("assignAuto"),
            "denyVoting": input_data.get("denyVoting"),
        })

    def _prepare_issue_types_config_data(self, issue_types_config: List[Dict[str, Any]], queue_key: str) -> List[Dict[str, Any]]:
        return [
            {
                "issueType": self._mapper.get_issue_type(issue_type_config["issueType"]),
                "workflow": _get_workflow_id(self._mapper.get_queue(queue_key), issue_type_config["workflow"]),
                "resolutions": [
                    self._mapper.get_resolution(r)
                    for r in issue_type_config.get("resolutions", [])
                ],
            } for issue_type_config in issue_types_config
        ]

    def _get_object(self, data: Dict[str, Any]) -> Optional[Resource]:
        return self._client.queues.get(self._mapper.get_queue(data["key"]))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.queues.create(**prepared_data)

    def import_issue_types_config(self, queue_obj: Resource, queue_data: Dict[str, Any]) -> None:
        try:
            update_data = {
                "issueTypesConfig": self._prepare_issue_types_config_data(queue_data.get("issueTypesConfig", []), queue_data["key"]),
            }
            queue_obj.update(**update_data)
            logger.info(f"Successfully updated issue types config for queue {queue_obj.key}")
        except Exception:
            logger.exception(f"Error updating issue types config for queue {queue_obj.key}")

    def update_permissions(self, queue_obj: Resource, queue_data: Dict[str, Any]) -> None:
        """
        Добавить разрешения к очереди, сохранив существующие.
        Удалить группу "Все сотрудники", если её нет в импортируемых данных, чтобы избежать общего доступа при первичном импорте очереди
        """
        incoming_permissions = queue_data.get("permissions")
        if not incoming_permissions:
            return

        permissions_changes = {}
        permission_types = ["create", "read", "write", "grant"]
        for permission_type in permission_types:
            groups_add = {
                self._group_name_to_id_map.get(self._mapper.get_group(group))
                for group in incoming_permissions.get(permission_type, {}).get("groups", [])
            }

            permissions_changes[permission_type] = {
                "users": {
                    "add": list({
                        self._mapper.get_user(user)
                        for user in incoming_permissions.get(permission_type, {}).get("users", [])
                    }),
                },
                "roles": {
                    "add": incoming_permissions.get(permission_type, {}).get("roles", []),
                },
                "groups": {
                    "add": list(groups_add),
                    # Удалять группу "Все сотрудники", если она не добавляется
                    "remove": [ALL_PARTICIPANTS_GROUP_ID] if ALL_PARTICIPANTS_GROUP_ID not in groups_add else []
                },
            }

        permissions_changes = clean_empty(permissions_changes)
        if not permissions_changes:
            return

        try:
            self._client.queues[queue_obj.key].update_permissions(permissions_changes)
            logger.info(f"Successfully updated permissions for queue {queue_obj.key}")
        except Exception:
            logger.exception(
                f"Error updating permissions for queue {queue_obj.key} "
                f"with data: {permissions_changes}"
            )


class ComponentImporter(CachedImporter):
    def __init__(self, client: TrackerClient, mapper: EntityMapper, queue_obj: Resource) -> None:
        super().__init__(client, mapper)
        self._queue_obj = queue_obj
        for component in self._queue_obj.components:
            self._put_to_cache("{}-{}".format(component.queue.key, component.name), component)

    @property
    def object_name(self) -> str:
        return "Component"

    def get_component_by_name(self, name: str) -> Resource:
        component_obj = self.get_from_cache(f"{self._queue_obj.key}-{name}")
        if component_obj is None:
            raise ComponentNotExistsError(self._queue_obj.key, name)
        return component_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return clean_empty({
            "queue": self._queue_obj.key,
            "name": input_data["name"],
            "description": input_data.get("description"),
            "assignAuto": input_data.get("assignAuto"),
            "lead": self._mapper.get_user(input_data.get("lead")),
        })

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.components.create(**prepared_data)

    def _get_key(self, input_data: Dict[str, Any]) -> Optional[str]:
        if "name" not in input_data:
            logger.error(
                f"Missing name in {self.object_name}. "
                f"Queue key: {self._queue_obj.key}, "
                f"Input data: {input_data}"
            )
            return None

        return "{}-{}".format(self._queue_obj.key, input_data["name"])


class VersionImporter(CachedImporter):
    def __init__(self, client: TrackerClient, mapper: EntityMapper, queue_obj: Resource) -> None:
        super().__init__(client, mapper)
        self._queue_obj = queue_obj
        for version in self._queue_obj.versions:
            self._put_to_cache("{}-{}".format(version.queue.key, version.name), version)

    @property
    def object_name(self) -> str:
        return "Version"

    def get_version_by_name(self, name: str) -> Resource:
        version_obj = self.get_from_cache(f"{self._queue_obj.key}-{name}")
        if version_obj is None:
            raise VersionNotExistsError(self._queue_obj.key, name)
        return version_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return clean_empty({
            "queue": self._queue_obj.key,
            "name": input_data["name"],
            "description": input_data.get("description"),
            "startDate": input_data.get("startDate"),
            "dueDate": input_data.get("dueDate"),
        })

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.versions.create(**prepared_data)

    def _get_key(self, input_data: Dict[str, Any]) -> Optional[str]:
        if "name" not in input_data:
            logger.error(
                f"Missing name in {self.object_name}. "
                f"Queue key: {self._queue_obj.key}, "
                f"Input data: {input_data}"
            )
            return None

        return "{}-{}".format(self._queue_obj.key, input_data["name"])


def _get_workflow_id(mapped_queue_key: str, workflow_key: str) -> str:
    if not mapped_queue_key:
        return workflow_key
    return mapped_queue_key + workflow_key
