import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .emoji import EMOJI_URL_PREFIX, emoji_text, replace_markdown_emoji
from .fetcher import WeeekDataFetcher
from .html_to_markdown import html_to_markdown
from .utils import (
    NAME_PART_LIMIT,
    TRACKER_DATETIME_FORMAT,
    KeyAllocator,
    NameAllocator,
    dump_to_yaml,
    encode_url,
    latest_tracker_datetime,
    local_midnight,
    prepare_attachment_filename,
    to_local_date,
    to_tracker_datetime,
    truncate_bytes,
)
from .weeek_client import ExpiredLinkError, SimpleWeeekClient

logger = logging.getLogger(__name__)

# system field category of Tracker; the importer finds it by its Russian name
FIELD_CATEGORY = "Системные"

# resolution of completed tasks
RESOLUTION_KEY = "fixed"
RESOLUTION_NAME = {"en": "Fixed", "ru": "Решён"}

# status of a task outside the board columns and the initial status of a queue without boards
OPEN_STATUS_KEY = "open"
OPEN_STATUS_NAME = "Открыт"
# final step of every workflow: completed tasks outside the board columns
RESOLVED_STATUS_KEY = "resolved"
RESOLVED_STATUS_NAME = "Решён"

DEFAULT_TYPE = "task"
# WEEEK task type -> issue type key and name; an action is the default type
ISSUE_TYPES = {
    "meet": ("meeting", {"en": "Meeting", "ru": "Встреча"}),
    "call": ("call", {"en": "Call", "ru": "Звонок"}),
}

DEFAULT_PRIORITY = "normal"
# WEEEK priorities: 0 — low, 1 — medium, 2 — high, 3 — hold
PRIORITIES = {0: "minor", 1: "normal", 2: "critical"}
HOLD_PRIORITY = 3
HOLD_TAG = "Hold"

# a file of a comment is uploaded by its author before the comment is sent
COMMENT_FILE_WINDOW_SECONDS = 60
# heading of the description list of files linked from external storages
EXTERNAL_FILES_HEADER = "Файлы по ссылкам:"
# first line of a comment or a time entry of a user who is not a member of the workspace
REMOVED_AUTHOR_NOTE = "Автор в WEEEK: исключённый участник"
OVERTIME_NOTE = "Сверхурочно"
CHECKBOX_VALUE = "Да"

# component of private tasks: a private WEEEK task is available to its author and assignees only; assignees after
# the first one get access through the issue field «Доступ»
PRIVATE_COMPONENT_NAME = "Приватные задачи"
PRIVATE_COMPONENT_DESCRIPTION = "Приватные задачи WEEEK: доступны автору и исполнителям"
PRIVATE_COMPONENT_ROLES = ["author", "assignee", "access"]

# id of a WEEEK file in its url, e.g. https://api.weeek.net/ws/<workspace>/files/<id>
_FILE_URL = re.compile(r"/ws/\d+/files/([0-9A-Fa-f-]+)")
# a markdown link or a bare url of a WEEEK file in a text
_FILE_REFERENCE = re.compile(
    r"!?\[[^\]]*\]\(https?://[^\s()]*/ws/\d+/files/([0-9A-Fa-f-]+)[^\s()]*\)"
    r"|(?<![(\w])https?://[^\s()<>\[\]]*/ws/\d+/files/([0-9A-Fa-f-]+)[^\s()<>\[\]]*"
)


class UserResolver:
    """E-mails of WEEEK users: members of the workspace and users of `configs/users.cfg` (``<id>:<e-mail>``).
    A user of neither is replaced by the token owner and recorded in `unknown_users`."""

    def __init__(self, fetcher: WeeekDataFetcher, users_mapping: Dict[str, str]):
        self._fetcher = fetcher
        self._users_mapping = users_mapping
        self.unknown_users: Set[str] = set()

    @property
    def owner(self) -> Optional[str]:
        return (self._fetcher.current_user or {}).get("email")

    def email(self, user_id: Any) -> Optional[str]:
        """E-mail of a known user; None for an unknown one."""
        if not user_id:
            return None
        if user_id in self._users_mapping:
            return self._users_mapping[user_id]
        member = self._fetcher.resolve_member(user_id)
        if member and member.get("email"):
            return member["email"]
        self.unknown_users.add(user_id)
        return None

    def member_email(self, user_id: Any) -> Optional[str]:
        """E-mail of a known user; an unknown id is not recorded."""
        if user_id in self._users_mapping:
            return self._users_mapping[user_id]
        return (self._fetcher.resolve_member(user_id) or {}).get("email")

    def project_users(self, project: Dict[str, Any]) -> Optional[List[str]]:
        """Users with access to a private project: the token owner and the known users of its team; None for a
        project open to the workspace."""
        if not project.get("isPrivate"):
            return None
        emails = [self.owner] + [self.member_email(user_id) for user_id in project.get("team") or []]
        return [email for email in dict.fromkeys(emails) if email]

    def author(self, user_id: Any) -> Tuple[Optional[str], bool]:
        """E-mail of an author and whether the author is known; an unknown author is the token owner. An empty
        author is the token owner without the note of an unknown author."""
        if not user_id:
            return self.owner, True
        email = self.email(user_id)
        return (email, True) if email else (self.owner, False)


class BaseDumper(ABC):

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass

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


def entity_dir(name: Optional[str], entity_id: Any, fallback: str) -> str:
    """Directory of a project or a portfolio: a name is not unique and may contain a path separator."""
    return re.sub(r"[\\/]", "_", f"{truncate_bytes(name or fallback, NAME_PART_LIMIT)}_{entity_id}")


def entity_id(workspace_id: Any, kind: str, weeek_id: Any) -> str:
    """Id of a project, a portfolio or a board in the export. The importer finds an imported object by its id: the
    prefix keeps it apart from objects of other workspaces and other exports."""
    return f"weeek-{workspace_id}-{kind}-{weeek_id}"


class ProjectDumper(BaseDumper):
    """WEEEK project -> Tracker project. The WEEEK API gives no author and creation time of a project: the author is the
    token owner, the time is the creation of its earliest task."""

    def __init__(self, workspace_id: Any):
        self._workspace_id = workspace_id

    @property
    def _filename(self) -> str:
        return "project.yaml"

    def dump(
        self, project: Dict[str, Any], dump_path: str, created_at: str, created_by: Optional[str],
        users: Optional[List[str]] = None, portfolio_id: Any = None,
    ) -> None:
        """`users` are the only users with access to a private project; `portfolio_id` is its exported portfolio."""
        prepared_data = {
            "id": entity_id(self._workspace_id, "project", project.get("id")),
            "summary": project.get("name") or project.get("title"),
            "createdAt": created_at,
            "createdBy": created_by,
            "teamAccess": True,
            "description": project.get("description"),
            "parentEntity": {
                "primary": entity_id(self._workspace_id, "portfolio", portfolio_id),
            } if portfolio_id is not None else None,
            "permissions": {"acl": {"READ": {"users": users}, "WRITE": {"users": users}}} if users else None,
        }
        self._clean_and_save_data(prepared_data, dump_path)


class PortfolioDumper(BaseDumper):
    """WEEEK portfolio -> Tracker portfolio, nested as in WEEEK. The author is the token owner, the creation time is
    the earliest of its projects."""

    def __init__(self, workspace_id: Any):
        self._workspace_id = workspace_id

    @property
    def _filename(self) -> str:
        return "portfolio.yaml"

    def dump(
        self, portfolio: Dict[str, Any], dump_path: str, created_at: str, created_by: Optional[str], parent_id: Any = None,
    ) -> None:
        """`parent_id` is the exported parent portfolio. The WEEEK API does not return the users of a portfolio: a
        private portfolio is accessible only to its author, the token owner."""
        prepared_data = {
            "id": entity_id(self._workspace_id, "portfolio", portfolio.get("id")),
            "summary": portfolio.get("name"),
            "createdAt": created_at,
            "createdBy": created_by,
            "teamAccess": True,
            "parentEntity": {
                "primary": entity_id(self._workspace_id, "portfolio", parent_id),
            } if parent_id is not None else None,
            "permissions": {
                "acl": {"READ": {"users": [created_by]}, "WRITE": {"users": [created_by]}},
            } if portfolio.get("isPrivate") and created_by else None,
        }
        self._clean_and_save_data(prepared_data, dump_path)


@dataclass
class LocalField:
    key: str
    name: str
    definition: Dict[str, Any]


@dataclass
class QueuePlan:
    """Statuses of the board columns and local fields of the queue of a project."""
    queue_key: str
    project_id: str
    workspace_id: Any
    status_by_column_id: Dict[Any, str] = field(default_factory=dict)
    # board id -> [(column, status key)] in board order
    columns_by_board: Dict[Any, List[Tuple[Dict[str, Any], str]]] = field(default_factory=dict)
    initial_status: Optional[str] = None
    fields: Dict[str, LocalField] = field(default_factory=dict)
    has_private_tasks: bool = False


class ProjectToQueueDumper(BaseDumper):
    """WEEEK project -> Tracker queue: board columns become statuses, fields of the project and fields filled in its
    tasks become local fields. Statuses are global: one status per column name across all projects."""

    def __init__(self, workspace_id: Any):
        self._workspace_id = workspace_id
        self._collected_statuses: Dict[str, Dict[str, Any]] = {}
        self._status_keys = KeyAllocator(reserved=[RESOLVED_STATUS_KEY, OPEN_STATUS_KEY])
        # the importer matches statuses by name in both languages
        self._status_names = NameAllocator(reserved=[OPEN_STATUS_NAME, RESOLVED_STATUS_NAME, "Open", "Resolved"])

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    @property
    def collected_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Status key -> ``{"name": ..., "type": ...}``."""
        return self._collected_statuses

    def plan(
        self,
        project: Dict[str, Any],
        queue_key: str,
        boards: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
    ) -> QueuePlan:
        """Statuses of the columns and local fields of the queue. The first column of a board is an initial status,
        other columns are in progress. A queue whose tasks are not all in columns starts with the status
        «Открыт»."""
        plan = QueuePlan(queue_key=queue_key, project_id=str(project.get("id")), workspace_id=self._workspace_id)
        for board in boards:
            columns = []
            for index, column in enumerate(board.get("columns") or []):
                key = self._status_key(column, "new" if index == 0 else "inProgress")
                plan.status_by_column_id[column.get("id")] = key
                columns.append((column, key))
            plan.columns_by_board[board.get("id")] = columns
        outside_columns = any(
            not task_column(task, project.get("id")) and not is_completed(task) for task in tasks
        )
        if outside_columns or not plan.status_by_column_id:
            self._collected_statuses.setdefault(OPEN_STATUS_KEY, {"name": OPEN_STATUS_NAME, "type": "new"})
            plan.initial_status = OPEN_STATUS_KEY
        else:
            plan.initial_status = next(iter(plan.status_by_column_id.values()))
        plan.fields = self._plan_fields(tasks, project.get("customFields") or [])
        plan.has_private_tasks = any(task.get("isPrivate") for task in tasks)
        return plan

    def _status_key(self, column: Dict[str, Any], status_type: str) -> str:
        name = self._status_names.name((column.get("name") or "").strip() or f"Колонка {column.get('id')}")
        key = self._status_keys.key(name, f"column{column.get('id')}")
        status = self._collected_statuses.setdefault(key, {"name": name, "type": status_type})
        if status["type"] != status_type:
            # a status name is unique in Tracker: columns with one name share one status
            logger.warning(f"Columns {name} have different types, the status type is {status['type']}")
        return key

    @staticmethod
    def _plan_fields(tasks: List[Dict[str, Any]], project_fields: List[Dict[str, Any]]) -> Dict[str, LocalField]:
        """Fields of the project and fields filled in its tasks, in the order of their first appearance: a task
        lists every field of the workspace, filled or not. Names of local fields are unique within a queue: a taken
        name gets « (2)», « (3)»."""
        filled = {
            custom_field.get("id")
            for task in tasks for custom_field in task.get("customFields") or []
            if is_filled(custom_field.get("value"))
        }
        project_field_ids = {custom_field.get("id") for custom_field in project_fields}
        keys, names = KeyAllocator(), NameAllocator()
        result: Dict[str, LocalField] = {}
        # a field of the project without tasks has its definition in the project only
        for custom_field in [field for task in tasks for field in task.get("customFields") or []] + project_fields:
            field_id = custom_field.get("id")
            if not field_id or field_id in result or field_id not in filled | project_field_ids:
                continue
            name = names.new_name((custom_field.get("name") or "").strip() or f"Поле {field_id}")
            result[field_id] = LocalField(keys.new_key(name, f"field{len(result) + 1}"), name, custom_field)
        return result

    def dump(
        self, project: Dict[str, Any], plan: QueuePlan, dump_path: str, lead: Optional[str],
        users: Optional[List[str]] = None,
    ) -> None:
        """`users` are the only users with access to the queue of a private project: they read, create and edit
        issues."""
        prepared_data = {
            "key": plan.queue_key,
            "name": project.get("name") or project.get("title"),
            "lead": lead,
            "defaultType": DEFAULT_TYPE,
            "defaultPriority": DEFAULT_PRIORITY,
            "description": project.get("description"),
            "workflows": self._prepare_workflows(plan, project.get("name") or plan.queue_key),
            "issueTypesConfig": self._prepare_issue_types_config(plan.queue_key),
            "local_fields": [self._prepare_local_field(local_field) for local_field in plan.fields.values()],
            "components": [{
                "name": PRIVATE_COMPONENT_NAME,
                "description": PRIVATE_COMPONENT_DESCRIPTION,
                "permissions": {
                    "read": {"roles": PRIVATE_COMPONENT_ROLES}, "write": {"roles": PRIVATE_COMPONENT_ROLES},
                },
            }] if plan.has_private_tasks else None,
            "permissions": {
                "read": {"users": users}, "create": {"users": users}, "write": {"users": users},
            } if users else None,
        }
        self._clean_and_save_data(prepared_data, dump_path)

    @staticmethod
    def _workflow_key(queue_key: str) -> str:
        return f"W_{queue_key}"

    def _prepare_workflows(self, plan: QueuePlan, project_name: str) -> List[Dict[str, Any]]:
        keys = [plan.initial_status] if plan.initial_status == OPEN_STATUS_KEY else []
        for key in plan.status_by_column_id.values():
            if key not in keys:
                keys.append(key)
        steps = [
            {
                "status": key,
                "metaAction": {"name": {"en": self._collected_statuses[key]["name"], "ru": self._collected_statuses[key]["name"]}, "target": key},
            }
            for key in keys
        ] + [
            {
                "status": RESOLVED_STATUS_KEY,
                "metaAction": {"name": {"en": "Resolved", "ru": RESOLVED_STATUS_NAME}, "target": RESOLVED_STATUS_KEY},
            }
        ]
        return [
            {
                "key": self._workflow_key(plan.queue_key),
                "name": f"Рабочий процесс очереди \"{project_name}\"",
                "type": "visual",
                "initialAction": {"name": {"ru": "Не задано", "en": "Undefined"}, "target": plan.initial_status},
                "steps": steps,
            }
        ]

    def _prepare_issue_types_config(self, queue_key: str) -> List[Dict[str, Any]]:
        workflow = self._workflow_key(queue_key)
        return [
            {"issueType": issue_type, "workflow": workflow, "resolutions": [RESOLUTION_KEY]}
            for issue_type in [DEFAULT_TYPE] + [key for key, _ in ISSUE_TYPES.values()]
        ]

    def _prepare_local_field(self, local_field: LocalField) -> Dict[str, Any]:
        definition = local_field.definition
        field_type = definition.get("type")
        options = list(dict.fromkeys(option.get("name") for option in definition.get("options") or [] if option.get("name")))
        schema, options_provider = {"type": "string"}, None
        if field_type == "number":
            schema = {"type": "float"}
        elif field_type == "datetime":
            schema = {"type": "datetime"}
        elif field_type == "link":
            schema = {"type": "uri"}
        elif field_type in ("member", "approval"):
            schema = {"type": "array", "items": "user"}
        elif field_type == "select":
            options_provider = {"type": "FixedListOptionsProvider", "values": options} if options else None
        elif field_type == "multiselect":
            schema = {"type": "array", "items": "string"}
            options_provider = {"type": "FixedListOptionsProvider", "values": options} if options else None
        elif field_type == "boolean":
            options_provider = {"type": "FixedListOptionsProvider", "values": [CHECKBOX_VALUE]}
        elif field_type == "text":
            schema = {"type": "text"}
        elif field_type != "contact":
            logger.warning(f"Unknown custom field type {field_type} of field {local_field.name}, it is exported as a string")
        return {
            "category": FIELD_CATEGORY,
            "key": local_field.key,
            "name": {"en": local_field.name, "ru": local_field.name},
            "schema": schema,
            "optionsProvider": options_provider,
        }


class BoardDumper:
    """WEEEK board -> board of `boards.yaml`; its columns hold the statuses of the WEEEK columns. The only board of a
    project gets the add rule by its queue."""

    @staticmethod
    def prepare(board: Dict[str, Any], plan: QueuePlan, single_board: bool) -> Dict[str, Any]:
        columns, used = [], set()
        for column, status in plan.columns_by_board.get(board.get("id"), []):
            # a status is in one column of a board: columns with one name share the first column
            if status in used:
                continue
            used.add(status)
            columns.append({"id": str(column.get("id")), "name": column.get("name"), "statuses": [status]})
        return {
            "id": entity_id(plan.workspace_id, "board", board.get("id")),
            "name": board.get("name"),
            "columns": columns,
            "autoFilters": {
                "addFilter": {"enabled": True, "fieldValues": {"queue": [{"fixed": plan.queue_key, "invert": False}]}},
            } if single_board else None,
        }


def is_filled(value: Any) -> bool:
    """A value of a custom field is set: an unchecked flag is false, an empty field is None, "" or []."""
    return value is not None and value is not False and value != "" and value != []


def is_completed(task: Dict[str, Any]) -> bool:
    return bool(task.get("isCompleted"))


def task_location(task: Dict[str, Any], project_id: Any) -> Dict[str, Any]:
    for location in task.get("locations") or []:
        if str(location.get("projectId")) == str(project_id):
            return location
    return {}


def task_column(task: Dict[str, Any], project_id: Any) -> Any:
    return task_location(task, project_id).get("boardColumnId")


def stored_files(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Files stored in WEEEK."""
    return [file for file in task.get("attachments") or [] if (file.get("service") or "weeek") == "weeek"]


def linked_files(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Files of external storages: their urls lead outside WEEEK."""
    return [file for file in task.get("attachments") or [] if (file.get("service") or "weeek") != "weeek"]


def file_name(file: Dict[str, Any]) -> str:
    return prepare_attachment_filename(file.get("name") or "file", str(file.get("id")))


def file_link(file: Dict[str, Any]) -> str:
    """Link of the importer to an attachment of the issue: an image is shown in the text."""
    name = file_name(file)
    return f"![{name}]()" if is_image(name) else f"[{name}]()"


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".tif", ".tiff", ".avif")


def is_image(name: str) -> bool:
    return name.lower().endswith(_IMAGE_EXTENSIONS)


def markdown_link(name: Optional[str], url: Optional[str]) -> str:
    label = re.sub(r"([\\\[\]])", r"\\\1", name or url or "")
    if not url:
        return label
    return f"[{label}]({encode_url(url)})"


def _timestamp(value: Any) -> Optional[float]:
    date = to_tracker_datetime(value)
    if not date:
        return None
    try:
        return datetime.strptime(date, TRACKER_DATETIME_FORMAT).timestamp()
    except ValueError:
        logger.warning(f"Unrecognized date {value} is skipped")
        return None


def referenced_file_ids(text: Optional[str]) -> Set[str]:
    """Ids of the files a text shows or links to."""
    return {match.lower() for match in _FILE_URL.findall(text or "")}


def rewrite_file_references(text: str, task: Dict[str, Any]) -> str:
    """Links and urls of the files of the task -> links of the importer to the attachments."""
    files = {str(file.get("id")).lower(): file for file in stored_files(task)}

    def replace(match: re.Match) -> str:
        file = files.get((match.group(1) or match.group(2)).lower())
        return file_link(file) if file else match.group(0)

    return _FILE_REFERENCE.sub(replace, text)


def comment_files(task: Dict[str, Any], comments: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    """Files of the comments by comment id. WEEEK does not link a file to its comment: a file belongs to the first
    comment of its uploader sent within a minute after the upload, unless the description or a comment refers to
    it."""
    shown = referenced_file_ids(task.get("description"))
    for comment in comments:
        shown |= referenced_file_ids(comment.get("markdown"))
    result: Dict[Any, List[Dict[str, Any]]] = {}
    ordered = sorted(comments, key=lambda comment: (_timestamp(comment.get("createdAt")) or 0, str(comment.get("id"))))
    for file in sorted(stored_files(task), key=lambda file: (_timestamp(file.get("createdAt")) or 0, str(file.get("id")))):
        if str(file.get("id")).lower() in shown:
            continue
        uploaded = _timestamp(file.get("createdAt"))
        if uploaded is None:
            continue
        for comment in ordered:
            sent = _timestamp(comment.get("createdAt"))
            if (
                comment.get("authorId") == file.get("creatorId")
                and sent is not None
                and 0 <= sent - uploaded <= COMMENT_FILE_WINDOW_SECONDS
            ):
                result.setdefault(comment.get("id"), []).append(file)
                break
    return result


@dataclass
class TaskContext:
    """Data of the export a task refers to."""
    plan: QueuePlan
    key_by_task_id: Dict[Any, str]
    created_at_by_task_id: Dict[Any, Any]
    children_by_task_id: Dict[Any, List[Any]]
    # ids of the exported projects
    exported_project_ids: Set[str]


class TaskDumper(BaseDumper):

    def __init__(self, client: SimpleWeeekClient, fetcher: WeeekDataFetcher, users: UserResolver):
        self._client = client
        self._fetcher = fetcher
        self._users = users
        # (issue key, item, error) of files and comments that are not exported
        self.failed_items: List[Tuple[str, str, str]] = []

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump(self, task: Dict[str, Any], dump_path: str, issue_key: str, context: TaskContext) -> None:
        created_by, _ = self._users.author(task.get("authorId"))
        comments = task.get("comments")
        if comments is None:
            self.failed_items.append((issue_key, "comments", "comments are not loaded"))
            comments = []
        files_by_comment = comment_files(task, comments)
        links = self._prepare_links(task, context, created_by)

        prepared_data = {
            "key": issue_key,
            "summary": task.get("title"),
            "createdAt": to_tracker_datetime(task.get("createdAt")),
            "createdBy": created_by,
            "updatedAt": self._prepare_updated_at(task, comments, links),
            # the WEEEK API gives no editor of a task
            "updatedBy": created_by,
            **self._prepare_resolution(task, created_by),
            "type": ISSUE_TYPES.get(task.get("type"), (DEFAULT_TYPE, None))[0],
            "priority": PRIORITIES.get(task.get("priority"), DEFAULT_PRIORITY),
            "status": self._prepare_status(task, context.plan),
            "description": self._prepare_description(task),
            **self._prepare_dates(task),
            "originalEstimation": task["duration"] * 60 * 1000 if task.get("duration") else None,
            "tags": self._prepare_tags(task),
            **self._prepare_people(task),
            "comments": self._prepare_comments(task, comments, files_by_comment),
            "attachments": self._prepare_attachments(stored_files(task)),
            "worklogs": self._prepare_worklogs(task.get("timeEntries") or [], task.get("createdAt")),
            "links": links,
            "project": self._prepare_project(task, context),
            "boards": self._prepare_boards(task, context.plan),
            **self._prepare_local_fields(task, context.plan),
        }

        self._download_attachments(stored_files(task), dump_path, issue_key)
        self._clean_and_save_data(prepared_data, dump_path)

    @staticmethod
    def _prepare_status(task: Dict[str, Any], plan: QueuePlan) -> Optional[str]:
        column_id = task_column(task, plan.project_id)
        if column_id is not None:
            status = plan.status_by_column_id.get(column_id)
            if status:
                return status
            logger.warning(f"Task {task.get('id')} is in column {column_id} missing from its project, it gets the initial status of the queue or, when completed, the resolved status")
        return RESOLVED_STATUS_KEY if is_completed(task) else plan.initial_status

    def _prepare_resolution(self, task: Dict[str, Any], author: Optional[str]) -> Dict[str, Any]:
        """A completed task is resolved at its completion, not earlier than its creation. The WEEEK API does not
        give who completed a task: the resolver is the first assignee, else the author."""
        if not is_completed(task):
            return {}
        assignees = [email for email in (self._users.email(user_id) for user_id in task.get("assignees") or []) if email]
        resolved_by = next(iter(assignees), None) or author
        resolved_at = latest_tracker_datetime([task.get("completedAt") or task.get("updatedAt"), task.get("createdAt")])
        if not resolved_by or not resolved_at:
            logger.warning(f"Task {task.get('id')} is completed, but has no dates or users: it is not resolved")
            return {}
        return {"resolution": RESOLUTION_KEY, "resolvedAt": resolved_at, "resolvedBy": resolved_by}

    @staticmethod
    def _prepare_updated_at(task: Dict[str, Any], comments: List[Dict[str, Any]], links: List[Dict[str, Any]]) -> Optional[str]:
        """The latest date of the task, its completion, comments, files and links: a new comment does not change the
        update time of a WEEEK task."""
        dates = [task.get("createdAt"), task.get("updatedAt"), task.get("completedAt")]
        for comment in comments:
            dates += [comment.get("createdAt"), comment.get("updatedAt")]
        dates += [file.get("createdAt") for file in stored_files(task)]
        dates += [link.get("createdAt") for link in links]
        return latest_tracker_datetime(dates)

    def _prepare_dates(self, task: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """Start and deadline as days; a date with time is a day in the time zone of the token owner."""
        timezone = self._fetcher.timezone
        return {
            "start": task.get("startDate") or to_local_date(task.get("startDateTime"), timezone),
            "deadline": task.get("dueDate") or to_local_date(task.get("dueDateTime"), timezone),
        }

    def _prepare_tags(self, task: Dict[str, Any]) -> List[str]:
        tags = []
        for tag_id in task.get("tags") or []:
            title = self._fetcher.tag_title(tag_id)
            if title:
                tags.append(title)
            else:
                logger.warning(f"Tag {tag_id} of task {task.get('id')} is not found, it is not exported")
        if task.get("priority") == HOLD_PRIORITY:
            tags.append(HOLD_TAG)
        return list(dict.fromkeys(tags))

    def _prepare_people(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """The first assignee is the assignee; other assignees and the subscribers are followers. Other assignees of a
        private task get access to it."""
        assignees = [email for email in (self._users.email(user_id) for user_id in task.get("assignees") or []) if email]
        followers = assignees[1:] + [self._users.email(user_id) for user_id in task.get("subscribers") or []]
        assignee = assignees[0] if assignees else None
        return {
            "assignee": assignee,
            "followers": [email for email in dict.fromkeys(followers) if email and email != assignee],
            "access": [email for email in dict.fromkeys(assignees[1:]) if email != assignee] if task.get("isPrivate") else [],
            "components": [PRIVATE_COMPONENT_NAME] if task.get("isPrivate") else [],
        }

    def _image(self, task: Dict[str, Any]):
        files = {str(file.get("id")).lower(): file for file in stored_files(task)}

        def render(src: str, alt: str) -> Optional[str]:
            if src.startswith(EMOJI_URL_PREFIX):
                return emoji_text(src[len(EMOJI_URL_PREFIX):], alt)
            match = _FILE_URL.search(src)
            if match and match.group(1).lower() in files:
                return f"![{file_name(files[match.group(1).lower()])}]()"
            return None

        return render

    def _prepare_description(self, task: Dict[str, Any]) -> Optional[str]:
        description = rewrite_file_references(html_to_markdown(task.get("description"), self._image(task)), task)
        links = [f"- {markdown_link(file.get('name'), file.get('url'))}" for file in linked_files(task)]
        if links:
            description = "\n\n".join(filter(None, [description, EXTERNAL_FILES_HEADER + "\n" + "\n".join(links)]))
        return description or None

    def _prepare_comments(
        self, task: Dict[str, Any], comments: List[Dict[str, Any]], files_by_comment: Dict[Any, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        result = []
        for comment in sorted(comments, key=lambda comment: (_timestamp(comment.get("createdAt")) or 0, str(comment.get("id")))):
            author, known = self._users.author(comment.get("authorId"))
            parts = [
                None if known else REMOVED_AUTHOR_NOTE,
                rewrite_file_references(replace_markdown_emoji(comment.get("markdown") or ""), task).strip(),
                "\n".join(file_link(file) for file in files_by_comment.get(comment.get("id"), [])),
            ]
            text = "\n\n".join(part for part in parts if part)
            if not text or text == REMOVED_AUTHOR_NOTE:
                logger.warning(f"Comment {comment.get('id')} has no text and files, it is not exported")
                continue
            data = {"text": text, "createdBy": author, "createdAt": to_tracker_datetime(comment.get("createdAt"))}
            if comment.get("updatedAt") and comment.get("updatedAt") != comment.get("createdAt"):
                data["updatedBy"] = author
                data["updatedAt"] = to_tracker_datetime(comment.get("updatedAt"))
            result.append(data)
        return result

    def _prepare_attachments(self, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "filename": file_name(file),
                "name": file.get("name"),
                "createdAt": to_tracker_datetime(file.get("createdAt")),
                "createdBy": self._users.author(file.get("creatorId"))[0],
            }
            for file in files
        ]

    def _prepare_worklogs(self, time_entries: List[Dict[str, Any]], task_created_at: Any) -> List[Dict[str, Any]]:
        """A time entry is dated by its day: its start is midnight in the time zone of the token owner. The WEEEK API
        gives no creation time of a time entry: it is its start, not earlier than the creation of the task."""
        result = []
        for entry in time_entries:
            author, known = self._users.author(entry.get("userId"))
            seconds = entry.get("durationSeconds") or (entry.get("duration") or 0) * 60
            notes = [None if known else REMOVED_AUTHOR_NOTE, OVERTIME_NOTE if entry.get("isOvertime") else None, entry.get("comment")]
            duration = int(float(seconds) * 1000)
            if duration <= 0:
                logger.warning(f"Time entry {entry.get('id')} has no duration, it is not exported")
                continue
            start = local_midnight(entry.get("date"), self._fetcher.timezone)
            result.append({
                "start": start,
                "duration": duration,
                "comment": "\n".join(note for note in notes if note),
                "createdAt": latest_tracker_datetime([start, task_created_at]),
                "createdBy": author,
            })
        return result

    @staticmethod
    def _prepare_links(task: Dict[str, Any], context: TaskContext, created_by: Optional[str]) -> List[Dict[str, Any]]:
        """Subtasks of the export: the importer creates the reciprocal link. The WEEEK API gives no time of a link: it is
        the later creation of the two tasks."""
        links = []
        for child_id in context.children_by_task_id.get(task.get("id"), []):
            key = context.key_by_task_id.get(child_id)
            if key:
                links.append({
                    "issue": key,
                    "relationship": "is parent task for",
                    "createdAt": latest_tracker_datetime([task.get("createdAt"), context.created_at_by_task_id.get(child_id)]),
                    "createdBy": created_by,
                })
        return links

    @staticmethod
    def _prepare_project(task: Dict[str, Any], context: TaskContext) -> Dict[str, Any]:
        secondary = []
        for location in task.get("locations") or []:
            project_id = str(location.get("projectId"))
            if project_id != context.plan.project_id and project_id in context.exported_project_ids and project_id not in secondary:
                secondary.append(project_id)
        return {
            "primary": entity_id(context.plan.workspace_id, "project", context.plan.project_id),
            "secondary": [entity_id(context.plan.workspace_id, "project", project_id) for project_id in secondary],
        }

    @staticmethod
    def _prepare_boards(task: Dict[str, Any], plan: QueuePlan) -> List[str]:
        board_id = task_location(task, plan.project_id).get("boardId")
        if board_id is None or board_id not in plan.columns_by_board:
            return []
        return [entity_id(plan.workspace_id, "board", board_id)]

    def _prepare_local_fields(self, task: Dict[str, Any], plan: QueuePlan) -> Dict[str, Any]:
        result = {}
        for custom_field in task.get("customFields") or []:
            local_field = plan.fields.get(custom_field.get("id"))
            if local_field is None:
                continue
            value = self._field_value(local_field.definition, custom_field.get("value"))
            if value not in (None, "", []):
                result[f"local_{local_field.key}"] = value
        return result

    def _field_value(self, definition: Dict[str, Any], value: Any) -> Any:
        if value is None:
            return None
        field_type = definition.get("type")
        if field_type in ("select", "multiselect"):
            names = {option.get("id"): option.get("name") for option in definition.get("options") or []}
            ids = value if isinstance(value, list) else [value]
            missing = [option_id for option_id in ids if not names.get(option_id)]
            if missing:
                logger.warning(f"Options {missing} of field {definition.get('name')} are not found, they are not exported")
            values = [names[option_id] for option_id in ids if names.get(option_id)]
            if field_type == "multiselect":
                return values
            return values[0] if values else None
        if field_type == "boolean":
            # an empty field and an unchecked one are both false
            return CHECKBOX_VALUE if value is True else None
        if field_type in ("member", "approval"):
            # an approval is a list of approvers: {id, approved}
            ids = [item.get("id") if isinstance(item, dict) else item for item in (value if isinstance(value, list) else [value])]
            return [email for email in (self._users.email(user_id) for user_id in ids) if email]
        if field_type == "contact":
            ids = value if isinstance(value, list) else [value]
            return ", ".join(self._fetcher.contact_name(contact_id) or str(contact_id) for contact_id in ids)
        if field_type == "datetime":
            if _timestamp(value) is None:
                logger.warning(f"Value {value} of field {definition.get('name')} is not a date, it is not exported")
                return None
            return to_tracker_datetime(value)
        if field_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            logger.warning(f"Value {value} of field {definition.get('name')} is not a number, it is not exported")
            return None
        if isinstance(value, (dict, list)):
            logger.warning(f"Value {value} of field {definition.get('name')} is not exported")
            return None
        return value

    def _download_attachments(self, files: List[Dict[str, Any]], task_path: str, issue_key: str) -> None:
        if not files:
            return

        attachments_path = os.path.join(task_path, "attachments")
        os.makedirs(attachments_path, exist_ok=True)

        for file in files:
            filename = file_name(file)
            file_path = os.path.join(attachments_path, filename)
            size = file.get("size")
            # a file of a previous export is kept when its size matches the size in WEEEK and removed
            # when it differs; without the size in WEEEK it stays until a new download replaces it
            if size is not None and os.path.isfile(file_path):
                if os.path.getsize(file_path) == size:
                    continue
                os.remove(file_path)
            try:
                self._download(file, file_path)
            except Exception as exc:
                # the traceback of a download error may hold the signed url
                logger.error(f"Failed to download {filename} of {issue_key}: {exc}")
                self.failed_items.append((issue_key, f"file {filename}", str(exc)))

    def _download(self, file: Dict[str, Any], file_path: str) -> None:
        """A file url lives one hour: an expired url is requested again."""
        url = file.get("url") or self._fetcher.fresh_file_url(file.get("id"))
        if not url:
            raise Exception("no file url")
        try:
            self._client.download_file(url, file_path, expected_size=file.get("size"))
        except ExpiredLinkError:
            url = self._fetcher.fresh_file_url(file.get("id"))
            if not url:
                raise Exception("no file url")
            self._client.download_file(url, file_path, expected_size=file.get("size"))
