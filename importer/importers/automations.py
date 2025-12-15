import logging
from typing import Any, Callable, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .base import CachedImporter
from .entities import ProjectImporter
from .fields import GlobalFieldImporter, LocalFieldImporter
from .queue_objects import ComponentImporter, VersionImporter
from ..exceptions import MissingFieldError
from ..mapper import EntityMapper
from ..utils import prepare_checklist_item_data, map_custom_user_field_value


logger = logging.getLogger(__name__)


class AutomationImporter(CachedImporter):
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        queue_obj: Resource,
        local_field_importer: LocalFieldImporter,
        global_field_importer: GlobalFieldImporter,
        component_importer: ComponentImporter,
        version_importer: VersionImporter,
        project_importer: ProjectImporter,
    ) -> None:
        super().__init__(client, mapper)
        self._queue_obj = queue_obj
        self._local_field_importer = local_field_importer
        self._global_field_importer = global_field_importer
        self._component_importer = component_importer
        self._version_importer = version_importer
        self._project_importer = project_importer

    def _get_key(self, input_data: Dict[str, Any]) -> Optional[str]:
        if "name" not in input_data:
            logger.error(
                f"Missing name in {self.object_name}. "
                f"Queue key: {self._queue_obj.key}, "
                f"Input data: {input_data}"
            )
            return None

        return "{}-{}".format(self._queue_obj.key, input_data["name"])

    def _prepare_field_update(self, field_obj: Resource, update: Any) -> Optional[Dict[str, Any]]:
        if update is None:
            return None

        update_data = {}
        for operation, value in update.items():
            if field_obj.key == "project":
                update_data.update(
                    {"set": {"any": {operation: self._prepare_field_value(field_obj, value)}}}
                )  # Handling special structure for project updates
            else:
                update_data[operation] = self._prepare_field_value(field_obj, value)
        return update_data

    def _prepare_field_value(self, field_obj: Resource, value: Any) -> Any:
        def process_value(val: Any, processor_func: Callable) -> Any:
            if val is None:
                return None
            if isinstance(val, list):
                return [processor_func(v) for v in val]
            return processor_func(val)

        match field_obj.key:
            case "status":
                return process_value(value, self._mapper.get_status)
            case "priority":
                return process_value(value, self._mapper.get_priority)
            case "type":
                return process_value(value, self._mapper.get_issue_type)
            case "resolution":
                return process_value(value, self._mapper.get_resolution)
            case "parent":
                return process_value(value, self._mapper.get_issue_key)
            case "queue" | "lastQueue" | "previousQueue":
                return process_value(value, self._mapper.get_queue)
            case "project":
                return process_value(
                    value,
                    lambda v: self._project_importer.get_project_by_id(str(v)).shortId,
                )
            case "components":
                return process_value(
                    value,
                    lambda v: self._component_importer.get_component_by_name(v).id,
                )
            case "fixVersions" | "affectedVersions":
                return process_value(
                    value,
                    lambda v: self._version_importer.get_version_by_name(v).id,
                )
            case _:
                return map_custom_user_field_value(field_obj, value, self._mapper)

    def _get_or_create_field(self, field_key: str) -> Resource:
        if field_key.startswith("local_"):
            return self._local_field_importer.get_local_field(field_key)

        return self._global_field_importer.get_or_create(field_key)

    def _prepare_actions_data(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        prepared_actions = []
        for action_data in actions:
            action_type = action_data["type"]

            prepared_data = {"type": action_type}
            match action_type:
                case "Transition":
                    prepared_data["status"] = self._mapper.get_status(action_data.get("status"))
                case "CalculateFormula":
                    prepared_data["formula"] = action_data.get("formula")
                    prepared_data["resultField"] = self._get_or_create_field(action_data.get("resultField")).id
                case "Update":
                    prepared_data["update"] = {}
                    for field_key, update in action_data.get("update", {}).items():
                        field_obj = self._get_or_create_field(field_key)
                        prepared_data["update"][field_obj.id] = self._prepare_field_update(field_obj, update)
                case "Move":
                    prepared_data["queue"] = self._mapper.get_queue(action_data.get("queue"))
                case "CreateComment":
                    prepared_data["text"] = action_data["text"]
                    prepared_data["fromRobot"] = action_data.get("fromRobot")
                case "CreateChecklist":
                    prepared_data["checklistItems"] = [
                        prepare_checklist_item_data(item, self._mapper)
                        for item in action_data.get("checklistItems", [])
                    ]
                case "Webhook":
                    prepared_data.update(
                        {
                            "endpoint": action_data.get("endpoint"),
                            "authContext": action_data.get("authContext"),
                            "method": action_data.get("method"),
                            "contentType": action_data.get("contentType"),
                            "headers": action_data.get("headers"),
                            "body": action_data.get("body"),
                        }
                    )
                case "CreateIssue":
                    prepared_data.update(
                        {
                            "queue": self._mapper.get_queue(action_data.get("queue")),
                            "summary": action_data.get("summary"),
                            "linkWithInitialIssue": action_data.get("linkWithInitialIssue"),
                            "fromRobot": action_data.get("fromRobot", False),
                        }
                    )

                    field_templates = action_data.get("fieldTemplates", {})
                    prepared_data["fieldTemplates"] = {
                        "followers": [self._mapper.get_user(u) for u in field_templates.get("followers", [])],
                        "assignee": self._mapper.get_user(field_templates.get("assignee")),
                        "description": field_templates.get("description"),
                        "dueDate": field_templates.get("dueDate"),
                        "priority": self._mapper.get_priority(field_templates.get("priority")),
                        "type": self._mapper.get_issue_type(field_templates.get("type")),
                        "tags": field_templates.get("tags", []),
                    }

            prepared_actions.append(prepared_data)
        return prepared_actions


class MacrosImporter(AutomationImporter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for macros in self._queue_obj.macros:
            self._put_to_cache("{}-{}".format(macros.queue.key, macros.name), macros)

    @property
    def object_name(self) -> str:
        return "Macros"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        issue_update_data = {}
        for field_key, update in input_data.get("issueUpdate", {}).items():
            field_obj = self._get_or_create_field(field_key)
            issue_update_data[field_obj.key] = self._prepare_field_update(field_obj, update)

        return {
            "name": input_data["name"],
            "body": input_data.get("body"),
            "issueUpdate": issue_update_data,
        }

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._queue_obj.collection.macros.create(**prepared_data)


class TriggerImporter(AutomationImporter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for trigger in self._queue_obj.triggers:
            self._put_to_cache("{}-{}".format(trigger.queue.key, trigger.name), trigger)

    @property
    def object_name(self) -> str:
        return "Trigger"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": input_data["name"],
            "actions": self._prepare_actions_data(input_data["actions"]),
            "conditions": self._prepare_conditions_data(input_data.get("conditions", [])),
            "active": input_data.get("active"),
        }

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._queue_obj.collection.triggers.create(**prepared_data)

    def _prepare_conditions_data(self, conditions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prepared_conditions = []
        for condition in conditions:
            cond_type = condition["type"]
            prepared_data = {"type": cond_type}

            match cond_type:
                case "Or" | "And":
                    prepared_data["conditions"] = self._prepare_conditions_data(condition["conditions"])
                case (
                    "CommentNoneMatchCondition"
                    | "CommentStringNotMatchCondition"
                    | "CommentFullyMatchCondition"
                    | "CommentAnyMatchCondition"
                    | "CommentStringMatchCondition"
                ):
                    prepared_data.update(
                        {
                            "word": condition.get("word"),
                            "ignoreCase": condition.get("ignoreCase"),
                            "removeMarkup": condition.get("removeMarkup"),
                            "noMatchBefore": condition.get("noMatchBefore"),
                        }
                    )
                case "CommentMessageInternal" | "CommentMessageExternal" | "CommentAuthorNot" | "CommentAuthor":
                    if "user" in condition:
                        prepared_data["user"] = self._mapper.get_user(condition["user"])
                case "UpdatedLinkCondition" | "CreatedLinkCondition" | "RemovedLinkCondition":
                    prepared_data["relationship"] = condition.get("relationship", [])
                case (
                    "FieldChangedCondition"
                    | "FieldIsNotEmpty"
                    | "FieldIsEmpty"
                    | "FieldBecameEmpty"
                    | "FieldBecameNotEmpty"
                ):
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data["field"] = field_obj.id
                case (
                    "FieldEquals"
                    | "FieldBecameEqual"
                    | "DateEqualCondition"
                    | "DateGreaterCondition"
                    | "DateGreaterOrEqualCondition"
                    | "DateLessCondition"
                    | "DateLessOrEqualCondition"
                    | "UserInGroups"
                    | "UserNotInGroups"
                    | "Container.SizeGreater"
                    | "Container.SizeGreaterOrEquals"
                    | "Container.SizeLess"
                    | "Container.SizeLessOrEquals"
                    | "Container.SizeNotEquals"
                    | "Container.SizeEquals"
                    | "GreaterCondition"
                    | "GreaterOrEqualCondition"
                    | "LessCondition"
                    | "LessOrEqualCondition"
                    | "BecameGreaterCondition"
                    | "BecameGreaterOrEqualCondition"
                    | "BecameLessCondition"
                    | "BecameLessOrEqualCondition"
                ):
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data["field"] = field_obj.id
                    prepared_data["value"] = self._prepare_field_value(field_obj, condition.get("value"))
                case "ContainerContainsNone" | "ContainerContainsAll" | "ContainerContainsAny":
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data.update(
                        {
                            "field": field_obj.id,
                            "value": self._prepare_field_value(field_obj, condition.get("value")),
                            "noMatchBefore": condition.get("noMatchBefore"),
                        }
                    )
                case "ContainsNoneOfStrings" | "FieldEqualsString" | "ContainsAnyOfStrings":
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data.update(
                        {
                            "field": field_obj.id,
                            "value": self._prepare_field_value(field_obj, condition.get("value")),
                            "ignoreCase": condition.get("ignoreCase"),
                        }
                    )

            prepared_conditions.append(prepared_data)
        return prepared_conditions


class AutoactionImporter(AutomationImporter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for autoaction in self._queue_obj.collection.autoactions:
            self._put_to_cache("{}-{}".format(autoaction.queue.key, autoaction.name), autoaction)

    @property
    def object_name(self) -> str:
        return "Autoaction"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prepared_data = {
            "name": input_data["name"],
            "active": input_data.get("active"),
            "enableNotifications": input_data.get("enableNotifications"),
            "actions": self._prepare_actions_data(input_data["actions"]),
        }

        if input_data.get("filter") is not None:
            prepared_data["filter"] = self._prepare_filter_data(input_data["filter"])
        elif input_data.get("query") is not None:
            logger.warning("Autoaction with query might not work after import because fields in queries are not mapped. Please check it in Tracker.")
            prepared_data["query"] = input_data["query"]
        else:
            raise MissingFieldError(input_data, "filter or query")

        return prepared_data

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._queue_obj.collection.autoactions.create(**prepared_data)

    def _prepare_filter_data(self, filter_data: Dict[str, Any]):
        prepared_filter_data = {}
        for field_key, value in filter_data.items():
            field_obj = self._get_or_create_field(field_key)
            prepared_filter_data[field_obj.id] = self._prepare_field_value(field_obj, value)
        return prepared_filter_data
