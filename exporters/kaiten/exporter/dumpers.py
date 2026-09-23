import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .fetcher import KaitenDataFetcher
from .kaiten_client import SimpleKaitenClient
from .utils import (
    dump_to_yaml,
    prepare_attachment_filename,
    title_to_key,
    to_tracker_date,
    to_tracker_datetime,
)

logger = logging.getLogger(__name__)

CATEGORY_NAME = {"en": "Kaiten", "ru": "Kaiten"}

# Kaiten swimlanes have no Tracker equivalent. When a board has more than one lane
# we carry each card's lane as a GLOBAL field (key is kaiten-prefixed to avoid
# clashing with existing Tracker fields), so tasks can be grouped by it.
LANE_FIELD_KEY = "kaitenLane"
LANE_FIELD_NAME = {"ru": "Дорожка (Kaiten)", "en": "Kaiten swimlane"}


class BaseDumper(ABC):

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass

    def _prepare_user(self, user: Optional[Dict[str, Any]]) -> Optional[str]:
        """Kaiten users carry ``email``, ``full_name`` and ``username``. We emit the
        most stable identifier (email) so it can be mapped to a Tracker login via
        ``mapping/users.cfg`` at import time."""
        if not user:
            return None
        return user.get("email") or user.get("full_name") or user.get("username")

    def _clean_and_save_data(self, prepared_data: Any, dump_path: str) -> None:
        prepared_data = self._clean_empty(prepared_data)
        os.makedirs(dump_path, exist_ok=True)
        dump_to_yaml(prepared_data, os.path.join(dump_path, self._filename))

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

    def dump(self, board: Dict[str, Any], dump_path: str) -> None:
        prepared_data = {
            "id": board.get("id"),
            "summary": board.get("title"),
            "teamAccess": self._STANDARD_TEAM_ACCESS,
            "description": board.get("description"),
        }
        self._clean_and_save_data(prepared_data, dump_path)


class BoardToQueueDumper(BaseDumper):

    _STANDARD_DEFAULT_TYPE = "task"
    _STANDARD_DEFAULT_PRIORITY = "normal"

    def __init__(self, card_types: List[Dict[str, Any]], custom_properties: List[Dict[str, Any]]):
        self._card_types = card_types
        self._custom_properties = custom_properties
        self._collected_statuses: Dict[str, str] = {}

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    @property
    def collected_statuses(self) -> Dict[str, str]:
        return self._collected_statuses

    def dump(self, board: Dict[str, Any], dump_path: str, queue_key: str, lead: Optional[str]) -> None:
        board_statuses = {
            title_to_key(column["title"]): column.get("title")
            for column in self._sorted_columns(board)
            if column.get("title")
        }

        prepared_data = {
            "key": queue_key,
            "name": board.get("title"),
            "lead": lead,
            "defaultType": self._STANDARD_DEFAULT_TYPE,
            "defaultPriority": self._STANDARD_DEFAULT_PRIORITY,
            "description": board.get("description"),
            "workflows": self._prepare_workflows_data(queue_key, board.get("title", "Unknown"), board_statuses),
            "issueTypesConfig": self._prepare_issue_types_config(queue_key),
            "local_fields": self._prepare_local_fields(),
        }

        self._clean_and_save_data(prepared_data, dump_path)
        self._collected_statuses.update(board_statuses)

    def _sorted_columns(self, board: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Kaiten may nest sub-columns; a flat, sort_order-ordered list of leaf columns
        # gives the workflow steps in board order.
        columns = board.get("columns", [])
        return sorted(columns, key=lambda c: c.get("sort_order", 0))

    def _get_workflow_key(self, queue_key: str) -> str:
        return f"W_{queue_key}"

    def _prepare_issue_types_config(self, queue_key: str) -> List[Dict[str, Any]]:
        workflow = self._get_workflow_key(queue_key)
        type_keys = {self._STANDARD_DEFAULT_TYPE}
        config = [{"issueType": self._STANDARD_DEFAULT_TYPE, "workflow": workflow}]
        for card_type in self._card_types:
            key = title_to_key(card_type.get("name"))
            if key and key not in type_keys:
                type_keys.add(key)
                config.append({"issueType": key, "workflow": workflow})
        return config

    def _prepare_workflows_data(self, queue_key: str, board_name: str, board_statuses: Dict[str, str]) -> List[Dict[str, Any]]:
        steps = [
            {
                "status": key,
                "metaAction": {"name": {"en": name, "ru": name}, "target": key},
            }
            for key, name in board_statuses.items()
        ] + [
            {
                "status": "resolved",
                "metaAction": {"name": {"en": "Resolved", "ru": "Решён"}, "target": "resolved"},
            }
        ]
        return [
            {
                "key": self._get_workflow_key(queue_key),
                "name": f"Рабочий процесс очереди \"{board_name}\"",
                "type": "visual",
                "initialAction": {"name": {"ru": "Не задано", "en": "Undefined"}, "target": "resolved"},
                "steps": steps,
            }
        ]

    def _prepare_local_fields(self) -> List[Dict[str, Any]]:
        result = []
        for prop in self._custom_properties:
            key = title_to_key(prop.get("name"))
            if not key:
                continue
            result.append({
                "category": CATEGORY_NAME["ru"],
                "key": key,
                "name": {"en": prop.get("name"), "ru": prop.get("name")},
                "schema": self._get_field_schema(prop),
                "optionsProvider": self._get_field_options(prop),
            })
        return result

    def _get_field_schema(self, prop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Kaiten expresses multi-value select via the ``multi_select`` flag on a
        # ``select`` type (there is no distinct ``multi_select`` type). User-type
        # property values are always arrays.
        prop_type = prop.get("type")
        if prop_type == "select":
            return {"type": "array", "items": "string"} if prop.get("multi_select") else {"type": "string"}
        if prop_type == "user":
            return {"type": "array", "items": "user"}
        schemas = {
            "string": {"type": "string"},
            "text": {"type": "text"},
            "number": {"type": "float"},
            "date": {"type": "date"},
            "checkbox": {"type": "string"},
            "link": {"type": "uri"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
        }
        if prop_type not in schemas:
            logger.warning(f"Unknown custom property type: {prop_type}")
        return schemas.get(prop_type, {"type": "string"})

    def _get_field_options(self, prop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if prop.get("type") == "select":
            return {
                "type": "FixedListOptionsProvider",
                "values": [v.get("value") for v in prop.get("select_values", []) if v.get("value")],
            }
        if prop.get("type") == "checkbox":
            return {"type": "FixedListOptionsProvider", "values": ["Да", "Нет"]}
        return None


class CardDumper(BaseDumper):

    _STANDARD_TYPE = "task"
    _STANDARD_PRIORITY = "normal"

    def __init__(self, client: SimpleKaitenClient, fetcher: KaitenDataFetcher, custom_properties: List[Dict[str, Any]]):
        self._client = client
        self._fetcher = fetcher
        self._properties_by_id = {prop["id"]: prop for prop in custom_properties}

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump(
        self,
        card: Dict[str, Any],
        dump_path: str,
        issue_key: str,
        key_by_card_id: Optional[Dict[Any, str]] = None,
        lane_field_active: bool = False,
    ) -> None:
        column = self._card_column(card)
        card_type = self._fetcher.resolve_card_type(card.get("type_id"))

        prepared_data = {
            "key": issue_key,
            "summary": card.get("title"),
            "createdAt": to_tracker_datetime(card.get("created")),
            "createdBy": self._prepare_user(card.get("owner") or self._fetcher.resolve_user(card.get("owner_id"))),
            "updatedAt": to_tracker_datetime(card.get("updated")),
            "type": title_to_key(card_type.get("name")) if card_type else self._STANDARD_TYPE,
            "priority": self._STANDARD_PRIORITY,
            "status": title_to_key(column.get("title")) if column else None,
            "description": self._prepare_description(card),
            "deadline": to_tracker_date(card.get("due_date")),
            "tags": self._prepare_tags(card),
            "assignee": self._prepare_assignee(card),
            "followers": self._prepare_members(card),
            "checklistItems": self._prepare_checklists(card.get("checklists", [])),
            "comments": self._prepare_comments(card.get("comments", [])),
            "attachments": self._prepare_attachments(card.get("files", [])),
            "worklogs": self._prepare_worklogs(card.get("time_logs", [])),
            "storyPoints": card.get("size"),
            "links": self._prepare_links(card, key_by_card_id or {}),
            "project": {"primary": card.get("board_id")},
            **self._prepare_local_fields(card),
        }

        if lane_field_active:
            lane_title = (card.get("lane") or {}).get("title")
            if lane_title:
                prepared_data[LANE_FIELD_KEY] = lane_title

        self._download_attachments(card.get("files", []), dump_path)
        self._clean_and_save_data(prepared_data, dump_path)

    def _card_column(self, card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # A trimmed card carries only column_id; the full card usually embeds
        # ``column`` with its title. Prefer the embedded object.
        if card.get("column"):
            return card["column"]
        return {"title": card.get("column_title")} if card.get("column_title") else None

    def _prepare_assignee(self, card: Dict[str, Any]) -> Optional[str]:
        # Kaiten members carry a ``type`` (2 == responsible/assignee, 1 == member).
        for member in card.get("members", []):
            if member.get("type") == 2:
                return self._prepare_user(member)
        return self._prepare_user(card.get("owner") or self._fetcher.resolve_user(card.get("owner_id")))

    def _prepare_members(self, card: Dict[str, Any]) -> List[str]:
        # Everyone except the responsible (``type == 2``, exported as ``assignee``).
        followers = []
        for member in card.get("members", []):
            if member.get("type") == 2:
                continue
            login = self._prepare_user(member)
            if login:
                followers.append(login)
        return followers

    def _prepare_tags(self, card: Dict[str, Any]) -> List[str]:
        tags = [tag.get("name") for tag in card.get("tags", []) if tag.get("name")]
        # Kaiten sprints have no Tracker equivalent; carry the sprint name as a tag.
        sprint = self._fetcher.resolve_sprint(card.get("sprint_id"))
        if sprint and sprint.get("title"):
            tags.append(sprint["title"])
        return tags

    def _prepare_description(self, card: Dict[str, Any]) -> Optional[str]:
        description = card.get("description") or ""
        # Rewrite embedded file links to the local ``[filename]()`` form the importer
        # resolves against the attachments directory.
        for file in card.get("files", []):
            url = file.get("url")
            if not url:
                continue
            local_name = prepare_attachment_filename(file.get("name", "file"), str(file.get("id")))
            escaped = re.escape(url)
            description = re.sub(
                rf"!\[[^\]]*\]\({escaped}[^)]*\)", f"![{local_name}]()", description
            )
            description = re.sub(
                rf"(?<!!)\[[^\]]*\]\({escaped}[^)]*\)", f"[{local_name}]()", description
            )
        return description or None

    def _prepare_checklists(self, checklists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for checklist in sorted(checklists, key=lambda c: c.get("sort_order", 0)):
            for item in sorted(checklist.get("items", []), key=lambda i: i.get("sort_order", 0)):
                checked = item.get("checked")
                if checked is None:
                    checked = item.get("completed")
                result.append({
                    "text": item.get("text") or item.get("name"),
                    "checked": bool(checked),
                    "assignee": self._prepare_user(item.get("responsible")),
                    "deadline": to_tracker_datetime(item.get("due_date")),
                })
        return result

    def _prepare_comments(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for comment in comments:
            data = {
                "text": comment.get("text"),
                "createdBy": self._prepare_user(comment.get("author")),
                "createdAt": to_tracker_datetime(comment.get("created")),
            }
            updated_at = comment.get("updated")
            if updated_at and updated_at != comment.get("created"):
                data["updatedBy"] = self._prepare_user(comment.get("author"))
                data["updatedAt"] = to_tracker_datetime(updated_at)
            result.append(data)
        return result

    def _prepare_attachments(self, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for file in files:
            att = {
                "filename": prepare_attachment_filename(file.get("name", "file"), str(file.get("id"))),
                "name": file.get("name"),
                "createdAt": to_tracker_datetime(file.get("created")),
                "createdBy": self._prepare_user(
                    file.get("author") or self._fetcher.resolve_user(file.get("author_id"))
                ),
            }
            result.append(att)
        return result

    def _prepare_links(self, card: Dict[str, Any], key_by_card_id: Dict[Any, str]) -> List[Dict[str, Any]]:
        # Kaiten parent/child relations. Emit one direction (parent -> child) only;
        # the importer creates the reciprocal link. Cross-board relations whose
        # counterpart isn't in this export are skipped.
        links = []
        for child_id in card.get("children_ids") or []:
            key = key_by_card_id.get(child_id)
            if key:
                links.append({"issue": key, "relationship": "is parent task for"})
        for blocker in card.get("blockers") or []:
            if blocker.get("released"):
                continue
            key = key_by_card_id.get(blocker.get("blocker_card_id"))
            if key:
                links.append({"issue": key, "relationship": "depends on"})
        return links

    def _prepare_worklogs(self, time_logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for log in time_logs:
            minutes = log.get("time_spent") or log.get("duration") or 0
            result.append({
                "start": to_tracker_datetime(log.get("for_date") or log.get("date") or log.get("created")),
                "duration": int(minutes) * 60 * 1000,  # Kaiten stores minutes; Tracker wants ms
                "comment": log.get("comment"),
            })
        return result

    def _prepare_local_fields(self, card: Dict[str, Any]) -> Dict[str, Any]:
        local_fields = {}
        # Kaiten stores custom property values on the card under ``properties`` keyed
        # by ``id_<propertyId>``.
        properties = card.get("properties", {}) or {}
        for raw_key, raw_value in properties.items():
            prop_id = self._parse_property_id(raw_key)
            prop = self._properties_by_id.get(prop_id)
            if not prop:
                # Company custom properties (/company/custom-properties) cover every
                # value a card can carry; a miss means an unexpected property scope.
                logger.warning(f"Card {card.get('id')} has value for unknown custom property {raw_key}")
                continue
            key = title_to_key(prop.get("name"))
            if not key:
                continue
            local_fields[f"local_{key}"] = self._resolve_property_value(prop, raw_value)
        return local_fields

    def _parse_property_id(self, raw_key: str) -> Any:
        match = re.match(r"id_(\d+)", str(raw_key))
        if match:
            return int(match.group(1))
        return raw_key

    def _resolve_property_value(self, prop: Dict[str, Any], value: Any) -> Any:
        if value is None:
            return None
        prop_type = prop.get("type")

        if prop_type == "select":
            # Values are stored as a list of select-value ids (even single-select).
            select_by_id = {v.get("id"): v.get("value") for v in prop.get("select_values", [])}
            ids = value if isinstance(value, list) else [value]
            names = [select_by_id.get(v, v) for v in ids]
            if prop.get("multi_select"):
                return names
            return names[0] if names else None
        if prop_type == "checkbox":
            return "Да" if value else "Нет"
        if prop_type == "user":
            # Read back as a list of numeric user ids.
            ids = value if isinstance(value, list) else [value]
            return [self._prepare_user(self._fetcher.resolve_user(v)) for v in ids]
        if prop_type == "date":
            # Date values come as an object; fall back to a plain string.
            raw = value.get("date") if isinstance(value, dict) else value
            return to_tracker_date(raw)
        return value

    def _download_attachments(self, files: List[Dict[str, Any]], card_path: str) -> None:
        if not files:
            return

        attachments_path = os.path.join(card_path, "attachments")
        os.makedirs(attachments_path, exist_ok=True)

        for file in files:
            url = file.get("url")
            if not url:
                continue
            try:
                file_path = os.path.join(
                    attachments_path,
                    prepare_attachment_filename(file.get("name", "file"), str(file.get("id"))),
                )
                self._client.download_file(url, file_path)
            except Exception:
                logger.exception(f"Failed to download {file.get('name')}")
