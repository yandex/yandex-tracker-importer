import logging
from contextlib import ExitStack
from typing import Any, Callable, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .base import CachedImporter
from .entities import ProjectImporter
from .fields import GlobalFieldImporter, LocalFieldImporter
from .queue_objects import ComponentImporter, VersionImporter
from ..exceptions import (
    BoardNotInDumpError,
    FieldTemplatesNotSupportedError,
    GroupNotExistsError,
    MissingFieldError,
    UnsupportedActionError,
)
from ..groups import GroupResolver
from ..mapper import EntityMapper
from ..utils import get_required_field, prepare_checklist_item_data, prepare_field_value


logger = logging.getLogger(__name__)


def is_reference(value: Any) -> bool:
    """Ссылка на объект, а не набор операций: `{"id": 30, "display": "Проект"}`."""
    return isinstance(value, dict) and "id" in value and set(value) <= {"id", "display", "self", "key"}


LINKED_ISSUE_CONDITIONS = {"AllLinkedIssueCondition", "AnyLinkedIssueCondition", "NoneLinkedIssueCondition"}


def iterate_conditions(conditions: List[Dict[str, Any]]):
    for condition in conditions:
        yield condition
        yield from iterate_conditions(condition.get("conditions") or [])


def has_masked_value(value: Any) -> bool:
    """Пароль или токен, скрытый в выгрузке: `********` или вложенный объект с таким значением."""
    if isinstance(value, dict):
        return any(has_masked_value(item) for item in value.values())
    if isinstance(value, list):
        return any(has_masked_value(item) for item in value)
    return isinstance(value, str) and len(value) >= 3 and set(value) == {"*"}


def project_id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


def user_key(user: Any) -> Any:
    """Прежние версии экспорта записывали пользователя в части условий ссылкой с именем."""
    return user.get("display") if isinstance(user, dict) else user


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
        board_importer: Any = None,
    ) -> None:
        super().__init__(client, mapper)
        self._board_importer = board_importer
        self._queue_obj = queue_obj
        self._local_field_importer = local_field_importer
        self._global_field_importer = global_field_importer
        self._component_importer = component_importer
        self._version_importer = version_importer
        self._project_importer = project_importer
        # поля, в которые пишет создаваемая автоматизация: на время её создания с них снимается
        # запрет редактирования
        self._written_fields = {}

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
        any_update = {}
        for operation, value in update.items():
            if field_obj.key == "project" and self._is_project_target_update(value):
                # операция над основным или дополнительными проектами: {"set": {"primary": {"set": 30}}}
                update_data[operation] = {
                    target: self._prepare_project_operations(field_obj, operations)
                    for target, operations in value.items()
                }
            elif field_obj.key == "project":
                # операции над набором проектов собираются в одно обновление {"set": {"any": ...}}
                any_update[operation] = self._prepare_field_value(field_obj, value)
            else:
                update_data[operation] = self._prepare_field_value(field_obj, value)

        if any_update:
            update_data["set"] = {"any": any_update}
        return update_data

    @staticmethod
    def _is_project_target_update(value: Any) -> bool:
        return isinstance(value, dict) and bool(value) and set(value) <= {"primary", "secondary", "any"}

    def _prepare_project_operations(self, field_obj: Resource, operations: Any) -> Any:
        """Номера проектов исходной организации переводятся в номера целевой. Номер может быть
        записан ссылкой `{"id": 30}` — так обновление проектов записывали прежние версии экспорта."""
        if not isinstance(operations, dict) or is_reference(operations):
            # короткая форма без операции: {"primary": 30} или {"primary": {"id": 30}}
            value = project_id(operations)
            return None if value is None else self._prepare_field_value(field_obj, value)

        prepared = {}
        for operation, value in operations.items():
            ids = [project_id(item) for item in value] if isinstance(value, list) else project_id(value)
            prepared[operation] = None if ids is None else self._prepare_field_value(field_obj, ids)
        return prepared

    def _prepare_field_value(self, field_obj: Resource, value: Any) -> Any:
        return prepare_field_value(
            field_obj,
            value,
            self._mapper,
            project_importer=self._project_importer,
            component_importer=self._component_importer,
            version_importer=self._version_importer,
            board_importer=self._board_importer,
        )

    def _prepare_field_templates(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Шаблон полей задачи, которую создаёт действие. Компоненты, версии и локальные поля
        сопоставляются в очереди, которую импортируют сейчас, поэтому для задачи в другой очереди
        такие поля не переносятся: триггер не создаётся."""
        field_templates = action_data.get("fieldTemplates") or {}
        target_queue = action_data.get("queue")
        queue_bound = [
            key for key in field_templates
            if key.startswith("local_") or key in ("components", "fixVersions", "affectedVersions")
        ]
        if queue_bound and self._mapper.get_queue(target_queue) != self._queue_obj.key:
            raise FieldTemplatesNotSupportedError(target_queue, queue_bound)

        prepared = {}
        for field_key, value in field_templates.items():
            field_obj = self._get_or_create_field(field_key)
            self._add_written_field(field_obj, checked_for_local=False)
            if field_obj.key == "project" and self._is_project_target_update(value):
                # {"primary": 65, "secondary": [57]}: номера проектов переводятся в номера целевой организации
                prepared[field_obj.id] = {
                    target: None if ids is None else self._prepare_field_value(field_obj, ids)
                    for target, ids in value.items()
                }
            else:
                prepared[field_obj.id] = self._prepare_field_value(field_obj, value)
        return prepared

    def _get_active(self, input_data: Dict[str, Any]) -> Optional[bool]:
        """Автоматизация, которая после переноса работала бы иначе, создаётся неактивной: условие
        на связанные задачи переносится без фильтра по ним, а у вебхука скрыты значения
        переменных, секреты, пароли и токены — в том числе в заголовках."""
        active = input_data.get("active")
        reasons = []
        if any(
            condition.get("type", "").removesuffix("Proto") in LINKED_ISSUE_CONDITIONS
            for condition in iterate_conditions(input_data.get("conditions") or [])
        ):
            reasons.append("the filter on linked issues is not transferred")
        if any(
            action.get("type") == "Webhook"
            and (
                action.get("variables")
                or action.get("secrets")
                or has_masked_value([action.get("authContext"), action.get("headers")])
            )
            for action in input_data.get("actions") or []
        ):
            reasons.append("webhook secrets are masked in the dump, set them manually")
        if active and reasons:
            logger.warning(f"{self.object_name} {input_data.get('name')} is created inactive: {'; '.join(reasons)}")
            return False
        return active

    def _try_creating_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        self._written_fields = {}
        return super()._try_creating_object(input_data)

    def _add_written_field(self, field_obj: Resource, checked_for_local: bool) -> None:
        """Запрет редактирования API проверяет у глобального поля во всех действиях, которые в него
        пишут, а у локального — только в «Вычислить по формуле»."""
        if checked_for_local or not self._is_local_field(field_obj):
            self._written_fields[field_obj.id] = field_obj

    @staticmethod
    def _is_local_field(field_obj: Resource) -> bool:
        return "--" in str(field_obj.id)

    def _create_with_writable_fields(self, prepared_data: Dict[str, Any], create: Callable[[], Resource]) -> Resource:
        """Создаёт автоматизацию, сняв запрет редактирования с полей, в которые она пишет, и сразу
        возвращает запрет: поле с запретом API в таких действиях не принимает."""
        purpose = f"creating {self.object_name} {prepared_data.get('name')} of queue {self._queue_obj.key}"
        with ExitStack() as stack:
            for field_obj in self._written_fields.values():
                importer = self._local_field_importer if self._is_local_field(field_obj) else self._global_field_importer
                stack.enter_context(importer.writable(field_obj, purpose))
            return create()

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
                    field_obj = self._get_or_create_field(action_data.get("resultField"))
                    self._add_written_field(field_obj, checked_for_local=True)
                    prepared_data["resultField"] = field_obj.id
                case "Update":
                    prepared_data["update"] = {}
                    for field_key, update in action_data.get("update", {}).items():
                        field_obj = self._get_or_create_field(field_key)
                        self._add_written_field(field_obj, checked_for_local=False)
                        prepared_data["update"][field_obj.id] = self._prepare_field_update(field_obj, update)
                case "Move":
                    prepared_data["queue"] = self._mapper.get_queue(action_data.get("queue"))
                    if "moveAllFields" in action_data:
                        prepared_data["moveAllFields"] = action_data["moveAllFields"]
                case "SetActiveSprint":
                    board_id = self._board_importer.get_board_id(action_data.get("board")) if self._board_importer else None
                    if board_id is None:
                        # как функция перехода: доски нет в выгрузке, например старого типа
                        logger.warning(
                            f"Action SetActiveSprint of {self.object_name} {action_data.get('board')} is skipped: "
                            "its board is not in the dump or cannot be created"
                        )
                        continue
                    prepared_data["board"] = board_id
                case "CreateComment":
                    prepared_data["text"] = action_data.get("text")
                    prepared_data["fromRobot"] = action_data.get("fromRobot")
                    if action_data.get("summonees"):
                        prepared_data["summonees"] = [self._mapper.get_user(user) for user in action_data["summonees"]]
                    if action_data.get("summoneesFields"):
                        # поля, из которых берутся призываемые: автор, исполнитель, локальное поле очереди
                        prepared_data["summoneesFields"] = [
                            self._get_or_create_field(field_key).id for field_key in action_data["summoneesFields"]
                        ]
                    if action_data.get("email"):
                        # отправитель должен быть подключён к очереди в целевой организации
                        prepared_data["email"] = action_data["email"]
                case "CreateChecklist":
                    # прежние версии экспорта записывали отметку пункта как done
                    prepared_data["checklistItems"] = [
                        prepare_checklist_item_data({"checked": item.get("done"), **item}, self._mapper)
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
                    if action_data.get("retryConfig"):
                        prepared_data["retryConfig"] = action_data["retryConfig"]
                    if action_data.get("variables"):
                        # значения переменных в выгрузке скрыты, имена сохраняются
                        prepared_data["variables"] = action_data["variables"]
                case "CreateIssue":
                    prepared_data.update(
                        {
                            "queue": self._mapper.get_queue(action_data.get("queue")),
                            "summary": action_data.get("summary"),
                            "linkWithInitialIssue": action_data.get("linkWithInitialIssue"),
                            "fromRobot": action_data.get("fromRobot", False),
                            "fieldTemplates": self._prepare_field_templates(action_data),
                        }
                    )
                    for key in ("notify", "notifyAuthor"):
                        if key in action_data:
                            prepared_data[key] = action_data[key]
                case "Duty":
                    # действие ссылается на сервис и график дежурств исходной организации
                    raise UnsupportedActionError(action_type)

            prepared_actions.append(prepared_data)
        if actions and not prepared_actions:
            # все действия пропущены: автоматизация без действий не создаётся
            raise BoardNotInDumpError("Boards of all actions")
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
            self._add_written_field(field_obj, checked_for_local=False)
            issue_update_data[field_obj.key] = self._prepare_field_update(field_obj, update)

        return {
            "name": input_data["name"],
            "body": input_data.get("body"),
            # пустой issueUpdate API не принимает
            **({"issueUpdate": issue_update_data} if issue_update_data else {}),
            **({"description": input_data["description"]} if input_data.get("description") is not None else {}),
        }

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._create_with_writable_fields(
            prepared_data, lambda: self._queue_obj.collection.macros.create(**prepared_data)
        )


class TriggerImporter(AutomationImporter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._group_resolver = GroupResolver(self._client, self._mapper)
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
            "active": self._get_active(input_data),
            **{key: input_data[key] for key in ("description", "skipFailures") if input_data.get(key) is not None},
        }

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._create_with_writable_fields(
            prepared_data, lambda: self._queue_obj.collection.triggers.create(**prepared_data)
        )

    def _prepare_conditions_data(self, conditions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prepared_conditions = []
        for condition in conditions:
            cond_type = condition["type"]
            prepared_data = {"type": cond_type}

            match cond_type:
                case "Or" | "And":
                    prepared_data["conditions"] = self._prepare_conditions_data(condition["conditions"])
                case "CommentNoneMatchCondition" | "CommentAnyMatchCondition":
                    # прежние версии экспорта записывали вместо списка слов одно слово, обычно пустое:
                    # без слов условие сработало бы иначе, чем в исходной организации
                    words = condition.get("words") or ([condition["word"]] if condition.get("word") else [])
                    if not words:
                        raise MissingFieldError(condition, "words")
                    prepared_data.update(
                        {
                            "words": words,
                            "ignoreCase": condition.get("ignoreCase"),
                            "removeMarkup": condition.get("removeMarkup"),
                            "noMatchBefore": condition.get("noMatchBefore"),
                        }
                    )
                case "CommentStringNotMatchCondition" | "CommentFullyMatchCondition" | "CommentStringMatchCondition":
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
                    "AllLinkedIssueCondition"
                    | "AnyLinkedIssueCondition"
                    | "NoneLinkedIssueCondition"
                    | "AllLinkedIssueConditionProto"
                    | "AnyLinkedIssueConditionProto"
                    | "NoneLinkedIssueConditionProto"
                ):
                    # прежние версии экспорта выгружали это условие с типом из ответа API и без вида связи
                    prepared_data["type"] = cond_type.removesuffix("Proto")
                    link_filter = get_required_field(condition, "linkFilter")
                    prepared_data["linkFilter"] = {"relationship": get_required_field(link_filter, "relationship")}
                case (
                    "CommentAuthorContainsAll"
                    | "CommentAuthorContainsAny"
                    | "CommentAuthorContainsNone"
                    # так это условие без списка авторов записывали прежние версии экспорта
                    | "CommentAuthorContainerConditionProto"
                ):
                    prepared_data["authors"] = [
                        self._mapper.get_user(user_key(user)) for user in get_required_field(condition, "authors")
                    ]
                case "FieldContainsAnyUser" | "FieldContainsNoUser":
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data["field"] = field_obj.id
                    prepared_data["value"] = [
                        self._mapper.get_user(user_key(user)) for user in condition.get("value") or []
                    ]
                case (
                    "MultiProjectFieldEquals"
                    | "MultiProjectFieldBecameEqual"
                    | "MultiProjectContainerContainsAll"
                    | "MultiProjectContainerContainsAny"
                    | "MultiProjectContainerContainsNone"
                ):
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data["field"] = field_obj.id
                    # {"any": [68]}: номера проектов исходной организации переводятся в номера целевой
                    # прежние версии экспорта записывали проект ссылкой {"id": 68}
                    prepared_data["value"] = {
                        target: self._prepare_field_value(
                            field_obj, [project_id(item) for item in ids] if isinstance(ids, list) else project_id(ids)
                        )
                        for target, ids in (condition.get("value") or {}).items()
                    }
                    if condition.get("noMatchBefore") is not None:
                        prepared_data["noMatchBefore"] = condition["noMatchBefore"]
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
                    prepared_data["value"] = (
                        # в условии на размер значение — число, а не ссылка на объект поля
                        condition.get("value") if cond_type.startswith("Container.Size")
                        else self._prepare_field_value(field_obj, condition.get("value"))
                    )
                case "ContainerContainsNone" | "ContainerContainsAll" | "ContainerContainsAny":
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data.update(
                        {
                            "field": field_obj.id,
                            "value": self._prepare_field_value(field_obj, condition.get("value")),
                            "noMatchBefore": condition.get("noMatchBefore"),
                        }
                    )
                case "UserInGroups" | "UserNotInGroups":
                    field_obj = self._get_or_create_field(condition.get("field"))
                    prepared_data["field"] = field_obj.id
                    prepared_data["value"] = self._get_groups_ids(condition.get("value"))
                case "CommentAuthorInGroups" | "CommentAuthorNotInGroups":
                    # прежние версии экспорта выгружали это условие без групп: без них API триггер
                    # не примет, и пусть в журнале будет понятная причина
                    prepared_data["groups"] = self._get_groups_ids(get_required_field(condition, "groups"))
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

    def _get_groups_ids(self, groups: Optional[List[Any]]) -> List[Any]:
        """Если группы в целевой организации нет, триггер не создаётся: с выпавшей группой
        условие стало бы шире, чем было."""
        groups_ids = []
        for group in groups or []:
            group_id = self._group_resolver.get_group_id(group)
            if group_id is None:
                raise GroupNotExistsError(group.get("display") if isinstance(group, dict) else group)
            groups_ids.append(group_id)

        return groups_ids


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
            "active": self._get_active(input_data),
            "enableNotifications": input_data.get("enableNotifications"),
            "intervalMillis": input_data.get("intervalMillis"),
            "actions": self._prepare_actions_data(input_data["actions"]),
        }
        if input_data.get("description") is not None:
            prepared_data["description"] = input_data["description"]

        if input_data.get("filter") is not None:
            prepared_data["filter"] = self._prepare_filter_data(input_data["filter"])
        elif input_data.get("query") is not None:
            logger.warning("Autoaction with query might not work after import because fields in queries are not mapped. Please check it in Tracker.")
            prepared_data["query"] = input_data["query"]
        else:
            raise MissingFieldError(input_data, "filter or query")

        return prepared_data

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._create_with_writable_fields(
            prepared_data, lambda: self._queue_obj.collection.autoactions.create(**prepared_data)
        )

    def _prepare_filter_data(self, filter_data: Dict[str, Any]):
        prepared_filter_data = {}
        for field_key, value in filter_data.items():
            field_obj = self._get_or_create_field(field_key)
            prepared_filter_data[field_obj.id] = self._prepare_field_value(field_obj, value)
        return prepared_filter_data
