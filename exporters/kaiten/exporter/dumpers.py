import logging
import os
import re
from urllib.parse import quote
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .fetcher import KaitenDataFetcher
from .kaiten_client import SimpleKaitenClient
from .utils import (
    TRACKER_DATETIME_FORMAT,
    KeyAllocator,
    NameAllocator,
    dump_to_yaml,
    issue_type_keys,
    latest_tracker_datetime,
    local_fields_by_property_id,
    prepare_attachment_filename,
    to_local_date,
    to_tracker_date,
    to_tracker_datetime,
)

logger = logging.getLogger(__name__)

CATEGORY_NAME = {"en": "Kaiten", "ru": "Kaiten"}

# an archived card is an issue with this tag, off the board and its sprints
ARCHIVE_TAG = "Архив Kaiten"

# characters kept as is in a link url; "(", ")" and spaces are percent-encoded
URL_SAFE_CHARS = ":/?#@!$&'*+,;=%-._~"

# a blocked card is an issue with this tag; a blocker reason becomes a comment
BLOCKED_TAG = "Заблокировано в Kaiten"
BLOCKER_COMMENT_PREFIX = "Заблокировано: "

# heading of the description list of files linked from external storages
EXTERNAL_FILES_HEADER = "Файлы по ссылкам:"

# resolution of done cards
RESOLUTION_KEY = "fixed"
RESOLUTION_NAME = {"en": "Fixed", "ru": "Решён"}

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

    def dump(
        self, board: Dict[str, Any], dump_path: str, created_by: Optional[str] = None,
        permissions: Optional[Dict[str, Any]] = None,
    ) -> None:
        prepared_data = {
            "id": board.get("id"),
            "summary": board.get("title"),
            "createdAt": to_tracker_datetime(board.get("created")),
            "createdBy": created_by,
            "teamAccess": self._STANDARD_TEAM_ACCESS,
            "description": board.get("description"),
            "permissions": permissions,
        }
        self._clean_and_save_data(prepared_data, dump_path)


class BoardToQueueDumper(BaseDumper):

    _STANDARD_DEFAULT_TYPE = "task"
    _STANDARD_DEFAULT_PRIORITY = "normal"

    # Kaiten column type -> Tracker status type
    _STATUS_TYPES = {1: "new", 2: "inProgress", 3: "done"}
    # Kaiten WIP limit types: 1 — number of cards, 2 — total card size
    _WIP_LIMIT_BY_COUNT = 1

    def __init__(self, card_types: List[Dict[str, Any]], custom_properties: List[Dict[str, Any]]):
        self._custom_properties = custom_properties
        self._type_keys = issue_type_keys(card_types)
        self._local_fields = local_fields_by_property_id(custom_properties)
        # statuses are global: one status per name across all boards; `resolved` is the final step
        # of every workflow
        self._collected_statuses: Dict[str, Dict[str, Any]] = {}
        self._status_keys = KeyAllocator(reserved=["resolved"])
        self._status_names = NameAllocator()

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    @property
    def collected_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Status key -> ``{"name": ..., "type": ...}``."""
        return self._collected_statuses

    def dump(
        self, board: Dict[str, Any], dump_path: str, queue_key: str, lead: Optional[str],
        permissions: Optional[Dict[str, Any]] = None,
    ) -> Dict[Any, str]:
        """Dumps the queue and returns the status key of every column and subcolumn by its id."""
        status_by_column_id: Dict[Any, str] = {}
        board_statuses: Dict[str, str] = {}
        for column, name, status_type in self._status_columns(board):
            key = self._status_key(name, column, status_type)
            status_by_column_id[column.get("id")] = key
            board_statuses.setdefault(key, name)

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
            "permissions": permissions,
        }

        self._clean_and_save_data(prepared_data, dump_path)
        return status_by_column_id

    def _status_columns(self, board: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str, str]]:
        """Columns that hold cards, in board order, with status names and types. A column with
        subcolumns holds cards only in the subcolumns. A subcolumn is named and typed after its column:
        Kaiten sets the state of a card by the top-level column."""
        result = []
        for column in self._by_sort_order(board.get("columns") or []):
            column_name = self._column_name(column)
            column_type = self._STATUS_TYPES.get(column.get("type"))
            subcolumns = self._by_sort_order(column.get("subcolumns") or [])
            if not subcolumns:
                result.append((column, column_name, self._status_type(column_type, column_name)))
            for subcolumn in subcolumns:
                name = f"{column_name} / {self._column_name(subcolumn)}"
                result.append((subcolumn, name, self._status_type(column_type, name)))
        return result

    @staticmethod
    def _status_type(status_type: Optional[str], name: str) -> str:
        if status_type is None:
            logger.warning(f"Column {name} has no known type, its status is exported as inProgress")
            return "inProgress"
        return status_type

    def board_columns(self, board: Dict[str, Any], status_by_column_id: Dict[Any, str]) -> List[Dict[str, Any]]:
        """Columns of the Tracker board: a column holds the statuses of its subcolumns and its WIP limit
        by the number of cards."""
        result = []
        for column in self._by_sort_order(board.get("columns") or []):
            leaves = self._by_sort_order(column.get("subcolumns") or []) or [column]
            statuses = []
            for leaf in leaves:
                status = status_by_column_id.get(leaf.get("id"))
                if status and status not in statuses:
                    statuses.append(status)
                if leaf is not column and leaf.get("wip_limit"):
                    logger.warning(
                        f"Subcolumn {self._column_name(leaf)} of column {self._column_name(column)} has a WIP limit, "
                        "it is not exported"
                    )
            limit = column.get("wip_limit")
            if limit and column.get("wip_limit_type") not in (None, self._WIP_LIMIT_BY_COUNT):
                logger.warning(f"Column {self._column_name(column)} has a WIP limit by card size, it is not exported")
                limit = None
            result.append({"id": column.get("id"), "name": self._column_name(column), "statuses": statuses, "limit": limit})
        return result

    @staticmethod
    def _by_sort_order(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(columns, key=lambda c: c.get("sort_order") or 0)

    @staticmethod
    def _column_name(column: Dict[str, Any]) -> str:
        return (column.get("title") or "").strip() or f"Колонка {column.get('id')}"

    def _status_key(self, column_name: str, column: Dict[str, Any], status_type: str) -> str:
        name = self._status_names.name(column_name)
        key = self._status_keys.key(name, f"column{column.get('id')}")
        status = self._collected_statuses.setdefault(key, {"name": name, "type": status_type})
        if status["type"] != status_type:
            # a status name is unique in Tracker: columns with one name share one status
            logger.warning(f"Columns {name} have different types, the status type is {status['type']}")
        return key

    def _get_workflow_key(self, queue_key: str) -> str:
        return f"W_{queue_key}"

    def _prepare_issue_types_config(self, queue_key: str) -> List[Dict[str, Any]]:
        workflow = self._get_workflow_key(queue_key)
        type_keys = {self._STANDARD_DEFAULT_TYPE}
        resolutions = [RESOLUTION_KEY]
        config = [{"issueType": self._STANDARD_DEFAULT_TYPE, "workflow": workflow, "resolutions": resolutions}]
        for key in self._type_keys.values():
            if key not in type_keys:
                type_keys.add(key)
                config.append({"issueType": key, "workflow": workflow, "resolutions": resolutions})
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
                # new issues start in the first column of the board
                "initialAction": {"name": {"ru": "Не задано", "en": "Undefined"}, "target": steps[0]["status"]},
                "steps": steps,
            }
        ]

    def _prepare_local_fields(self) -> List[Dict[str, Any]]:
        result = []
        for prop in self._custom_properties:
            if prop["id"] not in self._local_fields:
                continue
            key, name = self._local_fields[prop["id"]]
            result.append({
                "category": CATEGORY_NAME["ru"],
                "key": key,
                "name": {"en": name, "ru": name},
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


# access level of a space role: comment, edit, administer; any other role reads
ACCESS_LEVEL_BY_ROLE = {"reader": 1, "writer": 2, "root_writer": 2, "admin": 3}
READ_LEVEL = 1
# Tracker group of everyone in the organization: a space open to everyone gives its role to it
EVERYONE_GROUP = "Все сотрудники"
WRITE_LEVEL = 2
ADMIN_LEVEL = 3


def sees_related_cards_only(user: Dict[str, Any]) -> bool:
    """A space member who sees only cards where they are the owner or a member."""
    card_permissions = ((user.get("role_permissions") or {}).get("space") or {}).get("card") or {}
    return user.get("access_mod") == "related_only" or bool(card_permissions.get("read_own") and not card_permissions.get("read"))


def board_access(
    spaces: List[Dict[str, Any]],
    users_by_space: Dict[Any, List[Dict[str, Any]]],
    role_names: Dict[str, str],
    prepare_user: Callable[[Dict[str, Any]], Optional[str]],
) -> Dict[Tuple[str, str], int]:
    """The highest access level of every user and of everyone over the spaces of a board:
    `{("users", login): level, ("groups", EVERYONE_GROUP): level}`. A user who sees only related cards reads the
    project (`("project_readers", login)`) and, as an editor, creates issues (`("creators", login)`): their own
    issues are reached by the queue permissions of author, assignee and followers, which a new queue has."""
    access: Dict[Tuple[str, str], int] = {}

    def grant(subject: Tuple[str, str], role_id: Any, max_level: int = ADMIN_LEVEL) -> None:
        name = role_names.get(role_id)
        level = min(ACCESS_LEVEL_BY_ROLE.get(name, READ_LEVEL), max_level)
        if level > access.get(subject, 0):
            access[subject] = level

    for space in spaces:
        if space.get("access") == "for_everyone" and space.get("for_everyone_access_role_id"):
            grant(("groups", EVERYONE_GROUP), space["for_everyone_access_role_id"])
        for user in users_by_space.get(space.get("id")) or []:
            # a deactivated or deleted user has no access
            if user.get("temporarily_inactive") or user.get("delete_requested_at"):
                continue
            role_ids = (user.get("role_ids") or []) + (user.get("groups_role_ids") or [])
            login = prepare_user(user)
            if sees_related_cards_only(user):
                if login:
                    for role_id in role_ids:
                        grant(("project_readers", login), role_id, max_level=READ_LEVEL)
                        grant(("creators", login), role_id, max_level=WRITE_LEVEL)
                continue
            if login:
                for role_id in role_ids:
                    grant(("users", login), role_id)
    return access


def _subjects(access: Dict[Tuple[str, str], int], level: int, kinds: Dict[str, str]) -> Dict[str, List[str]]:
    """Subjects with at least the level; `kinds` maps a kind of `board_access` to a kind of permissions."""
    subjects: Dict[str, List[str]] = {}
    for (kind, name), subject_level in sorted(access.items()):
        if subject_level >= level and kind in kinds and name not in subjects.get(kinds[kind], []):
            subjects.setdefault(kinds[kind], []).append(name)
    return subjects


SUBJECTS = {"users": "users", "groups": "groups"}


def queue_permissions(access: Dict[Tuple[str, str], int]) -> Dict[str, Dict[str, List[str]]]:
    return {
        "read": _subjects(access, READ_LEVEL, SUBJECTS),
        "create": _subjects(access, WRITE_LEVEL, {**SUBJECTS, "creators": "users"}),
        "write": _subjects(access, WRITE_LEVEL, SUBJECTS),
        "grant": _subjects(access, ADMIN_LEVEL, SUBJECTS),
    }


def project_permissions(access: Dict[Tuple[str, str], int]) -> Dict[str, Any]:
    return {"acl": {
        "READ": _subjects(access, READ_LEVEL, {**SUBJECTS, "project_readers": "users"}),
        "WRITE": _subjects(access, WRITE_LEVEL, SUBJECTS),
        "GRANT": _subjects(access, ADMIN_LEVEL, SUBJECTS),
    }}


def stored_files(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Files stored in Kaiten."""
    return [file for file in card.get("files") or [] if not file.get("external")]


def linked_files(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Files linked from external storages (`external`): their urls lead outside Kaiten."""
    return [file for file in card.get("files") or [] if file.get("external")]


def markdown_link(name: Optional[str], url: Optional[str]) -> str:
    label = re.sub(r"([\\\[\]])", r"\\\1", name or url or "")
    if not url:
        return label
    return f"[{label}]({quote(url, safe=URL_SAFE_CHARS)})"


def active_blockers(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [blocker for blocker in card.get("blockers") or [] if not blocker.get("released")]


def is_archived(card: Dict[str, Any]) -> bool:
    """Kaiten card conditions: 1 — on the board, 2 — archived."""
    return bool(card.get("archived")) or card.get("condition") == 2


def has_valid_dates(sprint: Dict[str, Any]) -> bool:
    """Both dates are set and recognized, the start is not later than the end: otherwise a sprint or an
    iteration is not exported."""
    start, end = sprint.get("start_date"), sprint.get("finish_date")
    if not start or not end:
        logger.warning(f"Sprint or iteration {_name(sprint)} has no start or end date, it is not exported")
        return False
    start_at, end_at = _timestamp(start), _timestamp(end)
    if not start_at or not end_at:
        logger.warning(f"Sprint or iteration {_name(sprint)} has an unrecognized date, it is not exported")
        return False
    if start_at > end_at:
        logger.warning(f"Sprint or iteration {_name(sprint)} starts after its end, it is not exported")
        return False
    return True


def _name(sprint: Dict[str, Any]) -> str:
    return sprint.get("title") or f"Sprint {sprint.get('id')}"


class BoardDumper:
    """Kaiten board -> board of `boards.yaml` with its sprints; the add rule is the board's queue without the
    archive tag.

    Sprints of a board are its Kaiten sprints and the iterations of its spaces that hold its cards that are not
    archived. An issue is in one sprint of its board: an active iteration, else a planned one, else a sprint,
    else the latest closed iteration. A sprint or an iteration without valid dates is not exported; an issue none
    of whose sprints is exported gets the sprint name as a tag, as does an archived card. One sprint of a board is
    in progress."""

    _ITERATION_STATUSES = {"planned": "draft", "active": "in_progress", "closed": "released"}

    def prepare(
        self,
        board: Dict[str, Any],
        queue_key: str,
        columns: List[Dict[str, Any]],
        sprints: List[Dict[str, Any]],
        created_by: Optional[str],
        estimate_by_story_points: bool,
    ) -> Dict[str, Any]:
        """`sprints` are sprints of `boards.yaml` (see `plan_sprints`)."""
        return {
            "id": str(board.get("id")),
            "name": board.get("title"),
            "createdBy": created_by,
            "sprintsAvailable": bool(sprints),
            "estimateBy": "storyPoints" if estimate_by_story_points else None,
            "columns": columns,
            "autoFilters": {
                "addFilter": {"enabled": True, "fieldValues": {
                    "queue": [{"fixed": queue_key, "invert": False}],
                    "tags": [{"fixed": ARCHIVE_TAG, "invert": True}],
                }},
            },
            "sprints": sprints,
        }

    # sprint candidates of a card, in order of priority
    _ACTIVE_ITERATION, _PLANNED_ITERATION, _SPRINT, _CLOSED_ITERATION = range(4)

    def plan_sprints(
        self,
        board_id: Any,
        sprints: List[Dict[str, Any]],
        iterations: List[Dict[str, Any]],
        cards: List[Dict[str, Any]],
        resolve_sprint: Callable[[Any], Optional[Dict[str, Any]]] = lambda sprint_id: None,
    ) -> Tuple[List[Dict[str, Any]], Dict[Any, str], Dict[Any, str]]:
        """Sprints of the board, the sprint of each card and the tag of each card none of whose sprints is exported.
        An archived card is in no sprint: its sprint name is a tag."""
        card_ids = {card["id"] for card in cards}
        archived = {card["id"] for card in cards if is_archived(card)}
        exported: List[Dict[str, Any]] = []
        # sprint candidates of a card: (rank, finish time, sprint id in boards.yaml or None, name)
        candidates: Dict[Any, List[Tuple[int, float, Optional[str], str]]] = defaultdict(list)

        sprint_by_id = {}
        for sprint in sprints:
            sprint_id = str(sprint.get("id")) if has_valid_dates(sprint) else None
            if sprint_id:
                exported.append(self._prepare_sprint(sprint))
            sprint_by_id[sprint.get("id")] = (sprint_id, _name(sprint))
        for card in cards:
            if card.get("sprint_id") is None:
                continue
            if card["sprint_id"] in sprint_by_id:
                sprint_id, name = sprint_by_id[card["sprint_id"]]
            else:
                # a sprint of another board is not exported for this board
                other = resolve_sprint(card["sprint_id"])
                if not other or not other.get("title"):
                    logger.warning(f"Sprint {card['sprint_id']} of card {card['id']} is not found, it is not exported")
                    continue
                sprint_id, name = None, other["title"]
            candidates[card["id"]].append((self._SPRINT, 0.0, sprint_id, name))

        for iteration in iterations:
            members = [card_id for card_id in self._iteration_cards(iteration) if card_id in card_ids]
            if not members:
                continue
            rank = {"active": self._ACTIVE_ITERATION, "planned": self._PLANNED_ITERATION}.get(
                iteration.get("status"), self._CLOSED_ITERATION
            )
            finished = _timestamp(iteration.get("actual_finish_date"))
            # an iteration of archived cards only is not a sprint of the board
            sprint_id = None
            if set(members) - archived and has_valid_dates(iteration):
                sprint_id = f"{iteration['id']}:{board_id}"
                exported.append(self._prepare_iteration(iteration, sprint_id))
            for card_id in members:
                candidates[card_id].append((rank, finished, sprint_id, _name(iteration)))

        self._keep_one_sprint_in_progress(exported)

        sprint_by_card, tag_by_card = {}, {}
        for card_id, card_candidates in candidates.items():
            if card_id in archived:
                # an archived card gets only a tag: the sprint of the highest priority
                tag_by_card[card_id] = min(card_candidates, key=lambda candidate: (candidate[0], -candidate[1]))[3]
                continue
            # the highest priority; among closed iterations the latest. A sprint that is not exported counts only
            # when no sprint of the card is exported
            best = min(card_candidates, key=lambda candidate: (candidate[2] is None, candidate[0], -candidate[1]))
            _, _, sprint_id, name = best
            if sprint_id:
                sprint_by_card[card_id] = sprint_id
            else:
                logger.warning(f"Card {card_id} is in sprint {name} that is not exported, the sprint is a tag")
                tag_by_card[card_id] = name
        return exported, sprint_by_card, tag_by_card

    @staticmethod
    def _keep_one_sprint_in_progress(sprints: List[Dict[str, Any]]) -> None:
        """A Tracker board has one sprint in progress: the sprint that started last; the others are drafts."""
        in_progress = [sprint for sprint in sprints if sprint["status"] == "in_progress"]
        if len(in_progress) < 2:
            return
        current = max(in_progress, key=lambda sprint: _timestamp(sprint["startDateTime"]))
        for sprint in in_progress:
            if sprint is not current:
                logger.warning(f"Sprint {sprint['name']} is in progress with sprint {current['name']}, it is exported as a draft")
                sprint["status"] = "draft"

    @staticmethod
    def _iteration_cards(iteration: Dict[str, Any]) -> List[Any]:
        """Cards of an iteration. Closing removes unfinished cards: a card removed at closing or later was in the
        closed iteration."""
        closed_at = _timestamp(iteration.get("actual_finish_date"))
        # without a recognized closing time only cards that were not removed count
        closed = iteration.get("status") == "closed" and closed_at > 0
        return [
            record.get("card_id")
            for record in iteration.get("card_records") or []
            if not record.get("removed_at") or (closed and _timestamp(record["removed_at"]) >= closed_at)
        ]

    @staticmethod
    def _prepare_sprint(sprint: Dict[str, Any]) -> Dict[str, Any]:
        if sprint.get("archived"):
            status = "archived"
        elif sprint.get("active"):
            status = "in_progress"
        elif sprint.get("actual_finish_date"):
            status = "released"
        else:
            status = "draft"
        return {
            "id": str(sprint.get("id")),
            "name": _name(sprint),
            "status": status,
            "startDateTime": to_tracker_datetime(sprint.get("start_date")),
            "endDateTime": to_tracker_datetime(sprint.get("finish_date")),
        }

    def _prepare_iteration(self, iteration: Dict[str, Any], sprint_id: str) -> Dict[str, Any]:
        return {
            "id": sprint_id,
            "name": _name(iteration),
            "status": self._ITERATION_STATUSES.get(iteration.get("status"), "draft"),
            "startDateTime": to_tracker_datetime(iteration.get("start_date")),
            "endDateTime": to_tracker_datetime(iteration.get("finish_date")),
        }


def _timestamp(value: Any) -> float:
    date = to_tracker_datetime(value)
    if not date:
        return 0.0
    try:
        return datetime.strptime(date, TRACKER_DATETIME_FORMAT).timestamp()
    except ValueError:
        logger.warning(f"Unrecognized date {value} is skipped")
        return 0.0


class CardDumper(BaseDumper):

    _STANDARD_TYPE = "task"
    _STANDARD_PRIORITY = "normal"
    # priority of a Kaiten ASAP card
    _ASAP_PRIORITY = "critical"
    # length limit of a checklist item text in Tracker
    _CHECKLIST_ITEM_LIMIT = 2000
    # Kaiten card states: 1 — queued, 2 — in progress, 3 — done
    _DONE_STATE = 3

    def __init__(
        self,
        client: SimpleKaitenClient,
        fetcher: KaitenDataFetcher,
        custom_properties: List[Dict[str, Any]],
        card_types: List[Dict[str, Any]],
    ):
        self._client = client
        self._fetcher = fetcher
        self._properties_by_id = {prop["id"]: prop for prop in custom_properties}
        self._type_keys = issue_type_keys(card_types)
        self._local_fields = local_fields_by_property_id(custom_properties)
        # (issue key, file name, error) of files that are not downloaded
        self.failed_files: List[Tuple[str, str, str]] = []

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump(
        self,
        card: Dict[str, Any],
        dump_path: str,
        issue_key: str,
        key_by_card_id: Optional[Dict[Any, str]] = None,
        created_at_by_card_id: Optional[Dict[Any, Any]] = None,
        related_card_ids: Iterable[Any] = (),
        status_by_column_id: Optional[Dict[Any, str]] = None,
        lane_field_active: bool = False,
        sprint: Optional[str] = None,
        sprint_tag: Optional[str] = None,
    ) -> None:
        created_at_by_card_id = created_at_by_card_id or {}
        created_by = self._prepare_user(card.get("owner") or self._fetcher.resolve_user(card.get("owner_id")))
        updated_by = self._prepare_user(self._fetcher.resolve_user(card.get("updater_id"))) or created_by

        prepared_data = {
            "key": issue_key,
            "summary": card.get("title"),
            "createdAt": to_tracker_datetime(card.get("created")),
            "createdBy": created_by,
            "updatedAt": self._prepare_updated_at(
                card, [created_at_by_card_id.get(card_id) for card_id in related_card_ids]
            ),
            "updatedBy": updated_by,
            **self._prepare_resolution(card, updated_by),
            "type": self._type_keys.get(card.get("type_id"), self._STANDARD_TYPE),
            "priority": self._ASAP_PRIORITY if card.get("asap") else self._STANDARD_PRIORITY,
            "status": self._prepare_status(card, status_by_column_id or {}),
            "description": self._prepare_description(card),
            **self._prepare_dates(card),
            "tags": self._prepare_tags(card, sprint_tag),
            "assignee": self._prepare_assignee(card),
            "followers": self._prepare_members(card),
            "checklistItems": self._prepare_checklists(card.get("checklists", [])),
            "comments": sorted(
                self._prepare_comments(card) + self._prepare_blocker_comments(card, created_by),
                key=lambda comment: comment.get("createdAt") or "",
            ),
            "attachments": self._prepare_attachments(stored_files(card)),
            "worklogs": self._prepare_worklogs(card.get("time_logs", [])),
            "storyPoints": card.get("size"),
            "links": self._prepare_links(card, key_by_card_id or {}, created_at_by_card_id, created_by),
            "project": {"primary": card.get("board_id")},
            "boards": [str(card["board_id"])] if card.get("board_id") is not None and not is_archived(card) else [],
            "sprint": [sprint] if sprint else [],
            **self._prepare_local_fields(card),
        }

        if lane_field_active:
            lane_title = (card.get("lane") or {}).get("title")
            if lane_title:
                prepared_data[LANE_FIELD_KEY] = lane_title

        self._download_attachments(stored_files(card), dump_path, issue_key)
        self._clean_and_save_data(prepared_data, dump_path)

    def _prepare_status(self, card: Dict[str, Any], status_by_column_id: Dict[Any, str]) -> Optional[str]:
        # ``column`` of a card in a subcolumn is the subcolumn
        column_id = (card.get("column") or {}).get("id", card.get("column_id"))
        status = status_by_column_id.get(column_id)
        if status is None:
            logger.warning(
                f"Card {card.get('id')} is in column {column_id} missing from its board, the status is not exported"
            )
        return status

    def _prepare_resolution(self, card: Dict[str, Any], resolved_by: Optional[str]) -> Dict[str, Any]:
        """A done card (Kaiten state 3) is resolved at its last move to a done column, else at its completion or
        last update, not earlier than its creation; the resolver is the last editor. Without the resolver or a date
        the card is not resolved: Tracker requires the resolution, its time and author together."""
        if card.get("state") != self._DONE_STATE:
            return {}
        if not resolved_by:
            logger.warning(f"Card {card.get('id')} is done, but its editor and author are unknown: it is not resolved")
            return {}
        resolved = card.get("last_moved_to_done_at") or card.get("completed_at") or card.get("updated")
        resolved_at = latest_tracker_datetime([resolved, card.get("created")])
        if not resolved_at:
            logger.warning(f"Card {card.get('id')} is done, but has no dates: it is not resolved")
            return {}
        return {"resolution": RESOLUTION_KEY, "resolvedAt": resolved_at, "resolvedBy": resolved_by}

    def _prepare_updated_at(self, card: Dict[str, Any], related_created_at: List[Any]) -> Optional[str]:
        """The latest date of the card, including its completion, and of its comments, files, time logs, active
        blockers and related cards."""
        dates = [
            card.get("created"), card.get("updated"), card.get("last_moved_to_done_at"), card.get("completed_at"),
            *related_created_at,
        ]
        for comment in card.get("comments") or []:
            dates += [comment.get("created"), comment.get("updated")]
        dates += [file.get("created") for file in card.get("files") or []]
        dates += [log.get("created") for log in card.get("time_logs") or []]
        dates += [blocker.get("created") for blocker in active_blockers(card)]
        return latest_tracker_datetime(dates)

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

    def _prepare_tags(self, card: Dict[str, Any], sprint_tag: Optional[str]) -> List[str]:
        tags = [tag.get("name") for tag in card.get("tags", []) if tag.get("name")]
        if sprint_tag:
            tags.append(sprint_tag)
        if card.get("blocked") or active_blockers(card):
            tags.append(BLOCKED_TAG)
        if is_archived(card):
            tags.append(ARCHIVE_TAG)
        elif ARCHIVE_TAG in tags:
            logger.warning(f"Card {card.get('id')} is not archived, but has the tag {ARCHIVE_TAG}: the board add rule skips it")
        return list(dict.fromkeys(tags))

    def _prepare_dates(self, card: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """Deadline and planned dates as days in the company timeline time zone. A deadline without time
        and a planned date at 00:00 UTC keep their day as written."""
        timezone = self._fetcher.timeline_timezone
        due_date = card.get("due_date")
        return {
            "deadline": to_local_date(due_date, timezone) if card.get("due_date_time_present") else to_tracker_date(due_date),
            "start": to_local_date(card.get("planned_start"), timezone, midnight_utc_is_date=True),
            "end": to_local_date(card.get("planned_end"), timezone, midnight_utc_is_date=True),
        }

    @staticmethod
    def _rewrite_file_links(text: str, card: Dict[str, Any]) -> str:
        """Links to files of the card in the local ``[filename]()`` form the importer resolves against the
        attachments of the issue."""
        for file in stored_files(card):
            url = file.get("url")
            if not url:
                continue
            local_name = prepare_attachment_filename(file.get("name", "file"), str(file.get("id")))
            escaped = re.escape(url)
            # the url is followed by a query, a fragment or a title only: a longer url is another file
            tail = r"(?:[?#\s][^)]*)?"
            # a function keeps backslashes of the name literal
            text = re.sub(rf"!\[[^\]]*\]\({escaped}{tail}\)", lambda _, name=local_name: f"![{name}]()", text)
            text = re.sub(rf"(?<!!)\[[^\]]*\]\({escaped}{tail}\)", lambda _, name=local_name: f"[{name}]()", text)
        return text

    def _prepare_description(self, card: Dict[str, Any]) -> Optional[str]:
        description = self._rewrite_file_links(card.get("description") or "", card)
        links = [f"- {markdown_link(file.get('name'), file.get('url'))}" for file in linked_files(card)]
        if links:
            description = "\n\n".join(filter(None, [description, EXTERNAL_FILES_HEADER + "\n" + "\n".join(links)]))
        return description or None

    def _prepare_checklists(self, checklists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Tracker has one checklist per issue: with several checklists an item starts with its checklist name
        named = len([checklist for checklist in checklists if checklist.get("items")]) > 1
        result = []
        for checklist in sorted(checklists, key=lambda c: c.get("sort_order", 0)):
            prefix = f"{checklist['name']}: " if named and checklist.get("name") else ""
            for item in sorted(checklist.get("items", []), key=lambda i: i.get("sort_order", 0)):
                checked = item.get("checked")
                if checked is None:
                    checked = item.get("completed")
                text = item.get("text") or item.get("name")
                if text and prefix and len(prefix + text) <= self._CHECKLIST_ITEM_LIMIT:
                    text = prefix + text
                result.append({
                    "text": text,
                    "checked": bool(checked),
                    "assignee": self._prepare_user(item.get("responsible")),
                    "deadline": to_tracker_datetime(item.get("due_date")),
                })
        return result

    def _prepare_comments(self, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = []
        for comment in card.get("comments") or []:
            data = {
                # a file of a comment is a file of the card linked from the comment text
                "text": self._rewrite_file_links(comment.get("text") or "", card) or comment.get("text"),
                "createdBy": self._prepare_user(comment.get("author")),
                "createdAt": to_tracker_datetime(comment.get("created")),
            }
            updated_at = comment.get("updated")
            if updated_at and updated_at != comment.get("created"):
                data["updatedBy"] = self._prepare_user(comment.get("author"))
                data["updatedAt"] = to_tracker_datetime(updated_at)
            result.append(data)
        return result

    def _prepare_blocker_comments(self, card: Dict[str, Any], card_author: Optional[str]) -> List[Dict[str, Any]]:
        # a blocker of an unknown user is commented by the card author: a comment without an author is not imported
        return [
            {
                "text": BLOCKER_COMMENT_PREFIX + blocker["reason"],
                "createdBy": self._prepare_user(blocker.get("blocker"))
                or self._prepare_user(self._fetcher.resolve_user(blocker.get("blocker_id")))
                or card_author,
                "createdAt": to_tracker_datetime(blocker.get("created") or card.get("created")),
            }
            for blocker in active_blockers(card)
            if blocker.get("reason")
        ]

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

    @staticmethod
    def link_targets(card: Dict[str, Any]) -> List[Tuple[Any, str]]:
        """Parent/child relations and active blockers of a card, one direction only: the importer
        creates the reciprocal link."""
        targets = [(child_id, "is parent task for") for child_id in card.get("children_ids") or []]
        targets += [(blocker.get("blocker_card_id"), "depends on") for blocker in active_blockers(card)]
        return targets

    def _prepare_links(
        self,
        card: Dict[str, Any],
        key_by_card_id: Dict[Any, str],
        created_at_by_card_id: Dict[Any, Any],
        created_by: Optional[str],
    ) -> List[Dict[str, Any]]:
        # relations to cards outside the export are skipped
        links = []
        for card_id, relationship in self.link_targets(card):
            key = key_by_card_id.get(card_id)
            if key:
                links.append({
                    "issue": key,
                    "relationship": relationship,
                    # Kaiten keeps no relation date: a link is dated by the later of its cards
                    "createdAt": latest_tracker_datetime([card.get("created"), created_at_by_card_id.get(card_id)]),
                    "createdBy": created_by,
                })
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
            if prop_id not in self._local_fields:
                continue
            key, _ = self._local_fields[prop_id]
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

    def _download_attachments(self, files: List[Dict[str, Any]], card_path: str, issue_key: str) -> None:
        if not files:
            return

        attachments_path = os.path.join(card_path, "attachments")
        os.makedirs(attachments_path, exist_ok=True)

        for file in files:
            filename = prepare_attachment_filename(file.get("name", "file"), str(file.get("id")))
            url = file.get("url")
            if not url:
                self.failed_files.append((issue_key, filename, "no file url"))
                continue
            file_path = os.path.join(attachments_path, filename)
            size = file.get("size")
            # a file of a previous export is kept when its size matches the size in Kaiten and removed
            # when it differs; without the size in Kaiten it stays until a new download replaces it
            if size is not None and os.path.isfile(file_path):
                if os.path.getsize(file_path) == size:
                    continue
                os.remove(file_path)
            try:
                self._client.download_file(url, file_path, expected_size=size)
            except Exception as exc:
                logger.exception(f"Failed to download {filename} of {issue_key}")
                self.failed_files.append((issue_key, filename, str(exc)))
