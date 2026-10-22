import logging
from abc import ABC, abstractmethod
from contextlib import ExitStack
from typing import Any, Dict, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import NotFound
from yandex_tracker_client.objects import Resource

from .base import CachedImporter
from .entities import ProjectImporter
from .fields import GlobalFieldImporter
from .global_objects import IssueTypeImporter, ResolutionImporter
from ..exceptions import BoardNotInDumpError
from ..groups import GroupResolver
from ..mapper import EntityMapper
from ..state import ImportState
from ..tracker_collections import update_template_permissions
from ..utils import clean_empty, get_optional, prepare_checklist_item_data, prepare_field_value


logger = logging.getLogger(__name__)

# компоненты и версии API принимает названием и ищет в очереди шаблона
QUEUE_BOUND_FIELDS = {"components", "fixVersions", "affectedVersions"}
BOARD_FIELDS = {"boards", "sprint"}


class TemplateImporter(CachedImporter, ABC):
    """Переносит шаблоны задач и комментариев.

    Названия шаблонов не уникальны, поэтому созданный шаблон находится по файлу в каталоге
    `state`, как сохранённый фильтр. Права приводятся к выгрузке на каждом прогоне: недостающие
    выдаются, лишние снимаются."""

    def __init__(self, client: TrackerClient, mapper: EntityMapper, state_path: str) -> None:
        super().__init__(client, mapper)
        self._state = ImportState(state_path, self.state_file)
        self._group_resolver = GroupResolver(client, mapper)
        self._myself = None
        self._user_uids = {}

    @property
    @abstractmethod
    def state_file(self) -> str:
        pass

    @property
    @abstractmethod
    def collection(self) -> Any:
        pass

    def get_or_create(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        try:
            template = super().get_or_create(input_data)
        except Exception:
            # шаблон прежнего прогона не прочитался: создать его заново значило бы завести дубликат
            logger.exception(f"Error loading {self.object_name} {input_data.get('name')} created by a previous run, it is skipped")
            return None
        if template is not None and input_data.get("permissions"):
            self._update_permissions(template, input_data)
        return template

    def _try_getting_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        """Шаблон заново создаётся, только если созданного прежним прогоном нет: любая другая
        ошибка чтения передаётся выше."""
        created_id = self._state.get(self._get_key(input_data))
        if created_id is None:
            return None

        try:
            return self.collection.get(created_id)
        except NotFound:
            logger.info(f"{self.object_name} {created_id} created by a previous run is not found, it is created again")
            return None

    def _get_key(self, input_data: Dict[str, Any]) -> str:
        return str(input_data["id"])

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        source_id = prepared_data.pop("source_id")
        created = self.collection.create(**{key: value for key, value in prepared_data.items() if value is not None})
        self._state.save(source_id, str(created.id))
        return created

    def _prepare_common_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if input_data.get("owner") is None:
            logger.warning(
                f"{self.object_name} {input_data['name']} has no owner in the dump, the import account becomes its owner"
            )
        return {
            "source_id": self._get_key(input_data),
            "name": input_data["name"],
            "description": input_data.get("description"),
            "queue": self._mapper.get_queue(input_data.get("queue")),
            "owner": self._mapper.get_user(input_data.get("owner")),
        }

    def _update_permissions(self, template: Resource, input_data: Dict[str, Any]) -> None:
        """Снятие и выдача — отдельными запросами: один отвергнутый субъект проваливает весь запрос,
        и шаблон остался бы с правами шире исходных."""
        name = f"{self.object_name} {input_data['name']}"
        try:
            current = self.collection.get(template.id, expand="permissions")
            update = self._get_permissions_update(input_data["permissions"], current.as_dict().get("permissions") or {}, name)
        except Exception:
            logger.exception(f"Error updating permissions of {name}")
            return

        for operation in ("revoke", "grant"):
            if not update.get(operation):
                continue
            try:
                version = self.collection.get(template.id).version
                update_template_permissions(
                    self.collection, self.collection_path, template.id, version, {operation: update[operation]}
                )
            except Exception:
                logger.exception(f"Error updating permissions of {name} with data: {{{operation!r}: {update[operation]}}}")

    def _get_permissions_update(self, permissions: Dict[str, Any], current: Dict[str, Any], name: str) -> Dict[str, Any]:
        """Недостающие права выдаются, лишние снимаются. Пользователи и группы сравниваются по
        номерам; если кого-то из них не удалось найти, этот вид прав не снимается."""
        myself = self._get_myself()
        myself_uid = str(myself.uid)
        grant, revoke = {}, {}
        for permission in set(permissions) | set(current):
            subjects = permissions.get(permission) or {}
            current_subjects = current.get(permission) or {}
            resolvers = {
                "users": lambda user: self._get_user_uid(user, name),
                "groups": lambda group: self._get_group_id(group, name),
                "roles": lambda role: role,
            }
            wanted, complete = {}, {}
            for kind, resolve in resolvers.items():
                resolved = [resolve(item) for item in subjects.get(kind) or []]
                wanted[kind] = set(resolved) - {None}
                complete[kind] = None not in resolved
            existing = {kind: {str(item["id"]) for item in current_subjects.get(kind) or []} for kind in resolvers}
            grant[permission] = {kind: sorted(wanted[kind] - existing[kind]) for kind in resolvers}
            revoke[permission] = {
                kind: sorted(existing[kind] - wanted[kind]) for kind in resolvers if complete[kind]
            }
            if myself_uid in revoke[permission].get("users", []) and not get_optional(myself, "admin"):
                # без своих прав учётная запись не администратора не прочитает шаблон на следующем прогоне
                revoke[permission]["users"].remove(myself_uid)
                logger.warning(f"{permission} permission of the import account is kept in {name}: it is not an administrator")
        return clean_empty({"grant": grant, "revoke": revoke})

    def _get_user_uid(self, user: str, name: str) -> Optional[str]:
        target = self._mapper.get_user(user)
        if target not in self._user_uids:
            try:
                self._user_uids[target] = str(self._client.users.get(target).uid)
            except Exception:
                logger.warning(f"User {user} is not found, it is skipped in permissions of {name}: check mapping/users.cfg")
                self._user_uids[target] = None
        return self._user_uids[target]

    def _get_group_id(self, group: str, name: str) -> Optional[str]:
        group_id = self._group_resolver.get_group_id(group)
        if group_id is None:
            # доступ без пропущенной группы только сужается, но молча такое не проходит
            logger.warning(f"Group {group} is not found, it is skipped in permissions of {name}: check mapping/groups.cfg")
            return None
        return str(group_id)

    def _get_myself(self) -> Resource:
        if self._myself is None:
            self._myself = self._client.myself
        return self._myself

    @property
    @abstractmethod
    def collection_path(self) -> str:
        pass


class IssueTemplateImporter(TemplateImporter):
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        state_path: str,
        global_field_importer: GlobalFieldImporter,
        project_importer: ProjectImporter,
        issue_type_importer: IssueTypeImporter,
        resolution_importer: ResolutionImporter,
        board_importer: Any = None,
    ) -> None:
        super().__init__(client, mapper, state_path)
        self._global_field_importer = global_field_importer
        self._project_importer = project_importer
        self._board_importer = board_importer
        # тип задачи и резолюция шаблона могут не встречаться в очередях выгрузки
        self._reference_importers = {"type": issue_type_importer, "resolution": resolution_importer}
        self._fields = []

    @property
    def object_name(self) -> str:
        return "Issue template"

    @property
    def state_file(self) -> str:
        return "issue_templates.cfg"

    @property
    def collection(self) -> Any:
        return self._client.issue_templates

    @property
    def collection_path(self) -> str:
        return "issueTemplates"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        self._fields = []
        field_templates = {}
        for field_key, value in (input_data.get("fieldTemplates") or {}).items():
            if field_key.startswith("local_"):
                logger.warning(
                    f"Local field {field_key} of {self.object_name} {input_data['name']} is not imported: "
                    "the API does not accept local fields in issue templates"
                )
                continue
            field_obj = self._global_field_importer.get_or_create(field_key)
            if field_obj.key in self._reference_importers and isinstance(value, str):
                self._reference_importers[field_obj.key].get_or_create(value)
            if field_obj.key in BOARD_FIELDS:
                # шаблон без доски или спринта не из выгрузки менее заполнен, но работает так же
                value = self._keep_transferred_boards(field_obj, value, input_data["name"])
                if not value:
                    continue
            self._fields.append(field_obj)
            field_templates[field_obj.id] = self._prepare_field_value(field_obj, value)

        if not field_templates:
            # шаблон без полей API не создаёт
            raise ValueError(f"{self.object_name} {input_data['name']} has no fields that can be imported")
        return {
            **self._prepare_common_data(input_data),
            "fieldTemplates": field_templates,
            "checklistItems": [
                prepare_checklist_item_data(item, self._mapper) for item in input_data.get("checklistItems") or []
            ],
        }

    def _try_creating_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        if not any(not key.startswith("local_") for key in input_data.get("fieldTemplates") or {}):
            # шаблон без полей API не создаёт, а локальные поля в шаблоне не принимает
            logger.error(
                f"{self.object_name} {input_data['name']} has no fields that can be imported, it is skipped: "
                "the API does not accept local fields in issue templates"
            )
            return None
        return super()._try_creating_object(input_data)

    def _keep_transferred_boards(self, field_obj: Resource, value: Any, name: str) -> Any:
        """Доски и спринты, которых нет в выгрузке, отбрасываются по одному, остальные остаются."""
        items = value if isinstance(value, list) else [value]
        kept = []
        for item in items:
            try:
                prepare_field_value(field_obj, [item], self._mapper, board_importer=self._board_importer)
            except BoardNotInDumpError:
                logger.warning(f"Board or sprint {item} in field {field_obj.key} of {self.object_name} {name} is skipped: it is not in the dump")
            else:
                kept.append(item)
        return kept if isinstance(value, list) else (kept[0] if kept else None)

    def _prepare_field_value(self, field_obj: Resource, value: Any) -> Any:
        if field_obj.key in QUEUE_BOUND_FIELDS:
            return value
        if field_obj.key == "project" and isinstance(value, dict):
            # {"primary": 65, "secondary": [57]}: номера проектов переводятся в номера целевой организации
            return {target: self._prepare_field_value(field_obj, ids) for target, ids in value.items()}
        return prepare_field_value(
            field_obj, value, self._mapper, project_importer=self._project_importer, board_importer=self._board_importer
        )

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        """Поле с запретом редактирования API в шаблон не принимает: запрет снимается на время
        создания."""
        with ExitStack() as stack:
            for field_obj in self._fields:
                stack.enter_context(
                    self._global_field_importer.writable(field_obj, f"creating {self.object_name} {prepared_data['name']}")
                )
            return super()._create_object(prepared_data)


class CommentTemplateImporter(TemplateImporter):
    @property
    def object_name(self) -> str:
        return "Comment template"

    @property
    def state_file(self) -> str:
        return "comment_templates.cfg"

    @property
    def collection(self) -> Any:
        return self._client.comment_templates

    @property
    def collection_path(self) -> str:
        return "commentTemplates"

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **self._prepare_common_data(input_data),
            "template": input_data.get("template"),
            "summonees": [self._mapper.get_user(user) for user in input_data.get("summonees") or []],
            "maillistSummonees": input_data.get("maillistSummonees") or [],
        }
