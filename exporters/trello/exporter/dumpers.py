import os
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional


from .trello_client import SimpleTrelloClient
from .utils import (
    dump_to_yaml,
    prepare_attachment_filename,
    prepare_attachment_links,
    title_to_key
)

logger = logging.getLogger(__name__)


class BaseDumper(ABC):

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass

    @abstractmethod
    def dump(self, data: Dict[str, Any], dump_path: str, **kwargs) -> None:
        pass

    def _prepare_user(self, user: Dict[str, Any]) -> Optional[str]:
        if not user:
            return None

        return user.get("fullName")

    def _clean_and_save_data(self, prepared_data: Any, dump_path: str) -> None:
        prepared_data = self._clean_empty(prepared_data)

        os.makedirs(dump_path, exist_ok=True)
        file_path = os.path.join(dump_path, f"{self._filename}")
        dump_to_yaml(prepared_data, file_path)

    def _clean_empty(self, data: Any) -> Any:
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                cleaned_value = self._clean_empty(value)
                if cleaned_value not in (None, "", [], {}):
                    result[key] = cleaned_value
            return result
        elif isinstance(data, list):
            result = []
            for item in data:
                cleaned_item = self._clean_empty(item)
                if cleaned_item not in (None, "", [], {}):
                    result.append(cleaned_item)
            return result
        else:
            return data


class BoardToProjectDumper(BaseDumper):
    _STANDARD_TEAM_ACCESS = True

    @property
    def _filename(self) -> str:
        return "project.yaml"

    def dump(self, data: Dict[str, Any], dump_path: str) -> None:
        prepared_data = {
            "id": data.get("id"),
            "summary": data.get("name"),
            "teamAccess": self._STANDARD_TEAM_ACCESS,
            "description": data.get("desc"),
            "author": self._prepare_user(data.get("creator")),
            "teamUsers": [member.get("fullName") for member in data.get("members", [])]
        }

        self._clean_and_save_data(prepared_data, dump_path)


class BoardToQueueDumper(BaseDumper):

    _STANDARD_DEFAULT_TYPE = "task"
    _STANDARD_DEFAULT_PRIORITY = "normal"
    _STANDARD_CATEGORY_FIELD = "Из Trello"

    def __init__(self):
        self._collected_statuses = {}

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    @property
    def collected_statuses(self) -> Dict[str, str]:
        return self._collected_statuses

    def dump(self, data: Dict[str, Any], dump_path: str, queue_key: str) -> None:
        board_statuses = {
            title_to_key(board_list["name"]): board_list.get("name")
            for board_list in data.get("lists", [])
            if board_list.get("name")
        }

        prepared_data = {
            "key": queue_key,
            "name": data.get("name"),
            "lead": self._prepare_user(data.get("creator")),
            "defaultType": self._STANDARD_DEFAULT_TYPE,
            "defaultPriority": self._STANDARD_DEFAULT_PRIORITY,
            "description": data.get("desc"),
            "teamUsers": [self._prepare_user(member) for member in data.get("members", [])],
            "workflows": self._prepare_workflows_data(
                queue_key,
                data.get("name", "Unknown"),
                board_statuses
            ),
            "issueTypesConfig": self._prepare_issue_types_config(queue_key),
            "local_fields": self._prepare_custom_fields(data.get("custom_fields", [])),
        }

        self._clean_and_save_data(prepared_data, dump_path)
        self._collected_statuses.update(board_statuses)

    def _get_workflow_key(self, queue_key: str) -> str:
        return f"W_{queue_key}"

    def _prepare_issue_types_config(self, queue_key: str) -> List[Dict[str, Any]]:
        return [{
            "issueType": self._STANDARD_DEFAULT_TYPE,
            "workflow": self._get_workflow_key(queue_key)
        }]

    def _prepare_workflows_data(self, queue_key: str, board_name: str, board_statuses: Dict[str, str]) -> List[Dict[str, Any]]:
        return [
            {
                "key": self._get_workflow_key(queue_key),
                "name": f"Рабочий процесс очереди \"{board_name}\"",
                "type": "visual",
                "initialAction": {
                    "name": {
                        "ru": "Не задано",
                        "en": "Undefined",
                    },
                    "target": "resolved",
                },
                "steps": [
                    {
                        "status": key,
                        "metaAction": {
                            "name": {
                                "en": f"{name}",
                                "ru": f"{name}"
                            },
                            "target": key
                        }
                    }
                    for key, name in board_statuses.items()
                ] + [{
                    "status": "resolved",
                    "metaAction": {
                        "name": {
                            "en": "Решен",
                            "ru": "Resolved"
                        },
                        "target": "resolved"
                    }
                }]
            }
        ]

    def _prepare_custom_fields(self, custom_fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for field in custom_fields:
            field_data = {
                "category": self._STANDARD_CATEGORY_FIELD,
                "key": title_to_key(field.get("name")),
                "name": {"en": field.get("name"), "ru": field.get("name")},
                "schema": self._get_field_schema(field.get("type")),
                "optionsProvider": self._get_field_options(field),
            }
            result.append(field_data)

        return result

    def _get_field_schema(self, field_type: Optional[str]) -> Optional[Dict[str, Any]]:
        if field_type is None:
            return None

        schemas = {
            "list": {"type": "string"},
            "number": {"type": "float"},
            "checkbox": {"type": "string"},
            "date": {"type": "datetime"},
            "text": {"type": "string"},
        }

        if field_type not in schemas:
            logger.warning(f"Unknown field type: {field_type}")

        return schemas.get(field_type)

    def _get_field_options(self, field: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        field_type = field.get("type")
        if field_type == "list":
            options = field.get("options", [])
            return {
                "type": "FixedListOptionsProvider",
                "values": [
                    opt.get("value", {}).get("text")
                    for opt in options
                ]
            }
        elif field_type == "checkbox":
            return {"type": "FixedListOptionsProvider", "values": ["Да", "Нет"]}

        return None


class CardDumper(BaseDumper):

    _STANDARD_TYPE = "task"
    _STANDARD_PRIORITY = "normal"

    def __init__(self, client: SimpleTrelloClient):
        self._client = client

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump(self, data: Dict[str, Any], dump_path: str, issue_key: str) -> None:
        create_info = data.get("create_info", {})
        update_info = data.get("update_info", {})
        list_info = data.get("list_info", {})

        prepared_data = {
            "key": issue_key,
            "summary": data.get("name"),
            "createdAt": create_info.get("date"),
            "createdBy": self._prepare_user(create_info.get("memberCreator")),
            "updatedAt": update_info.get("date"),
            "updatedBy": self._prepare_user(update_info.get("memberCreator")),
            "type": self._STANDARD_TYPE,
            "priority": self._STANDARD_PRIORITY,
            "status": title_to_key(list_info.get("name")),
            "description": self._prepare_description(data),
            "deadline": data.get("due"),
            "tags": [label.get("name") for label in data.get("labels", []) if label.get("name")],
            "followers": data.get("members_full", []),
            "checklistItems": self._prepare_checklists(data.get("checklists", [])),
            "comments": self._prepare_comments(data.get("comments", [])),
            "attachments": self._prepare_attachments(data.get("attachments", [])),
            "project": {"primary": data.get("board_id")},
            **self._prepare_local_fields(data)
        }

        self._download_attachments(data.get("attachments"), dump_path)
        self._clean_and_save_data(prepared_data, dump_path)

    def _prepare_description(self, card_data: Dict[str, Any]) -> Optional[str]:
        READABLE_DATA_PATTERN = "%d.%m.%Y %H:%M:%S"
        new_description = prepare_attachment_links(card_data.get("desc", ""))

        attachments = card_data.get("attachments", [])

        non_downloadable = [a for a in attachments if not a.get("isUpload")]

        if not non_downloadable:
            return new_description

        attachments_section = "\n## Ссылки\n"
        for attachment in non_downloadable:
            attachments_section += f"- [{attachment['name']}]({attachment['url']})"
            if attachment.get("date"):
                dt = datetime.fromisoformat(attachment['date'].replace('Z', '+00:00'))
                attachments_section += f"  \n  *Добавлено: {dt.strftime(READABLE_DATA_PATTERN)}*"
            attachments_section += "\n"

        return new_description + attachments_section

    def _prepare_checklists(self, checklists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for checklist in checklists:
            items = checklist.get("checkItems", [])
            for item in items:
                item_data = {
                    "text": item.get("name"),
                    "checked": item.get("state", "incomplete") == "complete",
                    "assignee": self._prepare_user(item.get("member")),
                    "deadline": item.get("due"),
                }

                result.append(item_data)

        return result

    def _prepare_comments(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for comment in comments:
            data = {
                "text": prepare_attachment_links(comment.get("data", {}).get("text", "")),
                "createdBy": self._prepare_user(comment.get("memberCreator")),
                "createdAt": comment.get("date"),
            }

            updated_at = comment.get("data", {}).get("dateLastEdited")
            if updated_at:
                data["updatedBy"] = self._prepare_user(comment.get("memberCreator"))
                data["updatedAt"] = updated_at

            result.append(data)

        return result

    def _prepare_local_fields(self, card_data: Dict[str, Any]) -> Dict[str, Any]:
        local_fields = {}
        field_definitions = {field["id"]: field for field in card_data.get("board_custom_fields", [])}
        for field_item in card_data.get("custom_field_items", []):
            field_def = field_definitions.get(field_item.get("idCustomField"))

            if field_def:
                key = f"local_{title_to_key(field_def['name'])}"
                value = self._get_field_value(field_item, field_def)

                local_fields[key] = value

        return local_fields

    def _get_field_value(self, field_item: Dict[str, Any], field_def: Dict[str, Any]) -> Any:

        def get_list_value(value_id: Optional[str], options: List[Dict]) -> Optional[str]:
            if not value_id:
                return None

            for option in options:
                if option["id"] == value_id:
                    return option.get("value", {}).get("text")
            return None

        field_type = field_def.get("type")
        value_data = field_item.get("value", {})

        processors = {
            "list": lambda: get_list_value(field_item.get("idValue"), field_def.get("options", [])),
            "checkbox": lambda: "Да" if value_data.get("checked", False) else "Нет",
            "number": lambda: value_data.get("number"),
            "text": lambda: value_data.get("text"),
            "date": lambda: value_data.get("date"),
        }

        processor = processors.get(field_type, lambda: field_item.get("value"))
        return processor()

    def _prepare_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for attachment in attachments:
            if attachment.get("isUpload"):
                att_data = {
                    "filename": prepare_attachment_filename(attachment["name"], attachment["id"]),
                    "name": attachment["name"]
                }

                if attachment.get("memberCreator"):
                    att_data["createdAt"] = attachment.get("date")
                    att_data["createdBy"] = self._prepare_user(attachment["memberCreator"])
                result.append(att_data)
        return result

    def _download_attachments(self, attachments: List[Dict[str, Any]], card_path: str) -> None:
        if not attachments:
            return

        attachments_path = os.path.join(card_path, "attachments")
        os.makedirs(attachments_path, exist_ok=True)

        for attachment in attachments:
            if attachment.get("isUpload"):
                try:
                    file_path = os.path.join(
                        attachments_path,
                        prepare_attachment_filename(attachment["name"], attachment["id"])
                    )
                    self._client.download_attachment(attachment["url"], file_path)
                except Exception:
                    logger.exception(f"Failed to download {attachment['name']}")
