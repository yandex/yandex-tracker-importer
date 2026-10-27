import logging
import re
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import BadRequest, Conflict, Forbidden, NotFound, UnprocessableEntity
from yandex_tracker_client.objects import Resource

from .base import CachedImporter, GetOrCreateImporter
from ..constants import ALL_PARTICIPANTS_GROUP_ID
from ..exceptions import (
    BaseTrackerError,
    BoardNotImportedError,
    BoardNotInDumpError,
    ComponentNotExistsError,
    GroupNotExistsError,
    VersionNotExistsError
)
from ..groups import GroupResolver
from ..mapper import EntityMapper
from ..tracker_collections import get_user_queue_permissions, run_version_action, update_workflow
from ..utils import clean_empty, get_optional, prepare_field_value


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

    # ключи условий и функций перехода по виду ссылки, которую надо сопоставить
    USER_KEYS = {"users", "user", "author"}
    # функция ссылается на доску: переносится вместе с ней
    BOARD_FUNCTIONS = {"SetActiveSprintFunction"}
    FIELD_KEYS = {"field", "fields", "target", "source", "from", "to"}
    COMPONENT_KEYS = {"component", "components"}

    def __init__(self, client: TrackerClient, mapper: EntityMapper, board_importer: Any = None) -> None:
        super().__init__(client, mapper)
        self._board_importer = board_importer
        self._group_resolver = None
        self._uses_local_fields = False
        self._context = (None, None, None, None, None)
        self._local_field_importer = None
        self._global_field_importer = None
        self._component_importer = None
        self._version_importer = None
        self._project_importer = None
        # воркфлоу, у которых в целевой организации нет условий, экранов и функций переходов,
        # а в выгрузке они есть: их импорт дополняет после всех очередей
        self._incomplete = []
        self._default_workflow = self.get_or_create(self.DEFAULT_WORKFLOW_DATA)

    def set_queue_context(
        self,
        local_field_importer: Any,
        global_field_importer: Any,
        component_importer: Any,
        version_importer: Any = None,
        project_importer: Any = None,
    ) -> None:
        """Поля, компоненты, версии и проекты в условиях, экранах и функциях переходов
        сопоставляются в очереди, воркфлоу которой импортируется."""
        self._context = (
            local_field_importer, global_field_importer, component_importer, version_importer, project_importer
        )
        (
            self._local_field_importer,
            self._global_field_importer,
            self._component_importer,
            self._version_importer,
            self._project_importer,
        ) = self._context

    def get_or_create(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        workflow = super().get_or_create(input_data)
        current = workflow.as_dict() if workflow is not None and hasattr(workflow, "as_dict") else {}
        if (
            workflow is not None
            and self._has_transition_settings(input_data)
            and not self._has_transition_settings(current if isinstance(current, dict) else {})
        ):
            self._incomplete.append((input_data, self._context))
        return workflow

    def complete_workflows(self) -> None:
        """Дополняет условиями, экранами и функциями переходов воркфлоу, у которых их нет: созданные
        только с переходами в этом прогоне (например, условие ссылается на очередь, которая
        импортируется позже) и прежними прогонами. Воркфлоу, где они уже есть, не меняется."""
        incomplete, self._incomplete = self._incomplete, []
        for input_data, context in incomplete:
            self.set_queue_context(*context)
            self._uses_local_fields = False
            try:
                prepared = self._prepare_data(input_data)
                current = self._client.workflows.get(prepared["id"])
                current_data = current.as_dict()
                if self._has_transition_settings(current_data):
                    # условия, экраны или функции задали в целевой организации после начала импорта
                    logger.info(f"Workflow {prepared['id']} already has conditions, screens or functions")
                    continue
                if self._transitions(current_data) != self._transitions(prepared):
                    # переходы меняли в целевой организации: изменение заменило бы их переходами выгрузки
                    logger.warning(
                        f"Workflow {prepared['id']} differs from the dump in transitions, its conditions, screens "
                        "and functions are not added: set them manually"
                    )
                    continue
                self._with_local_field_retries(
                    lambda: update_workflow(
                        self._client,
                        prepared["id"],
                        current.version,
                        {"initialAction": prepared["initialAction"], "steps": prepared["steps"]},
                    )
                )
                logger.info(f"Workflow {prepared['id']} is completed with conditions, screens and functions")
            except Exception:
                logger.exception(
                    f"Error adding conditions, screens and functions to workflow {input_data.get('key')}: "
                    "fix the mapping and run the import again, or set them manually"
                )

    @staticmethod
    def _transitions(workflow: Dict[str, Any]) -> Counter:
        """Шаги и переходы воркфлоу — в выгрузке или в ответе API. Идентификаторы переходов не
        сравниваются: прежние версии импорта создавали переходы без них, и API назначал свои."""
        def key(value: Any) -> Any:
            return value.get("key") if isinstance(value, dict) else value

        initial = workflow.get("initialAction") or {}
        transitions = Counter([("initialAction", None, key(initial.get("target")))])
        for step in workflow.get("steps") or []:
            status = key(step.get("status"))
            transitions[("step", status, None)] += 1
            for kind, actions in (("actions", step.get("actions") or []), ("metaAction", [step.get("metaAction")])):
                transitions.update((kind, status, key(action.get("target"))) for action in actions if action)
        return transitions

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

        prepared = {
            "name": {
                "en": action_data["name"]["en"],
                "ru": action_data["name"]["ru"],
            },
            "target": self._mapper.get_status(action_data["target"])
        }
        if action_data.get("key"):
            # на идентификатор перехода ссылается переход по комментарию (actionId)
            prepared["id"] = action_data["key"]
        if action_data.get("conditions"):
            prepared["conditions"] = self._prepare_workflow_items(action_data["conditions"])
        if action_data.get("postFunctions"):
            prepared["postFunctions"] = self._prepare_workflow_items(action_data["postFunctions"])
        if action_data.get("screen"):
            prepared["screen"] = self._prepare_screen(action_data["screen"])
        return prepared

    def _prepare_steps_data(self, steps_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        steps: Dict[str, Dict[str, Any]] = {}
        for step in steps_data:
            prepared = {
                "status": self._mapper.get_status(step["status"]),
                "actions": [self._prepare_action_data(action) for action in step.get("actions", [])],
                "metaAction": self._prepare_action_data(step.get("metaAction", {})),
                **(
                    {"afterCommentCreated": self._prepare_workflow_items(step["afterCommentCreated"])}
                    if step.get("afterCommentCreated") else {}
                ),
            }
            first = steps.setdefault(prepared["status"], prepared)
            if first is not prepared:
                self._merge_step(first, prepared)
        return list(steps.values())

    @staticmethod
    def _merge_step(first: Dict[str, Any], other: Dict[str, Any]) -> None:
        """Шаг статуса, с которым сопоставлены несколько статусов выгрузки: воркфлоу принимает один шаг
        на статус. Переходы объединяются; переход в статус и функции после комментария берутся у
        первого шага, у которого они есть."""
        status = first["status"]
        first["actions"] += [action for action in other["actions"] if action not in first["actions"]]
        dropped = []
        for setting in ("metaAction", "afterCommentCreated"):
            if not other.get(setting):
                continue
            if not first.get(setting):
                first[setting] = other[setting]
            elif first[setting] != other[setting]:
                dropped.append(setting)
        logger.warning(
            f"Workflow steps of status {status} are merged into one"
            + (f", {', '.join(dropped)} of the next steps are dropped" if dropped else "")
        )

    def _prepare_screen(self, screen_data: Dict[str, Any]) -> Dict[str, Any]:
        prepared = {
            key: screen_data[key]
            for key in ("title", "comment", "commentRequired", "allowLinkCreation")
            if screen_data.get(key) is not None
        }
        if screen_data.get("commentSummonees"):
            # призываемые задаются вместе с текстом комментария
            prepared["comment"] = {
                "text": screen_data.get("comment"),
                "summonees": [self._mapper.get_user(user) for user in screen_data["commentSummonees"]],
            }
        prepared["elements"] = []
        for element in screen_data.get("elements") or []:
            field_obj = self._get_field(element["field"])
            prepared_element = {**element, "field": field_obj.id}
            schema = getattr(field_obj, "schema", None) or {}
            if element.get("defaultValue") is not None and "group" in (schema.get("type"), schema.get("items")):
                # группы выгрузка пишет названием, API принимает идентификатор
                default = element["defaultValue"]
                prepared_element["defaultValue"] = (
                    [self._get_group_id(group) for group in default] if isinstance(default, list)
                    else self._get_group_id(default)
                )
            elif element.get("defaultValue") is not None:
                try:
                    prepared_element["defaultValue"] = prepare_field_value(
                        field_obj,
                        element["defaultValue"],
                        self._mapper,
                        project_importer=self._project_importer,
                        component_importer=self._component_importer,
                        version_importer=self._version_importer,
                        board_importer=self._board_importer,
                    )
                except BoardNotInDumpError:
                    # как функция перехода: доски нет в выгрузке — значение по умолчанию не переносится
                    logger.warning(f"Default value of field {element['field']} on a screen is skipped: its board is not in the dump")
                    prepared_element.pop("defaultValue", None)
            prepared["elements"].append(prepared_element)
        return prepared

    def _prepare_workflow_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Условия и функции перехода: пользователи, группы, поля, очереди, резолюции и компоненты
        сопоставляются с целевой организацией. Группа, которой там нет, — ошибка: без неё условие
        стало бы шире, чем было."""
        prepared = []
        for item in items:
            if item.get("type") in self.BOARD_FUNCTIONS:
                # пока доски не созданы, BoardNotImportedError: воркфлоу дополняется после них
                board_id = self._board_importer.get_board_id(item.get("board")) if self._board_importer else None
                if board_id is None:
                    logger.warning(f"Workflow function {item} is skipped: its board is not in the dump")
                    continue
                prepared.append({**item, "board": board_id})
                continue
            prepared.append({key: self._prepare_workflow_item_value(key, value) for key, value in item.items()})
        return prepared

    def _prepare_workflow_item_value(self, key: str, value: Any) -> Any:
        if key == "conditions":
            return self._prepare_workflow_items(value or [])
        if isinstance(value, list):
            return [self._prepare_workflow_item_value(key, item) for item in value]
        if value is None or isinstance(value, bool):
            return value
        if key in self.USER_KEYS:
            return self._mapper.get_user(value)
        if key in ("group", "department"):
            return self._get_group_id(value)
        if key in self.FIELD_KEYS:
            return self._get_field_id(value)
        if key in self.COMPONENT_KEYS:
            return self._component_importer.get_component_by_name(value).id
        if key == "queues":
            return self._mapper.get_queue(value)
        if key == "resolution":
            return self._mapper.get_resolution(value)
        return value

    def _get_field_id(self, field_key: str) -> Any:
        return self._get_field(field_key).id

    def _get_field(self, field_key: str) -> Resource:
        if field_key.startswith("local_"):
            self._uses_local_fields = True
            return self._local_field_importer.get_local_field(field_key)
        return self._global_field_importer.get_or_create(field_key)

    def _get_group_id(self, group: Any) -> Any:
        if self._group_resolver is None:
            self._group_resolver = GroupResolver(self._client, self._mapper)
            self._group_resolver.load_groups()
        group_id = self._group_resolver.get_group_id(group)
        if group_id is None:
            raise GroupNotExistsError(group)
        return group_id

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        mapped_queue_key = self._mapper.get_queue(input_data.get("queue"))
        workflow_id = _get_workflow_id(mapped_queue_key, input_data["key"])
        return self._client.workflows.get(workflow_id)

    def _try_creating_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        """Если воркфлоу не создаётся с условиями, экранами и функциями из-за данных — например,
        группы из условия нет в целевой организации или API отклоняет условие, — он создаётся
        только с переходами, как прежними версиями импорта: без воркфлоу не настроить очередь, и
        импорт бы остановился. Условия, экраны и функции импорт пробует добавить после всех
        очередей (`complete_workflows`). Сбой связи ведёт себя как раньше: воркфлоу не создаётся."""
        self._uses_local_fields = False
        try:
            prepared_data = self._prepare_data(input_data)
        except BoardNotImportedError:
            # функция перехода ссылается на доску, которую импорт создаёт после всех очередей
            logger.info(f"Workflow {input_data.get('key')} is completed with its transition settings after boards are imported")
            return super()._try_creating_object(self._strip_transition_settings(input_data))
        except (BaseTrackerError, NotFound, BadRequest, UnprocessableEntity):
            # данных не сопоставить: например, группы нет в целевой организации
            logger.exception(f"Error preparing data for {self.object_name} with data: {input_data}")
        except Exception:
            logger.exception(f"Error preparing data for {self.object_name} with data: {input_data}")
            return None
        else:
            try:
                return self._create_object(prepared_data)
            except (BadRequest, UnprocessableEntity):
                logger.exception(f"Error creating {self.object_name} with data: {prepared_data}")
            except Exception:
                logger.exception(f"Error creating {self.object_name} with data: {prepared_data}")
                return None

        if not self._has_transition_settings(input_data):
            return None

        self._uses_local_fields = False
        workflow = super()._try_creating_object(self._strip_transition_settings(input_data))
        if workflow is not None:
            logger.error(
                f"Workflow {input_data.get('key')} of queue {self._mapper.get_queue(input_data.get('queue'))} "
                "is created without conditions, screens and functions of transitions, see the error above"
            )
        return workflow

    def _has_transition_settings(self, input_data: Dict[str, Any]) -> bool:
        steps = input_data.get("steps") or []
        actions = [input_data.get("initialAction") or {}]
        for step in steps:
            actions += (step.get("actions") or []) + ([step["metaAction"]] if step.get("metaAction") else [])

        def transferred(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            # функция с доской, которой нет в выгрузке, не переносится
            # у воркфлоу целевой организации доска — ссылка: функция уже перенесена
            return [
                function for function in functions or []
                if function.get("type") not in self.BOARD_FUNCTIONS
                or isinstance(function.get("board"), dict)
                or (self._board_importer is not None and self._board_importer.in_dump(function.get("board")))
            ]

        return any(step.get("afterCommentCreated") for step in steps) or any(
            action.get("conditions")
            or any((action.get("screen") or {}).values())
            or transferred(action.get("postFunctions"))
            for action in actions
        )

    @staticmethod
    def _strip_transition_settings(input_data: Dict[str, Any]) -> Dict[str, Any]:
        def strip_action(action: Dict[str, Any]) -> Dict[str, Any]:
            return {key: value for key, value in action.items() if key not in ("conditions", "postFunctions", "screen")}

        return {
            **input_data,
            "initialAction": strip_action(input_data.get("initialAction") or {}),
            "steps": [
                {
                    **{key: value for key, value in step.items() if key != "afterCommentCreated"},
                    "actions": [strip_action(action) for action in step.get("actions") or []],
                    **({"metaAction": strip_action(step["metaAction"])} if step.get("metaAction") else {}),
                }
                for step in input_data.get("steps") or []
            ],
        }

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._with_local_field_retries(lambda: self._client.workflows.create(**prepared_data))

    def _with_local_field_retries(self, request: Callable[[], Any]) -> Any:
        """Локальное поле, созданное только что, API принимает в воркфлоу не сразу: первые запросы
        получают 422 «Локальные поля … не принадлежат очереди». Другие ошибки не повторяются."""
        delay = 2
        for attempt in range(1, 6):
            try:
                return request()
            except UnprocessableEntity as error:
                if not self._uses_local_fields or not self._is_local_field_error(error) or attempt == 5:
                    raise
                time.sleep(delay)
                delay *= 2

    @staticmethod
    def _is_local_field_error(error: UnprocessableEntity) -> bool:
        text = f"{getattr(error, 'error_messages', '')} {getattr(error, 'errors', '')}".lower()
        return "локальн" in text or "local field" in text


# Снятие запрета доступа начинает действовать с задержкой до 15–20 с, а API прав сразу показывает
# его снятым. Поэтому после снятия — пауза, а отказ в доступе повторяется
DENY_LIFT_DELAY = 30
FORBIDDEN_RETRY_DELAYS = (5, 10, 15, 30)


class QueueImporter(GetOrCreateImporter):
    def __init__(self, client: TrackerClient, mapper: EntityMapper) -> None:
        super().__init__(client, mapper)
        self._group_resolver = GroupResolver(client, mapper)
        # до создания очередей: сбой позже оставил бы созданную очередь без прав
        self._group_resolver.load_groups()
        # запрет доступа по ключу очереди, ставится в конце импорта
        self._pending_deny = {}
        # часть запрета, снятая на время импорта
        self._lifted_deny = {}
        # очереди, где отказ в доступе не прошёл за все повторы
        self._forbidden_persists = set()
        self._myself = None

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
        configs: Dict[str, Dict[str, Any]] = {}
        for issue_type_config in issue_types_config:
            issue_type = self._mapper.get_issue_type(issue_type_config["issueType"])
            workflow = _get_workflow_id(self._mapper.get_queue(queue_key), issue_type_config["workflow"])
            resolutions = [self._mapper.get_resolution(r) for r in issue_type_config.get("resolutions", [])]
            if issue_type in configs:
                # типы выгрузки сопоставлены с одним типом: у очереди одна настройка на тип, резолюции объединяются
                first = configs[issue_type]
                first["resolutions"] += [r for r in resolutions if r not in first["resolutions"]]
                if first["workflow"] == workflow:
                    continue
                logger.error(
                    f"Issue type {issue_type_config['issueType']} of queue {queue_key} is mapped to {issue_type} "
                    f"configured by an earlier entry, its workflow {issue_type_config.get('workflow')} is not used: "
                    "its issues in statuses missing from the kept workflow are not imported"
                )
                continue
            configs[issue_type] = {"issueType": issue_type, "workflow": workflow, "resolutions": resolutions}
        return list(configs.values())

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

    def _get_groups_ids(self, groups: List[Any], queue_key: str, permission_type: str) -> set:
        """Номера групп целевой организации. Ненайденная группа пропускается с предупреждением."""
        groups_ids = set()
        for group in groups:
            group_id = self._group_resolver.get_group_id(group)
            if group_id is None:
                logger.warning(
                    f"Group {group} is not found, it is skipped in permissions of queue {queue_key} ({permission_type}): "
                    "check mapping/groups.cfg"
                )
            else:
                groups_ids.add(group_id)

        return groups_ids

    def update_permissions(self, queue_obj: Resource, queue_data: Dict[str, Any]) -> None:
        """
        Добавить разрешения к очереди, сохранив существующие.
        Удалить группу "Все сотрудники", если её нет в импортируемых данных, чтобы избежать общего доступа при первичном импорте очереди.
        Запрет доступа откладывается до конца импорта — см. `update_deny_permissions`.
        """
        incoming_permissions = queue_data.get("permissions")
        if not incoming_permissions:
            return

        permissions_changes = {}
        for permission_type in ["create", "read", "write", "writeNoAssign", "grant"]:
            if permission_type == "writeNoAssign" and permission_type not in incoming_permissions:
                continue
            permissions_changes[permission_type] = self._prepare_permission_changes(
                incoming_permissions.get(permission_type) or {}, queue_obj.key, permission_type
            )
            groups_add = permissions_changes[permission_type]["groups"]["add"]
            if ALL_PARTICIPANTS_GROUP_ID not in groups_add:
                permissions_changes[permission_type]["groups"]["remove"] = [ALL_PARTICIPANTS_GROUP_ID]

        deny = clean_empty(self._prepare_permission_changes(incoming_permissions.get("deny") or {}, queue_obj.key, "deny"))
        if deny:
            self._pending_deny[queue_obj.key] = deny

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

    def lift_deny(self, queue_key: str) -> None:
        """Снимает с очереди субъекты запрета из выгрузки, через которые запрет действует на
        учётную запись импорта: под запретом она не видит версий и задач очереди. Возвращает их
        `update_deny_permissions`. Если запрет действует и через субъекты не из выгрузки, ничего
        не снимается, в журнал пишется ошибка. После снятия — пауза `DENY_LIFT_DELAY`."""
        # без запрета в выгрузке права тоже проверяются, чтобы сообщить о ручном запрете
        deny = self._pending_deny.get(queue_key) or {}
        try:
            if self._myself is None:
                self._myself = self._client.myself
            myself = self._myself
            applied = (get_user_queue_permissions(self._client, queue_key, myself.uid).get("permissions") or {}).get("DENY")
            if not applied:
                return
            lifted = {}
            not_in_dump = []
            for kind in ("users", "groups", "roles"):
                applied_ids = self._subject_ids(applied.get(kind))
                for item in deny.get(kind, {}).get("add", []):
                    keys = self._user_keys(item) if kind == "users" else {str(item)}
                    subject_id = next((applied_ids[key] for key in keys if key in applied_ids), None)
                    if subject_id is not None:
                        lifted.setdefault(kind, []).append(subject_id)
                not_in_dump += [
                    f"{kind} {subject_id}" for subject_id in set(applied_ids.values()) - set(lifted.get(kind, []))
                ]
            if not_in_dump:
                logger.error(
                    f"Access to queue {queue_key} is denied to the import account by a deny permission that is not "
                    f"in the dump ({', '.join(sorted(map(str, not_in_dump)))}): remove the account from it to import "
                    "issues and versions of the queue"
                )
                return
            if not lifted:
                return
            # запоминается до запроса, чтобы вернуть запрет и при потерянном ответе
            self._lifted_deny[queue_key] = lifted
            self._client.queues[queue_key].update_permissions(
                {"deny": {kind: {"remove": ids} for kind, ids in lifted.items()}}
            )
            logger.warning(
                f"Deny permission of queue {queue_key} is lifted for the import account while importing, it is set "
                "again at the end: if the import is interrupted, run it again"
            )
        except Forbidden:
            # права очереди доступны только с правом на её настройки
            logger.warning(f"Permissions of queue {queue_key} are not available to the import account, its deny is not checked")
        except Exception:
            logger.exception(f"Error lifting deny permission of queue {queue_key}")
        if queue_key in self._lifted_deny:
            time.sleep(DENY_LIFT_DELAY)

    def retry_forbidden(self, queue_key: str, request: Callable[[], Any], on_retry: Callable[[], Any] = None) -> Any:
        """Выполняет запрос к очереди; если её запрет снят, отказ в доступе повторяется с паузами
        `FORBIDDEN_RETRY_DELAYS`. Перед повтором вызывается `on_retry`; если он вернул объект,
        возвращается этот объект. После окончательного отказа повторы в очереди отключаются."""
        retry = queue_key in self._lifted_deny and queue_key not in self._forbidden_persists
        for delay in FORBIDDEN_RETRY_DELAYS if retry else ():
            try:
                return request()
            except Forbidden:
                logger.warning(f"Access to queue {queue_key} is denied, retrying in {delay} s: the lifted deny permission may still apply")
                time.sleep(delay)
                if on_retry is not None:
                    try:
                        found = on_retry()
                    except Forbidden:
                        continue
                    if found is not None:
                        return found
        try:
            return request()
        except Forbidden:
            if retry:
                self._forbidden_persists.add(queue_key)
            raise

    @staticmethod
    def _subject_ids(subjects: Any) -> Dict[str, Any]:
        """Номер субъекта по каждому из его идентификаторов (`id`, `uid`, `passportUid`, …)."""
        return {
            str(get_optional(subject, key)): get_optional(subject, "id")
            for subject in subjects or []
            for key in ("id", "uid", "passportUid", "cloudUid", "trackerUid")
            if get_optional(subject, key) is not None
        }

    def _user_keys(self, user: Any) -> set:
        """Идентификаторы пользователя из users.cfg; логин переводится в номера запросом."""
        if str(user).isdigit():
            return {str(user)}
        user_obj = self._client.users.get(user)
        return {
            str(get_optional(user_obj, key))
            for key in ("id", "uid", "passportUid", "cloudUid", "trackerUid", "login")
            if get_optional(user_obj, key) is not None
        }

    def update_deny_permissions(self) -> None:
        """Ставит запрет из выгрузки отдельным запросом, чтобы его ошибка не мешала остальным
        правам. Снятая часть запрета возвращается ещё одним запросом, до полного."""
        for queue_key, deny in self._pending_deny.items():
            lifted = self._lifted_deny.get(queue_key)
            if lifted:
                try:
                    self._client.queues[queue_key].update_permissions(
                        {"deny": {kind: {"add": ids} for kind, ids in lifted.items()}}
                    )
                except Exception:
                    logger.exception(
                        f"Error restoring lifted deny permission of queue {queue_key}: {lifted}, set it manually"
                    )
            try:
                self._client.queues[queue_key].update_permissions({"deny": deny})
                logger.info(f"Successfully updated deny permission for queue {queue_key}")
            except Exception:
                logger.exception(
                    f"Error updating deny permission for queue {queue_key} with data: {deny}, set it manually"
                )
        self._pending_deny = {}
        self._lifted_deny = {}

    def _prepare_permission_changes(self, permission: Dict[str, Any], queue_key: str, permission_type: str) -> Dict[str, Any]:
        return {
            "users": {"add": list({self._mapper.get_user(user) for user in permission.get("users", [])})},
            "roles": {"add": permission.get("roles", [])},
            "groups": {"add": list(self._get_groups_ids(permission.get("groups", []), queue_key, permission_type))},
        }


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
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        queue_obj: Resource,
        retry_forbidden: Callable[[Callable[[], Any], Callable[[], Any]], Any] = lambda request, on_retry: request(),
    ) -> None:
        super().__init__(client, mapper)
        self._queue_obj = queue_obj
        # повтор отказа в доступе у очереди со снятым запретом
        self._retry_forbidden = retry_forbidden
        self._load_versions()

    def _load_versions(self) -> None:
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
        """Дата выпуска при создании сразу делает версию выпущенной с этой датой."""
        release_date = input_data.get("releasedAt") if input_data.get("released") else None
        if release_date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(release_date)):
            # дата не в виде YYYY-MM-DD: версия выпускается в конце импорта датой прогона
            logger.warning(f"Release date {release_date} of version {input_data['name']} is not YYYY-MM-DD, it is skipped")
            release_date = None
        return clean_empty({
            "queue": self._queue_obj.key,
            "name": input_data["name"],
            "description": input_data.get("description"),
            "startDate": input_data.get("startDate"),
            "dueDate": input_data.get("dueDate"),
            "releaseDate": f"{release_date}T00:00:00.000+0000" if release_date else None,
        })

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        def find_created() -> Optional[Resource]:
            # под запретом список версий пуст
            self._load_versions()
            return self.get_from_cache(f"{self._queue_obj.key}-{prepared_data['name']}")

        try:
            return self._retry_forbidden(lambda: self._client.versions.create(**prepared_data), find_created)
        except Conflict:
            # версия есть, но список читался под запретом
            found = find_created()
            if found is None:
                raise
            return found

    def update_settings(self, input_data: Dict[str, Any]) -> None:
        """Следующая версия, выпуск и архив задаются, когда созданы все версии очереди: следующая
        версия может идти в выгрузке позже. Следующая версия указывается номером: одноимённые
        версии бывают в разных очередях. Версия, созданная без даты выпуска, выпускается датой
        прогона.

        Архивирование убирает версию у задач без резолюции. Поэтому версия, у которой в целевой
        организации уже есть такие задачи (их создал прежний прогон), не архивируется."""
        version_obj = self.get_from_cache(f"{self._queue_obj.key}-{input_data.get('name')}")
        if version_obj is None:
            logger.error(f"Version {input_data.get('name')} of queue {self._queue_obj.key} does not exist")
            return
        steps = []
        next_obj = self.get_from_cache(f"{self._queue_obj.key}-{input_data['next']}") if input_data.get("next") else None
        if input_data.get("next") and next_obj is None:
            logger.error(f"Next version {input_data['next']} of version {input_data['name']} does not exist")
        elif next_obj is not None:
            current_next = get_optional(version_obj.next, "id") if version_obj.next else None
            if str(current_next) != str(next_obj.id):
                steps.append(("next", lambda: version_obj.update(next=next_obj.id)))
        elif "next" in input_data and version_obj.next:
            # следующей версии в источнике нет: экспорт пишет пустую строку, в выгрузке прежней
            # версии ключа нет. Значение null API принимает, но связь не снимает
            steps.append(("next", lambda: version_obj.update(next={"unset": True})))
        if input_data.get("released") and not version_obj.released:
            steps.append(("release", lambda: run_version_action(self._client, version_obj.id, "_release")))
        if input_data.get("archived") and not version_obj.archived:
            unresolved = self._client.issues.find(
                filter={"queue": self._queue_obj.key, "fixVersions": version_obj.id, "resolution": "empty()"},
                count_only=True,
            )
            if unresolved:
                logger.warning(
                    f"Version {input_data['name']} of queue {self._queue_obj.key} is not archived: archiving "
                    f"removes it from {unresolved} unresolved issues, archive it manually"
                )
            else:
                steps.append(("archive", lambda: run_version_action(self._client, version_obj.id, "_archive")))

        for step, run in steps:
            try:
                self._retry_forbidden(run, None)
            except Exception:
                logger.exception(
                    f"Error setting {step} of version {input_data['name']} of queue {self._queue_obj.key}"
                )

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
