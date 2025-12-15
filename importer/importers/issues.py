import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource
from yandex_tracker_client.exceptions import BadRequest

from .base import GetOrCreateImporter
from .entities import ProjectImporter
from .fields import GlobalFieldImporter, LocalFieldImporter
from .queue_objects import ComponentImporter, VersionImporter
from ..link_collectors import IssueLinksCollector
from ..exceptions import LocalFieldNotExistsError
from ..mapper import EntityMapper
from ..text_normalizer import TextNormalizer
from ..utils import (
    datetime_to_str,
    str_to_datetime,
    prepare_checklist_item_data,
    map_custom_user_field_value,
    clean_empty,
    retry
)


logger = logging.getLogger(__name__)


class IssueImporter(GetOrCreateImporter):
    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        local_field_importer: LocalFieldImporter,
        global_field_importer: GlobalFieldImporter,
        component_importer: ComponentImporter,
        version_importer: VersionImporter,
        project_importer: ProjectImporter,
        issue_links_collector: IssueLinksCollector
    ):
        super().__init__(client, mapper)
        self._local_field_importer = local_field_importer
        self._global_field_importer = global_field_importer
        self._component_importer = component_importer
        self._version_importer = version_importer
        self._project_importer = project_importer
        self._issue_links_collector = issue_links_collector

    @property
    def object_name(self) -> str:
        return "Issue"

    def get_or_create(self, input_data: Dict[str, Any], attachments_path: str) -> Tuple[Optional[Resource], bool]:
        issue_obj = self._try_getting_object(input_data)
        if issue_obj is not None:
            return issue_obj

        attachments_data = input_data.pop("attachments", [])
        checklist_data = input_data.pop("checklistItems", [])
        worklogs_data = input_data.pop("worklogs", [])
        comments_data = input_data.pop("comments", [])
        links = input_data.pop("links", [])

        issue_obj = self._try_creating_object(input_data)
        if issue_obj is not None:
            created_attachments = self._create_attachments(attachments_data, issue_obj, attachments_path)
            text_normalizer = TextNormalizer(
                self._mapper,
                created_attachments,
                lambda attachment_id: f"https://tracker.yandex.ru/{issue_obj.key}/attachments/{attachment_id}"
            )
            issue_obj = self._create_local_fields(input_data, issue_obj)
            issue_obj = self._update_description(issue_obj, text_normalizer)
            self._create_checklist(checklist_data, issue_obj)
            self._create_worklogs(worklogs_data, issue_obj)
            self._create_comments(comments_data, issue_obj, text_normalizer)
            self._issue_links_collector.add_issue_links(issue_obj, links)

        return issue_obj

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        issue_key = self._mapper.get_issue_key(input_data["key"])
        queue_key = issue_key.split("-")[0]

        prepared_data = {
            "key": issue_key,
            "queue": queue_key,
            "summary": input_data["summary"],
            "createdAt": input_data["createdAt"],
            "createdBy": self._mapper.get_user(input_data.get("createdBy")),
            "updatedAt": self._get_updated_at(input_data),
            "resolvedAt": input_data.get("resolvedAt"),
            "deadline": input_data.get("deadline"),
            "start": input_data.get("start"),
            "end": input_data.get("end"),
            "updatedBy": self._mapper.get_user(input_data.get("updatedBy")),
            "resolvedBy": self._mapper.get_user(input_data.get("resolvedBy")),
            "assignee": self._mapper.get_user(input_data.get("assignee")),
            "followers": [self._mapper.get_user(u) for u in input_data.get("followers", [])],
            "access": [self._mapper.get_user(u) for u in input_data.get("access", [])],
            "votedBy": [self._mapper.get_user(u) for u in input_data.get("votedBy", [])],
            "favoritedBy": [self._mapper.get_user(u) for u in input_data.get("favoritedBy", [])],
            "status": self._mapper.get_status(input_data.get("status")),
            "resolution": self._mapper.get_resolution(input_data.get("resolution")),
            "type": self._mapper.get_issue_type(input_data.get("type")),
            "priority": self._mapper.get_priority(input_data.get("priority")),
            "description": input_data.get("description"),
            "affectedVersions": [
                self._version_importer.get_version_by_name(version_name).id
                for version_name in input_data.get("affectedVersions", [])
            ],
            "fixVersions": [
                self._version_importer.get_version_by_name(version_name).id
                for version_name in input_data.get("fixVersions", [])
            ],
            "components": [
                self._component_importer.get_component_by_name(component_name).id
                for component_name in input_data.get("components", [])
            ],
            "project": {
                "primary": (
                    self._project_importer.get_project_by_id(input_data["project"]["primary"]).shortId
                    if input_data.get("project") and input_data["project"].get("primary")
                    else None
                ),
                "secondary": (
                    [
                        self._project_importer.get_project_by_id(project_id).shortId
                        for project_id in input_data["project"]["secondary"]
                    ]
                    if input_data.get("project") and input_data["project"].get("secondary")
                    else []
                ),
            },
            "tags": input_data.get("tags", []),
            "followingMaillists": input_data.get("followingMaillists", []),
            "originalEstimation": input_data.get("originalEstimation"),
            "estimation": input_data.get("estimation"),
            "spent": input_data.get("spent"),
            "storyPoints": input_data.get("storyPoints"),
            "unique": input_data.get("unique"),
        }

        for key, value in input_data.items():
            if (
                key not in prepared_data
                and not key.startswith("local_")
            ):
                field_obj = self._global_field_importer.get_or_create(key)
                prepared_data[field_obj.id] = map_custom_user_field_value(field_obj, value, self._mapper)

        return clean_empty(prepared_data)

    def _get_object(self, data: Dict[str, Any]) -> Optional[Resource]:
        return self._client.issues.get(self._mapper.get_issue_key(data['key']))

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._client.issues.import_object(**prepared_data)

    def _create_local_fields(self, data: Dict[str, Any], issue_obj: Resource) -> Optional[Resource]:
        queue_key = self._mapper.get_queue(data["key"].split("-")[0])

        local_fields_prepared_data = {}
        for key, value in data.items():
            if key.startswith("local_"):
                try:
                    local_field_obj = self._local_field_importer.get_local_field(key)
                except LocalFieldNotExistsError:
                    logger.error(
                        f"Error creating a local field in the issue {issue_obj.key}. "
                        f"Local Field '{key}' in queue '{queue_key}' does not exist"
                    )
                    continue

                local_fields_prepared_data[local_field_obj.id] = map_custom_user_field_value(local_field_obj, value, self._mapper)

        @retry(max_attempts=5, delay=2, backoff=2, exceptions=BadRequest)
        def update_issue(issue_obj: Resource, local_fields_prepared_data: Dict[str, Any]) -> Resource:
            return self._client.issues[issue_obj.key].update(**local_fields_prepared_data)

        return update_issue(issue_obj, local_fields_prepared_data)

    def _create_attachments(
        self,
        attachments_data: List[Dict[str, Any]],
        issue_obj: Resource,
        attachments_path: str
    ) -> Dict[str, Resource]:
        created_attachments = {}
        issue_created_at = str_to_datetime(issue_obj.createdAt)
        for attachment in attachments_data:
            try:
                attachment_created_at = str_to_datetime(attachment.get("createdAt"))
                if attachment_created_at and issue_created_at > attachment_created_at:
                    attachment["createdAt"] = datetime_to_str(issue_created_at)
                    logger.warning(
                        f"The date of creation in the attachment for issue {issue_obj.key} has been normalized: {attachment}"
                    )

                prepared_data = {
                    "full_path": os.path.join(attachments_path, attachment["filename"]),
                    "name": attachment.get("name", attachment["filename"]),
                    "createdAt": attachment.get("createdAt"),
                    "createdBy": self._mapper.get_user(attachment.get("createdBy")),
                }
            except Exception:
                logger.exception(f"Error in preparing attachment data for issue {issue_obj.key}: {attachment}")
                continue

            attachment_obj = None
            try:
                attachment_obj = issue_obj.attachments.import_object(
                    prepared_data["full_path"],
                    params={
                        "filename": prepared_data["name"],
                        "createdAt": prepared_data["createdAt"],
                        "createdBy": prepared_data["createdBy"],
                    }
                )
            except Exception:
                logger.exception(
                    f"Error importing attachment for issue {issue_obj.key} with data: {prepared_data}"
                )

            if attachment_obj is None:
                try:
                    logger.info(f"Attempt to create attachment {prepared_data['full_path']}")
                    attachment_obj = issue_obj.attachments.create(
                        prepared_data["full_path"],
                        params={"filename": prepared_data["name"]}
                    )
                except Exception:
                    logger.exception(
                        f"Error creating attachment for issue {issue_obj.key} with path: {prepared_data['full_path']}"
                    )

            if attachment_obj is not None:
                created_attachments[attachment["filename"]] = attachment_obj

        return created_attachments

    def _create_checklist(self, checklist_data: List[Dict[str, Any]], issue_obj: Resource) -> None:
        for checklist_item in checklist_data:
            try:
                prepared_data = prepare_checklist_item_data(checklist_item, self._mapper)
            except Exception:
                logger.exception(f"Error preparing checklist item for issue {issue_obj.key} with data: {checklist_item}")
                continue

            try:
                issue_obj.checklist_items.create(**prepared_data)
            except Exception:
                logger.exception(f"Error creating checklist item for issue {issue_obj.key} with data: {prepared_data}")

    def _create_worklogs(self, worklogs_data: List[Dict[str, Any]], issue_obj: Resource) -> None:
        issue_created_at = str_to_datetime(issue_obj.createdAt)
        for worklog in worklogs_data:
            try:
                worklog_start = str_to_datetime(worklog["start"])
                if issue_created_at > worklog_start:
                    worklog["start"] = datetime_to_str(issue_created_at)
                    logger.warning(
                        f"The start date in the worklog for issue {issue_obj.key} has been normalized: {worklog}"
                    )

                prepared_data = {
                    "start": worklog["start"],
                    "duration": worklog["duration"],
                    "comment": worklog.get("comment"),
                }
            except Exception:
                logger.exception(f"Error in preparing worklog data for issue {issue_obj.key}: {worklog}")
                continue

            try:
                issue_obj.worklog.create(**prepared_data)
            except Exception:
                logger.exception(f"Error creating worklog for issue {issue_obj.key} with data: {prepared_data}")

    def _create_comments(self, comments_data: List[Dict[str, Any]], issue_obj: Resource, text_normalizer: TextNormalizer) -> None:
        issue_created_at = str_to_datetime(issue_obj.createdAt)
        for comment in comments_data:
            try:
                comment_created_at = str_to_datetime(comment["createdAt"])
                comment_updated_at = str_to_datetime(comment.get("updatedAt"))
                if issue_created_at > comment_created_at:
                    comment["createdAt"] = datetime_to_str(issue_created_at)
                    comment_created_at = issue_created_at
                    logger.warning(
                        f"The date of creation in the comment for issue {issue_obj.key} has been normalized: {comment}"
                    )

                if (
                    comment_updated_at is not None
                    and comment_created_at > comment_updated_at
                ):
                    comment["updatedAt"] = datetime_to_str(comment_created_at)
                    logger.warning(
                        f"The date of the update in the comment for issue {issue_obj.key} has been normalized: {comment}"
                    )

                prepared_data = {
                    "text": text_normalizer.normalize_text(comment["text"]),
                    "createdAt": comment["createdAt"],
                    "createdBy": self._mapper.get_user(comment["createdBy"]),
                    "updatedAt": comment.get("updatedAt"),
                    "updatedBy": self._mapper.get_user(comment.get("updatedBy")),
                }
            except Exception:
                logger.exception(f"Error in preparing comment data for issue {issue_obj.key}: {comment}")
                continue

            try:
                issue_obj.comments.import_object(**prepared_data)
            except Exception:
                logger.exception(f"Error creating comment for issue {issue_obj.key} with data: {prepared_data}")

    def _update_description(self, issue_obj: Resource, text_normalizer: TextNormalizer) -> Resource:
        old_description = issue_obj.description
        new_description = text_normalizer.normalize_text(old_description)
        if new_description != old_description:
            try:
                issue_obj = self._client.issues[issue_obj.key].update(description=new_description)
            except Exception:
                logger.exception(
                    "Error normalizing attachments in the description "
                    f"for issue {issue_obj.key} with current description: {old_description}"
                )

        return issue_obj

    def _get_updated_at(self, input_data: Dict[str, Any]) -> Optional[str]:
        # get most recent timestamp from input data
        options = {input_data[field_key] for field_key in ("updatedAt", "resolvedAt") if input_data.get(field_key)}
        return max(options) if options else None
