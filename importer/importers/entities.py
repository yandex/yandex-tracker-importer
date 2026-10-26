import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .base import CachedImporter
from ..constants import ALL_PARTICIPANTS_GROUP_ID, DEFAULT_TRACKER_UI_URL
from ..exceptions import GoalNotExistsError, ProjectNotExistsError
from ..groups import GroupResolver
from ..link_collectors import EntityLinksCollector
from ..mapper import EntityMapper
from ..text_normalizer import TextNormalizer
from ..tracker_collections import create_checklist_item
from ..utils import clean_empty, prepare_checklist_item_data


logger = logging.getLogger(__name__)


class EntityImporter(CachedImporter, ABC):
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        entity_links_collector: EntityLinksCollector,
        ui_url: str = DEFAULT_TRACKER_UI_URL,
    ):
        super().__init__(client, mapper)
        self._entity_links_collector = entity_links_collector
        self._ui_url = ui_url
        self._parent_entities = {}
        self._permission_sources = {}
        self._metric_items = {}
        self._entities_by_name = {}
        self._group_resolver = GroupResolver(client, mapper)
        # до создания сущностей: сбой позже оставил бы созданную сущность без прав
        self._group_resolver.load_groups()

    @property
    @abstractmethod
    def filename(self) -> str:
        pass

    @property
    @abstractmethod
    def objects(self):
        pass

    @property
    def parent_entities(self) -> Dict[str, str]:
        return self._parent_entities

    @property
    def permission_sources(self) -> Dict[str, List[Dict[str, Any]]]:
        """Сущности, от которых наследуется доступ: `[{"type": "goal", "id": "12"}]`. Собираются и
        для созданных прежним прогоном сущностей — наследование проставляется после создания всех
        сущностей и приводится к выгрузке при каждом прогоне."""
        return self._permission_sources

    @property
    def metric_items(self) -> Dict[str, Any]:
        """Метрики ссылаются на виджеты дашбордов, а дашборды импортируются после сущностей,
        поэтому метрики проставляются отдельным проходом."""
        return self._metric_items

    def get_or_create(self, input_data: Dict[str, Any], attachments_path: str) -> Optional[Resource]:
        permissions = input_data.get("permissions") or {}
        sources = permissions.get("permissionSources")
        if isinstance(sources, list) and self._get_key(input_data) is not None:
            self._permission_sources[self._get_key(input_data)] = sources
        elif "permissionSources" in permissions:
            # не список — источники не разобрать, и снимать текущие нельзя
            logger.error(
                f"permissionSources of {self.object_name} {input_data.get('id')} is not a list, inheritance is not changed"
            )
        # без ключа (выгрузка прежней версии) наследование не трогается

        entity_obj = self._try_getting_object(input_data)
        if entity_obj is not None:
            self._cache_entity(input_data, entity_obj)
            return entity_obj

        entity_obj = self._try_creating_object(input_data)
        if entity_obj is not None:
            summary = input_data["summary"]
            self._cache_entity(input_data, entity_obj)
            self._update_permissions(input_data.get("permissions"), entity_obj, summary)

            created_attachments = self._create_attachments(
                input_data.get("attachments", []),
                entity_obj,
                attachments_path,
                summary,
            )
            text_normalizer = TextNormalizer(
                self._mapper,
                created_attachments,
                lambda attachment_id: f"{self._ui_url}/ajax/v2/attachments/{attachment_id}"
            )
            self._create_comments(input_data.get("comments", []), entity_obj, text_normalizer, summary)
            # формат входных данных описан в docs/: чеклист лежит в checklistItems. Ключ checklist
            # поддерживается ради данных, подготовленных по прежнему поведению импортёра
            self._create_checklist(
                input_data.get("checklistItems") or input_data.get("checklist", []),
                entity_obj,
                summary,
            )
            self._update_description(input_data.get("description"), entity_obj, text_normalizer, summary)

            if input_data.get("metricItems"):
                self._metric_items[self._get_key(input_data)] = input_data["metricItems"]

            self._entity_links_collector.add_entity_links(self._get_key(input_data), input_data.get("links", []))
            if input_data.get("parentEntity"):
                self._parent_entities[self._get_key(input_data)] = {
                    "primary": (
                        str(input_data["parentEntity"]["primary"])
                        if input_data.get("parentEntity") and input_data["parentEntity"].get("primary")
                        else None
                    ),
                    "secondary": (
                        [
                            str(parent_id)
                            for parent_id in input_data["parentEntity"]["secondary"]
                        ]
                        if input_data.get("parentEntity") and input_data["parentEntity"].get("secondary")
                        else []
                    ),
                }

        return entity_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "createdAt": input_data.get("createdAt"),
            "createdBy": self._mapper.get_user(input_data.get("createdBy")),
            "fields": {
                "summary": input_data["summary"],
                "teamAccess": input_data.get("teamAccess"),
                "description": input_data.get("description"),
                "markupType": input_data.get("markupType"),
                "author": self._mapper.get_user(input_data.get("author")),
                "lead": self._mapper.get_user(input_data.get("lead")),
                "teamUsers": [self._mapper.get_user(u) for u in input_data.get("teamUsers", [])],
                "clients": [self._mapper.get_user(u) for u in input_data.get("clients", [])],
                "followers": [self._mapper.get_user(u) for u in input_data.get("followers", [])],
                "tags": input_data.get("tags", []) + [self._get_searching_key(input_data)],
                "end": input_data.get("end"),
                "entityStatus": input_data.get("entityStatus"),
            }
        }

    def _cache_entity(self, input_data: Dict[str, Any], entity_obj: Resource) -> None:
        self._put_to_cache(self._get_key(input_data), entity_obj)
        self._entities_by_name.setdefault(input_data.get("summary"), []).append(entity_obj)

    def get_by_name(self, summary: str) -> Optional[Resource]:
        """Названия сущностей в Трекере не уникальны, поэтому неоднозначная ссылка не используется."""
        entities = self._entities_by_name.get(summary, [])
        if len(entities) > 1:
            logger.warning(
                f"Found {len(entities)} objects {self.object_name} named {summary}, "
                "the reference by name is ambiguous"
            )
            return None

        return entities[0] if entities else None

    def _get_key(self, input_data: Dict[str, Any]) -> str:
        if "id" not in input_data:
            logger.error(f"Missing 'id' in {self.object_name}. Input data: {input_data}")
            return None

        return str(input_data["id"])

    def _get_searching_key(self, input_data: Dict[str, Any]) -> str:
        return f"imported_{input_data['id']}"

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        for entity_obj in self.objects.find(
            filter={"tags": [self._get_searching_key(input_data)]},
        ).get("values", []):
            return entity_obj
        return None

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self.objects.import_object(**prepared_data)

    def _update_permissions(self, permissions_data: Dict[str, Any], entity_obj: Resource, summary: str) -> None:
        if not permissions_data or not permissions_data.get("acl"):
            return

        permission_types = ["GRANT", "WRITE", "READ"]
        permissions_update = {"acl": {"grant": {}, "revoke": {}}}

        for permission_type in permission_types:
            if not permissions_data["acl"].get(permission_type):
                continue

            groups_add = set()
            for group in permissions_data["acl"][permission_type].get("groups", []):
                group_id = self._group_resolver.get_group_id(group)
                if group_id is None:
                    # доступ от пропущенной группы только сужается, но молча такое не проходит
                    logger.warning(
                        f"Group {group} is not found, it is skipped in permissions of {summary}: "
                        "check mapping/groups.cfg"
                    )
                else:
                    groups_add.add(group_id)

            permissions_update["acl"]["grant"][permission_type] = {
                "users": [self._mapper.get_user(u) for u in permissions_data["acl"][permission_type].get("users", [])],
                "groups": list(groups_add) if groups_add else None,
                "roles": permissions_data["acl"][permission_type].get("roles", None),
            }

            if ALL_PARTICIPANTS_GROUP_ID not in groups_add:
                permissions_update["acl"]["revoke"][permission_type] = {
                    "groups": [ALL_PARTICIPANTS_GROUP_ID],
                }

        permissions_update = clean_empty(permissions_update)
        if not permissions_update:
            return

        try:
            self.objects[entity_obj.id].update_extended_permissions(permissions_update)
        except Exception:
            logger.exception(
                f"Error updating permissions for entity {summary} with data: {permissions_update}"
            )

    def _create_attachments(
        self,
        attachments_data: List[Dict[str, Any]],
        entity_obj: Resource,
        attachments_path: str,
        summary: str,
    ) -> Dict[str, Resource]:
        created_attachments = {}
        for attachment in attachments_data:
            try:
                attachment_obj = self._client.attachments.create(
                    os.path.join(attachments_path, attachment["filename"]),
                    params={"filename": attachment.get("name", attachment["filename"])}
                )
                entity_obj.attachments.attach(attachment_obj.id)
                created_attachments[attachment["filename"]] = attachment_obj
            except Exception:
                logger.exception(
                    f"Error creating attachment for entity {summary} with data: {attachment}"
                )

        return created_attachments

    def _create_checklist(self, checklist_data: List[Dict[str, Any]], entity_obj: Resource, summary: str) -> None:
        for checklist_item in checklist_data:
            try:
                prepared_data = prepare_checklist_item_data(checklist_item, self._mapper)
            except Exception:
                logger.exception(
                    f"Error preparing checklist item data for entity {summary}: {checklist_item}"
                )
                continue

            try:
                create_checklist_item(entity_obj.checklist_items, prepared_data)
            except Exception:
                logger.exception(
                    f"Error creating checklist item for entity {summary} with data: {prepared_data}"
                )

    def _create_comments(self, comments_data: List[Dict[str, Any]], entity_obj: Resource, text_normalizer: TextNormalizer, summary: str) -> None:
        for comment in comments_data:
            try:
                prepared_data = {
                    "text": text_normalizer.normalize_text(comment["text"]),
                    "summonees": [self._mapper.get_user(u) for u in comment.get("summonees", [])],
                }
            except Exception:
                logger.exception(
                    f"Error preparing comment data for entity {summary}: {comment}"
                )
                continue

            try:
                entity_obj.comments.create(**prepared_data)
            except Exception:
                logger.exception(
                    f"Error creating comment for entity {summary} with data: {prepared_data}"
                )

    def _update_description(self, old_description: str, entity_obj: Resource, text_normalizer: TextNormalizer, summary: str) -> None:
        new_description = text_normalizer.normalize_text(old_description)
        if new_description != old_description:
            try:
                entity_obj.update(fields={"description": new_description})
            except Exception:
                logger.exception(
                    "Error normalizing attachments in the description "
                    f"for entity {summary} with current description: {old_description}"
                )


class ProjectImporter(EntityImporter):

    @property
    def object_name(self) -> str:
        return "Project"

    @property
    def filename(self) -> str:
        return "project.yaml"

    @property
    def objects(self):
        return self._client.project

    def get_project_by_id(self, project_id: str) -> Resource:
        project_obj = self.get_from_cache(project_id)
        if project_obj is None:
            raise ProjectNotExistsError(project_id)

        return project_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        data = super()._prepare_data(input_data)
        data["fields"]["start"] = input_data.get("start")
        return clean_empty(data)


class GoalImporter(EntityImporter):

    @property
    def object_name(self) -> str:
        return "Goal"

    @property
    def filename(self) -> str:
        return "goal.yaml"

    @property
    def objects(self):
        return self._client.goal

    def get_goal_by_reference(self, goal_reference: Any) -> Resource:
        if isinstance(goal_reference, dict):
            # задачи, выгруженные прежними версиями экспортёра, ссылаются на цель её названием
            goal_obj = self.get_by_name(goal_reference.get("display"))
        else:
            goal_obj = self.get_from_cache(str(goal_reference))

        if goal_obj is None:
            raise GoalNotExistsError(goal_reference)

        return goal_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        data = super()._prepare_data(input_data)
        data["fields"]["keyResultItems"] = [
            {
                "type": item["type"],
                "text": item["text"],
                "assignee": self._mapper.get_user(item.get("assignee")),
                "deadline": self._prepare_deadline_data(item.get("deadline")),
                "progress": self._prepare_progress_data(item.get("progress")),
                "achieved": item.get("achieved"),
                "status": self._prepare_key_result_status(item.get("status")),
            }
            for item in input_data.get("keyResultItems", [])
        ]
        return clean_empty(data)

    def _prepare_key_result_status(self, status_data: Any) -> Optional[str]:
        # цели, выгруженные прежними версиями экспортёра, хранят статус объектом
        if isinstance(status_data, dict):
            return status_data.get("id")

        return status_data

    def _prepare_deadline_data(self, deadline_data: Dict[str, Any]) -> Optional[str]:
        if not deadline_data:
            return None

        return {
            "date": deadline_data["date"],
            "deadlineType": deadline_data["deadlineType"],
            "isExceeded": deadline_data.get("isExceeded"),
        }

    def _prepare_progress_data(self, progress_data: Dict[str, Any]) -> str:
        if not progress_data:
            return None

        return {
            "start": progress_data["start"],
            "end": progress_data["end"],
            "current": progress_data.get("current"),
        }


class PortfolioImporter(EntityImporter):

    @property
    def object_name(self) -> str:
        return "Portfolio"

    @property
    def filename(self) -> str:
        return "portfolio.yaml"

    @property
    def objects(self):
        return self._client.portfolio

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        data = super()._prepare_data(input_data)
        data["fields"]["start"] = input_data.get("start")
        return clean_empty(data)
