import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

from .fetcher import YouTrackDataFetcher
from .utils import (
    KeyAllocator,
    NameAllocator,
    dump_to_yaml,
    latest_tracker_datetime,
    normalize_name,
    ms_to_date,
    ms_to_tracker_datetime,
    prepare_attachment_filename,
)
from .youtrack_client import DownloadError, SimpleYouTrackClient

logger = logging.getLogger(__name__)

# standard category of Tracker fields, referred to by its Russian name
FIELD_CATEGORY = "Системные"

DEFAULT_TYPE = "task"
# type of a queue whose project has no type field
DEFAULT_TYPE_VALUE = {"name": "Task", "localizedName": "Задача"}
DEFAULT_PRIORITY = "normal"
# status of a queue whose project has no state field
OPEN_STATE = {"name": "Open", "localizedName": "Открыт"}

# names of the system fields of a YouTrack project
STATE_FIELD = "state"
TYPE_FIELD = "type"
PRIORITY_FIELD = "priority"
ASSIGNEE_FIELD = "assignee"
SUBSYSTEM_FIELD = "subsystem"
FIX_VERSIONS_FIELD = "fix versions"
AFFECTED_VERSIONS_FIELD = "affected versions"
DUE_DATE_FIELD = "due date"
STORY_POINTS_FIELD = "story points"

# base names of YouTrack values (lower case) -> Tracker keys
TYPE_KEYS = {"bug": "bug", "task": "task", "feature": "newFeature", "epic": "epic", "user story": "story"}
PRIORITY_KEYS = {"show-stopper": "blocker", "critical": "critical", "major": "normal", "normal": "normal", "minor": "minor"}
RESOLUTION_FIXED = "fixed"
RESOLUTION_KEYS = {
    "duplicate": "duplicate",
    "won't fix": "wontFix",
    "can't reproduce": "cantReproduce",
    "obsolete": "dontDo",
    "incomplete": "dontDo",
}
RESOLUTION_NAMES = {
    "fixed": {"en": "Fixed", "ru": "Решен"},
    "duplicate": {"en": "Duplicate", "ru": "Дубликат"},
    "wontFix": {"en": "Won't fix", "ru": "Не будет исправлено"},
    "cantReproduce": {"en": "Can't reproduce", "ru": "Не воспроизводится"},
    "dontDo": {"en": "Won't do", "ru": "Не делаем"},
}

# YouTrack link type -> relationship of the importer by the direction of the link
LINK_RELATIONSHIPS = {
    ("subtask", "OUTWARD"): "is parent task for",
    ("subtask", "INWARD"): "is subtask for",
    ("depend", "OUTWARD"): "is dependent by",
    ("depend", "INWARD"): "depends on",
    ("duplicate", "OUTWARD"): "is duplicated by",
    ("duplicate", "INWARD"): "duplicates",
}
RELATES = "relates"

# permissions of YouTrack roles -> permissions of a Tracker queue
QUEUE_PERMISSIONS = {
    "read": "JetBrains.YouTrack.READ_ISSUE",
    "create": "JetBrains.YouTrack.CREATE_ISSUE",
    "write": "JetBrains.YouTrack.UPDATE_ISSUE",
}
# groups of all users of YouTrack and of Tracker
EVERYONE_GROUPS = ("AllUsersGroup", "RegisteredUsersGroup")
ALL_EMPLOYEES_GROUP = "Все сотрудники"

# component of the issues visible to a part of users in YouTrack: the author, the assignee and the users of the field
# «Доступ» read and edit them
RESTRICTED_COMPONENT_NAME = "Ограниченная видимость"
RESTRICTED_COMPONENT = {
    "description": "Задачи, видимые части пользователей",
    "permissions": {
        "read": {"roles": ["author", "assignee", "access"]},
        "write": {"roles": ["author", "assignee", "access"]},
    },
}

# first line of a comment or a work item of a user without an e-mail
UNKNOWN_AUTHOR_NOTE = "Автор: {name}"
WORK_TYPE_NOTE = "Тип работ: {name}"

# a markdown image or link whose target is a file name: ![alt](name.png "title"){width=50%}, ![alt](<a name.png>)
_FILE_REFERENCE = re.compile(r'(!?)\[([^\]]*)\]\((?:<([^<>\n]+)>|([^()<>\s]+))(?:\s+"[^"]*")?\)(\{[^{}\n]*\})?')


def base_name(item: Optional[Dict[str, Any]]) -> str:
    """Base name of a YouTrack field or value in lower case: system names are English."""
    return ((item or {}).get("name") or "").strip().lower()


def bilingual_name(value: Dict[str, Any]) -> Dict[str, str]:
    """English base name and the name in the language of the instance; a value created by a user has one name."""
    name = value.get("name") or ""
    return {"en": name, "ru": value.get("localizedName") or name}


def field_type_id(project_field: Dict[str, Any]) -> str:
    return ((project_field.get("field") or {}).get("fieldType") or {}).get("id") or ""


def is_multi(project_field: Dict[str, Any]) -> bool:
    return field_type_id(project_field).endswith("[*]")


def status_field(project_fields: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The field of a project whose values become statuses: the state field State, else the first state field."""
    states = [f for f in project_fields if field_type_id(f) == "state[1]"]
    return next((f for f in states if base_name(f.get("field")) == STATE_FIELD), next(iter(states), None))


def bundle_values(project_field: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = (project_field.get("bundle") or {}).get("values") or []
    return sorted(values, key=lambda value: (value.get("ordinal") or 0, value.get("name") or ""))


class UserResolver:
    """E-mails of YouTrack users by login: `configs/users.cfg` (``<login>:<e-mail>``), the e-mail in the data, the list
    of users. A user without an e-mail is replaced by the token owner and recorded in `unknown_users`."""

    def __init__(self, fetcher: YouTrackDataFetcher, users_mapping: Dict[str, str]):
        self._fetcher = fetcher
        self._users_mapping = users_mapping
        self.unknown_users: Set[str] = set()

    @property
    def owner(self) -> Optional[str]:
        """E-mail of the token owner."""
        return self.email(self._fetcher.current_user)

    def email(self, user: Optional[Dict[str, Any]]) -> Optional[str]:
        """E-mail of a user; None for an empty user and for a user without an e-mail."""
        if not user or not user.get("login"):
            return None
        login = user["login"]
        if login in self._users_mapping:
            return self._users_mapping[login]
        email = user.get("email") or (self._fetcher.user(login) or {}).get("email")
        if email:
            return email
        self.unknown_users.add(login)
        return None

    def emails(self, users: Optional[List[Dict[str, Any]]]) -> List[str]:
        return [email for email in dict.fromkeys(self.email(user) for user in users or []) if email]

    def author(self, user: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        """E-mail of an author and the note of an unknown author; an unknown or empty author is the token owner."""
        email = self.email(user)
        if email:
            return email, None
        if not user or not user.get("login"):
            return self.owner, None
        return self.owner, UNKNOWN_AUTHOR_NOTE.format(name=user.get("fullName") or user["login"])


class BaseDumper(ABC):

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass

    def _clean_and_save_data(self, prepared_data: Any, dump_path: str) -> None:
        prepared_data = clean_empty(prepared_data)
        os.makedirs(dump_path, exist_ok=True)
        dump_to_yaml(prepared_data, os.path.join(dump_path, self._filename))


def clean_empty(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            cleaned_value = clean_empty(value)
            if cleaned_value not in (None, "", [], {}):
                result[key] = cleaned_value
        return result
    if isinstance(data, list):
        result = []
        for item in data:
            cleaned_item = clean_empty(item)
            if cleaned_item not in (None, "", [], {}):
                result.append(cleaned_item)
        return result
    return data


@dataclass
class LocalField:
    key: str
    name: str
    project_field: Dict[str, Any]


@dataclass
class QueuePlan:
    """Roles of the fields of a project and the Tracker objects of its queue."""
    queue_key: str
    # role -> project custom field id: status, type, priority, assignee, components, fixVersions, affectedVersions,
    # deadline, storyPoints, estimation
    roles: Dict[str, str] = field(default_factory=dict)
    # role -> id of the YouTrack field: boards refer to fields by these ids
    role_field_ids: Dict[str, str] = field(default_factory=dict)
    # project custom field id -> local field
    fields: Dict[str, LocalField] = field(default_factory=dict)
    # base name of a state -> status key
    status_by_state: Dict[str, str] = field(default_factory=dict)
    # base name of a resolved state -> resolution key
    resolution_by_state: Dict[str, str] = field(default_factory=dict)
    initial_status: Optional[str] = None
    # base name of a type -> issue type key
    type_by_value: Dict[str, str] = field(default_factory=dict)
    default_type: str = DEFAULT_TYPE
    # name of the component of issues visible to a part of users; it is taken when a Subsystem value has this name
    restricted_component: str = RESTRICTED_COMPONENT_NAME
    restricted_component_taken: bool = False

    def role_of(self, project_field_id: Optional[str]) -> Optional[str]:
        return next((role for role, field_id in self.roles.items() if field_id == project_field_id), None)


class Catalog:
    """Statuses, issue types and resolutions of all queues: one object per base name."""

    def __init__(self):
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.issue_types: Dict[str, Dict[str, str]] = {}
        self.resolutions: Dict[str, Dict[str, str]] = {}
        self._status_keys = KeyAllocator()
        # base name of a state -> status key
        self._status_by_state: Dict[str, str] = {}
        self._status_names = {"en": NameAllocator(), "ru": NameAllocator()}
        self._type_keys = KeyAllocator(reserved=TYPE_KEYS.values())
        self._type_names = {"en": NameAllocator(), "ru": NameAllocator()}

    def status(self, value: Dict[str, Any], status_type: str) -> str:
        """Key of the status of a state; states with one base name share a status."""
        names = bilingual_name(value)
        identity = normalize_name(names["en"])
        key = self._status_by_state.get(identity)
        if key is None:
            key = self._status_keys.new_key(_without_apostrophes(names["en"]) or "status", _fallback_key("status", value))
            self._status_by_state[identity] = key
            self.statuses[key] = {
                # Tracker matches statuses by name in both languages: names of different statuses differ
                "name": {language: self._status_names[language].new_name(name) for language, name in names.items()},
                "type": status_type,
            }
        elif self.statuses[key]["type"] != status_type:
            logger.warning(f"States {names['en']} have different types, the status type is {self.statuses[key]['type']}")
        return key

    def open_status(self) -> str:
        return self.status(OPEN_STATE, "new")

    def issue_type(self, value: Dict[str, Any]) -> str:
        key = TYPE_KEYS.get(base_name(value)) or self._type_keys.key(
            _without_apostrophes(value.get("name") or "") or "type", _fallback_key("type", value)
        )
        if key not in self.issue_types:
            self.issue_types[key] = {
                language: self._type_names[language].new_name(name) for language, name in bilingual_name(value).items()
            }
        return key

    def resolution(self, state: Dict[str, Any]) -> str:
        key = RESOLUTION_KEYS.get(base_name(state), RESOLUTION_FIXED)
        self.resolutions.setdefault(key, RESOLUTION_NAMES[key])
        return key


def queue_access(
    project: Dict[str, Any], assignments: Optional[List[Dict[str, Any]]], users: UserResolver,
    teams: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Dict[str, List[str]]]]:
    """Read, create and write access to the queue of a project by the roles in the project and the global roles.
    A permission of all users is the group «Все сотрудники»; None when all users have all three. Without roles the
    team of the project gets all three. `teams` are the teams of projects by id."""

    def team_subjects(team: Dict[str, Any]) -> Dict[str, List[str]]:
        return {
            "users": users.emails(team.get("users")),
            "groups": [group["name"] for group in team.get("groups") or [] if group.get("name")],
        }

    if assignments is None:
        subjects = team_subjects(project.get("team") or {})
        return _closed_access({permission: subjects for permission in QUEUE_PERMISSIONS}, users)
    access = {permission: {"users": [], "groups": []} for permission in QUEUE_PERMISSIONS}
    everyone = set()
    for assignment in assignments:
        scope = assignment.get("scope") or {}
        if scope.get("$type") == "ProjectScope":
            if (scope.get("project") or {}).get("id") != project.get("id"):
                continue
        elif scope.get("$type") != "GlobalScope":
            continue
        keys = {permission.get("key") for permission in (assignment.get("role") or {}).get("permissions") or []}
        holder = assignment.get("holder") or {}
        for permission, key in QUEUE_PERMISSIONS.items():
            if key not in keys:
                continue
            subjects = access[permission]
            if holder.get("$type") in EVERYONE_GROUPS:
                everyone.add(permission)
                subjects["groups"].append(ALL_EMPLOYEES_GROUP)
            elif holder.get("$type") == "ProjectTeam":
                if holder.get("id") not in teams:
                    logger.warning(f"Team {holder.get('name')} with a role in project {project.get('shortName')} is not found")
                    continue
                team = team_subjects(teams[holder["id"]])
                subjects["users"] += team["users"]
                subjects["groups"] += team["groups"]
            elif holder.get("login"):
                subjects["users"] += users.emails([holder])
            elif holder.get("name"):
                subjects["groups"].append(holder["name"])
    if everyone == set(QUEUE_PERMISSIONS):
        return None
    return _closed_access(access, users)


def _closed_access(access: Dict[str, Dict[str, List[str]]], users: UserResolver) -> Dict[str, Dict[str, List[str]]]:
    """Access without repeated subjects. A queue without permissions is open to all users: when no subject is
    exported, the token owner keeps it closed."""
    access = {
        permission: {kind: list(dict.fromkeys(values)) for kind, values in subjects.items()}
        for permission, subjects in access.items()
    }
    if not any(values for subjects in access.values() for values in subjects.values()):
        owner = {"users": [users.owner], "groups": []}
        access = {permission: owner for permission in QUEUE_PERMISSIONS}
    return access


class ProjectToQueueDumper(BaseDumper):
    """YouTrack project -> Tracker queue. Values of the state field become statuses, values of the type field issue
    types; system fields go to Tracker fields, other fields become local fields."""

    def __init__(self, catalog: Catalog, restricted_component: Optional[str] = None):
        """`restricted_component` is the name of the component of issues visible to a part of users."""
        self._catalog = catalog
        self._restricted_component = restricted_component or RESTRICTED_COMPONENT_NAME

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    def plan(
        self, project: Dict[str, Any], queue_key: str, project_fields: List[Dict[str, Any]],
        time_tracking: Dict[str, Any], sprint_field_ids: Set[str], story_points_names: Set[str],
    ) -> QueuePlan:
        """`sprint_field_ids` are ids of the fields that hold sprints of boards: they are not exported as fields;
        `story_points_names` are names of the fields boards estimate by."""
        plan = QueuePlan(queue_key=queue_key)
        by_role = self._roles(project_fields, time_tracking, story_points_names)
        plan.roles = {role: project_field["id"] for role, project_field in by_role.items()}
        plan.role_field_ids = {role: (project_field.get("field") or {}).get("id") for role, project_field in by_role.items()}
        self._plan_statuses(plan, by_role.get("status"))
        self._plan_types(plan, by_role.get("type"))
        subsystems = value_names(bundle_values(by_role["components"])) if "components" in by_role else []
        plan.restricted_component = self._restricted_component
        plan.restricted_component_taken = normalize_name(self._restricted_component) in {
            normalize_name(name) for name in subsystems
        }
        for priority in bundle_values(by_role["priority"]) if "priority" in by_role else []:
            if base_name(priority) not in PRIORITY_KEYS:
                logger.warning(f"Priority {priority.get('name')} of project {queue_key} is exported as {DEFAULT_PRIORITY}")
        keys, names = KeyAllocator(), NameAllocator()
        for project_field in sorted(project_fields, key=lambda f: f.get("ordinal") or 0):
            field_id = project_field.get("id")
            if field_id in plan.roles.values() or (project_field.get("field") or {}).get("id") in sprint_field_ids:
                continue
            name = names.new_name(((project_field.get("field") or {}).get("name") or "").strip() or f"Поле {field_id}")
            plan.fields[field_id] = LocalField(keys.new_key(name, f"field{len(plan.fields) + 1}"), name, project_field)
        return plan

    @staticmethod
    def _roles(
        project_fields: List[Dict[str, Any]], time_tracking: Dict[str, Any], story_points_names: Set[str],
    ) -> Dict[str, Dict[str, Any]]:
        """System fields of a project by their names and types."""
        def find(name: str, *types: str) -> Optional[Dict[str, Any]]:
            return next(
                (f for f in project_fields if base_name(f.get("field")) == name and field_type_id(f).split("[")[0] in types),
                None,
            )

        roles = {
            "status": status_field(project_fields),
            "type": find(TYPE_FIELD, "enum"),
            "priority": find(PRIORITY_FIELD, "enum"),
            "assignee": find(ASSIGNEE_FIELD, "user"),
            "components": find(SUBSYSTEM_FIELD, "ownedField"),
            "fixVersions": find(FIX_VERSIONS_FIELD, "version"),
            "affectedVersions": find(AFFECTED_VERSIONS_FIELD, "version"),
            "deadline": find(DUE_DATE_FIELD, "date"),
            "storyPoints": find(STORY_POINTS_FIELD, "integer", "float") or next(
                (f for f in project_fields
                 if base_name(f.get("field")) in story_points_names and field_type_id(f) in ("integer", "float")),
                None,
            ),
        }
        estimate_id = (time_tracking.get("estimate") or {}).get("id")
        if time_tracking.get("enabled") and estimate_id:
            roles["estimation"] = next((f for f in project_fields if f.get("id") == estimate_id), None)
        spent_id = (time_tracking.get("timeSpent") or {}).get("id")
        if time_tracking.get("enabled") and spent_id:
            roles["spent"] = next((f for f in project_fields if f.get("id") == spent_id), None)
        if roles["type"] is not None and is_multi(roles["type"]):
            roles["type"] = None
        if roles["priority"] is not None and is_multi(roles["priority"]):
            roles["priority"] = None
        return {role: project_field for role, project_field in roles.items() if project_field is not None}

    def _plan_statuses(self, plan: QueuePlan, state_field: Optional[Dict[str, Any]]) -> None:
        """A resolved state is a status of type done, or cancelled when its resolution is not «Fixed»; the first
        unresolved state is the initial status, other states are in progress."""
        states = bundle_values(state_field) if state_field else []
        initial = next((state for state in states if not state.get("isResolved")), None)
        for state in states:
            if state.get("isResolved"):
                resolution = self._catalog.resolution(state)
                plan.resolution_by_state[base_name(state)] = resolution
                status_type = "done" if resolution == RESOLUTION_FIXED else "cancelled"
            else:
                status_type = "new" if state is initial else "inProgress"
            plan.status_by_state[base_name(state)] = self._catalog.status(state, status_type)
        if initial is not None:
            plan.initial_status = plan.status_by_state[base_name(initial)]
        elif plan.status_by_state:
            plan.initial_status = next(iter(plan.status_by_state.values()))
        else:
            plan.initial_status = self._catalog.open_status()

    def _plan_types(self, plan: QueuePlan, type_field: Optional[Dict[str, Any]]) -> None:
        for value in bundle_values(type_field) if type_field else []:
            plan.type_by_value[base_name(value)] = self._catalog.issue_type(value)
        if not plan.type_by_value:
            plan.type_by_value[""] = self._catalog.issue_type(DEFAULT_TYPE_VALUE)
        types = list(dict.fromkeys(plan.type_by_value.values()))
        plan.default_type = DEFAULT_TYPE if DEFAULT_TYPE in types else types[0]

    def dump(
        self, project: Dict[str, Any], plan: QueuePlan, project_fields: List[Dict[str, Any]], dump_path: str,
        users: UserResolver, access: Optional[Dict[str, Dict[str, List[str]]]], restricted: bool = False,
    ) -> None:
        """`access` is the access to the queue, see `queue_access`; `restricted` adds the component of issues visible to
        a part of users."""
        by_id = {project_field.get("id"): project_field for project_field in project_fields}
        team_users = users.emails((project.get("team") or {}).get("users"))
        prepared_data = {
            "key": plan.queue_key,
            "name": project.get("name") or plan.queue_key,
            "lead": users.email(project.get("leader")) or users.owner,
            "defaultType": plan.default_type,
            "defaultPriority": DEFAULT_PRIORITY,
            "description": project.get("description"),
            "teamUsers": team_users,
            "workflows": self._prepare_workflows(plan, project.get("name") or plan.queue_key),
            "issueTypesConfig": self._prepare_issue_types_config(plan),
            "components": self._prepare_components(by_id.get(plan.roles.get("components")), users)
            + ([dict(RESTRICTED_COMPONENT, name=plan.restricted_component)] if restricted else []),
            "versions": self._prepare_versions(
                [by_id.get(plan.roles.get(role)) for role in ("fixVersions", "affectedVersions")]
            ),
            "local_fields": [self._prepare_local_field(local_field) for local_field in plan.fields.values()],
            "permissions": access,
        }
        self._clean_and_save_data(prepared_data, dump_path)

    @staticmethod
    def _workflow_key(queue_key: str) -> str:
        return f"W_{queue_key}"

    def _prepare_workflows(self, plan: QueuePlan, project_name: str) -> List[Dict[str, Any]]:
        """Any status is reachable from any other: workflows of YouTrack are scripts and are not exported."""
        keys = list(dict.fromkeys([plan.initial_status] + list(plan.status_by_state.values())))
        steps = [
            {"status": key, "metaAction": {"name": self._catalog.statuses[key]["name"], "target": key}}
            for key in keys
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

    def _prepare_issue_types_config(self, plan: QueuePlan) -> List[Dict[str, Any]]:
        resolutions = list(dict.fromkeys(plan.resolution_by_state.values())) or [RESOLUTION_FIXED]
        for key in resolutions:
            self._catalog.resolutions.setdefault(key, RESOLUTION_NAMES[key])
        return [
            {"issueType": issue_type, "workflow": self._workflow_key(plan.queue_key), "resolutions": resolutions}
            for issue_type in dict.fromkeys(plan.type_by_value.values())
        ]

    @staticmethod
    def _prepare_components(project_field: Optional[Dict[str, Any]], users: UserResolver) -> List[Dict[str, Any]]:
        if project_field is None:
            return []
        return [
            {
                "name": value.get("name"),
                "description": value.get("description") or value.get("name"),
                "lead": users.email(value.get("owner")),
            }
            for value in bundle_values(project_field) if value.get("name")
        ]

    @staticmethod
    def _prepare_versions(project_fields: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Values of the version fields; the fields usually share one bundle."""
        versions: Dict[str, Dict[str, Any]] = {}
        for project_field in project_fields:
            for value in bundle_values(project_field) if project_field else []:
                name = value.get("name")
                if not name or name in versions:
                    continue
                released = bool(value.get("released"))
                date = ms_to_date(value.get("releaseDate"))
                versions[name] = {
                    "name": name,
                    "description": value.get("description"),
                    "released": released,
                    "releasedAt": date if released else None,
                    "dueDate": None if released else date,
                    "archived": bool(value.get("archived")),
                }
        return list(versions.values())

    @staticmethod
    def _prepare_local_field(local_field: LocalField) -> Dict[str, Any]:
        project_field = local_field.project_field
        type_id = field_type_id(project_field)
        kind = type_id.split("[")[0]
        multi = is_multi(project_field)
        options = list(dict.fromkeys(value.get("name") for value in bundle_values(project_field) if value.get("name")))
        options_provider = None
        if kind == "user":
            schema = {"type": "user"}
        elif kind in ("enum", "state", "ownedField", "version", "build", "group"):
            schema = {"type": "string"}
            if options:
                options_provider = {"type": "FixedListOptionsProvider", "values": options}
        elif type_id in ("integer", "period"):
            schema = {"type": "integer"}
        elif type_id == "float":
            schema = {"type": "float"}
        elif type_id == "date":
            schema = {"type": "date"}
        elif type_id == "date and time":
            schema = {"type": "datetime"}
        elif type_id == "text":
            schema = {"type": "text"}
        else:
            if type_id != "string":
                logger.warning(f"Unknown field type {type_id} of field {local_field.name}, it is exported as a string")
            schema = {"type": "string"}
        if multi:
            schema = {"type": "array", "items": schema["type"]}
        return {
            "category": FIELD_CATEGORY,
            "key": local_field.key,
            "name": {"en": local_field.name, "ru": local_field.name},
            "description": "Минуты" if type_id == "period" else None,
            "schema": schema,
            "optionsProvider": options_provider,
        }


@dataclass
class BoardPlan:
    """Boards and sprints of the issues: issue id -> board ids and sprint ids."""
    boards: List[Dict[str, Any]] = field(default_factory=list)
    boards_by_issue: Dict[str, List[str]] = field(default_factory=dict)
    sprints_by_issue: Dict[str, List[str]] = field(default_factory=dict)


class BoardDumper:
    """YouTrack agile board -> board of `boards.yaml`. Columns by the state field hold its statuses; sprints with dates
    become sprints. An issue is on a board when it is in a sprint of the board, including the sprint of a board without
    sprints and a sprint without dates."""

    def __init__(self, users: UserResolver):
        self._users = users

    def prepare(
        self, board: Dict[str, Any], plans: List[QueuePlan], now_ms: int,
    ) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
        """The board and its sprints by issue id. `plans` are the queues of the projects of the board in the export."""
        # base name of a state -> status keys of the projects of the board
        statuses_by_state: Dict[str, List[str]] = {}
        for plan in plans:
            for state, key in plan.status_by_state.items():
                statuses_by_state.setdefault(state, [])
                if key not in statuses_by_state[state]:
                    statuses_by_state[state].append(key)
        settings = board.get("columnSettings") or {}
        columns = []
        used = set()
        ordered = sorted(settings.get("columns") or [], key=lambda c: c.get("ordinal") or 0)
        for number, column in enumerate(ordered, start=1):
            statuses = [
                key for value in column.get("fieldValues") or []
                for key in statuses_by_state.get(base_name(value), []) if key not in used
            ]
            used.update(statuses)
            if statuses:
                columns.append({
                    # the API takes a number as a column id
                    "id": str(number),
                    "name": column.get("presentation"),
                    "statuses": list(dict.fromkeys(statuses)),
                    "limit": (column.get("wipLimit") or {}).get("max"),
                })
        sprints_settings = board.get("sprintsSettings") or {}
        sprints_enabled = not sprints_settings.get("disableSprints")
        sprints, sprint_by_issue = [], {}
        for sprint in board.get("sprints") or []:
            if not sprints_enabled:
                continue
            if sprint.get("start") is None or sprint.get("finish") is None:
                # a board with a sprint field shows issues without a sprint in a sprint without dates
                if sprint.get("issueIds") and not sprints_settings.get("sprintSyncField"):
                    logger.warning(
                        f"Sprint {sprint.get('name')} of board {board.get('name')} has no dates, "
                        f"its issues are on the board without a sprint"
                    )
                continue
            sprints.append({
                "id": str(sprint.get("id")),
                "name": sprint.get("name"),
                "status": self._sprint_status(sprint, now_ms),
                "startDateTime": ms_to_tracker_datetime(sprint.get("start")),
                "endDateTime": ms_to_tracker_datetime(sprint.get("finish")),
            })
            for issue_id in sprint.get("issueIds") or []:
                # an issue is in one sprint of a board in Tracker: the latest one
                previous = sprint_by_issue.get(issue_id)
                if previous is None or previous[1] <= sprint["start"]:
                    sprint_by_issue[issue_id] = (str(sprint.get("id")), sprint["start"])
        estimate_id = (board.get("estimationField") or {}).get("id")
        prepared = {
            "id": str(board.get("id")),
            "name": board.get("name"),
            "createdBy": self._users.email(board.get("owner")),
            "sprintsAvailable": sprints_enabled,
            "estimateBy": "storyPoints" if estimate_id and any(
                plan.role_field_ids.get("storyPoints") == estimate_id for plan in plans
            ) else None,
            "columns": columns,
            # a board without sprints that takes new issues gets the add rule by the queues of its projects
            "autoFilters": {
                "addFilter": {
                    "enabled": True,
                    "fieldValues": {"queue": [{"fixed": plan.queue_key, "invert": False} for plan in plans]},
                },
            } if not sprints_enabled and sprints_settings.get("addNewIssueToKanban") else None,
            "sprints": sprints,
        }
        return prepared, {issue_id: [sprint_id] for issue_id, (sprint_id, _) in sprint_by_issue.items()}

    @staticmethod
    def _sprint_status(sprint: Dict[str, Any], now_ms: int) -> str:
        if sprint.get("archived"):
            return "archived"
        if sprint["finish"] < now_ms:
            return "released"
        if sprint["start"] <= now_ms:
            return "in_progress"
        return "draft"


def is_restricted(issue: Dict[str, Any]) -> bool:
    """The issue is visible to a part of users: its visibility lists groups or users, none of them all users."""
    visibility = issue.get("visibility") or {}
    if visibility.get("$type") != "LimitedVisibility":
        return False
    groups = visibility.get("permittedGroups") or []
    if any(group.get("$type") in EVERYONE_GROUPS for group in groups):
        return False
    return bool(groups or visibility.get("permittedUsers"))


def custom_field(issue: Dict[str, Any], project_field_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not project_field_id:
        return None
    return next(
        (f for f in issue.get("customFields") or [] if (f.get("projectCustomField") or {}).get("id") == project_field_id),
        None,
    )


def field_value(issue: Dict[str, Any], project_field_id: Optional[str]) -> Any:
    return (custom_field(issue, project_field_id) or {}).get("value")


def single_value(issue: Dict[str, Any], project_field_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Value object of a single field; None when the value is empty or is not an object."""
    value = field_value(issue, project_field_id)
    return value if isinstance(value, dict) else None


def period_minutes(value: Any) -> Optional[int]:
    """Minutes of a period value ``{minutes, presentation}``."""
    minutes = value.get("minutes") if isinstance(value, dict) else None
    return minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else None


def value_list(value: Any) -> List[Dict[str, Any]]:
    """Values of a single or multiple field; a value that is not an object is skipped."""
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, dict)]


def value_names(value: Any) -> List[str]:
    return [item["name"] for item in value_list(value) if item.get("name")]


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".tif", ".tiff", ".avif")


def _fallback_key(prefix: str, value: Dict[str, Any]) -> str:
    """Key of a value whose name has no letters: the id of the value without separators."""
    return prefix + re.sub(r"[^0-9A-Za-z]", "", str(value.get("id") or ""))


def _without_apostrophes(name: str) -> str:
    """An apostrophe does not split words of a key: «Won't fix» -> ``wontFix``."""
    return re.sub(r"['’]", "", name)


def is_image(name: str) -> bool:
    return name.lower().endswith(_IMAGE_EXTENSIONS)


def file_name(file: Dict[str, Any]) -> str:
    return prepare_attachment_filename(file.get("name") or "file", str(file.get("id")))


def file_link(file: Dict[str, Any]) -> str:
    """Link of the importer to an attachment of the issue: an image is shown in the text."""
    name = file_name(file)
    return f"![{name}]()" if is_image(name) else f"[{name}]()"


def stored_files(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [file for file in issue.get("attachments") or [] if not file.get("removed")]


def rewrite_file_references(text: Optional[str], files: List[Dict[str, Any]], comment_id: Optional[str] = None) -> str:
    """Images and links of a text whose target is a file name of the issue -> links of the importer. A name of several
    files refers to the file of the same comment, else to the file of the issue itself."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for file in sorted(files, key=lambda f: ((f.get("comment") or {}).get("id") != comment_id, f.get("created") or 0)):
        by_name.setdefault(file.get("name") or "", file)

    def replace(match: re.Match) -> str:
        target = match.group(3) or match.group(4)
        file = by_name.get(target) or by_name.get(unquote(target))
        return file_link(file) if file else match.group(0)

    return _FILE_REFERENCE.sub(replace, text or "")


@dataclass
class IssueContext:
    """Data of the export an issue refers to."""
    plan: QueuePlan
    # issue id -> issue key in Tracker
    key_by_issue_id: Dict[str, str]
    created_by_issue_id: Dict[str, Optional[int]]
    board_plan: BoardPlan


class IssueDumper(BaseDumper):

    def __init__(self, client: SimpleYouTrackClient, fetcher: YouTrackDataFetcher, users: UserResolver):
        self._client = client
        self._fetcher = fetcher
        self._users = users
        # (issue key, item, error) of files and collections that are not exported
        self.failed_items: List[Tuple[str, str, str]] = []

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump(self, issue: Dict[str, Any], dump_path: str, issue_key: str, context: IssueContext) -> None:
        for collection, name in (("comments", "comments"), ("attachments", "files"), ("workItems", "work items")):
            if issue.get(collection) is None:
                self.failed_items.append((issue_key, name, f"{name} are not loaded"))
                issue[collection] = []
        if issue.get("activities") is None:
            logger.warning(f"History of {issue_key} is not loaded: links get the time of the later issue")
            issue["activities"] = []
        created_by, _ = self._users.author(issue.get("reporter"))
        files = stored_files(issue)
        comments = self._prepare_comments(issue, files)
        links = self._prepare_links(issue, context, created_by)
        plan = context.plan
        estimation = period_minutes(field_value(issue, plan.roles.get("estimation")))
        spent = period_minutes(field_value(issue, plan.roles.get("spent")))
        story_points = field_value(issue, plan.roles.get("storyPoints"))

        prepared_data = {
            "key": issue_key,
            "summary": issue.get("summary"),
            "createdAt": ms_to_tracker_datetime(issue.get("created")),
            "createdBy": created_by,
            "updatedAt": latest_tracker_datetime(
                [ms_to_tracker_datetime(issue.get("updated")), ms_to_tracker_datetime(issue.get("created"))]
                + [comment.get("updatedAt") or comment.get("createdAt") for comment in comments]
                + [ms_to_tracker_datetime(file.get("created")) for file in files]
                + [link.get("createdAt") for link in links]
                # a link is exported from one of the two issues: the other one is updated at the time of the link too
                + [ms_to_tracker_datetime(timestamp) for timestamp, _ in self._link_history(issue).values()]
            ),
            "updatedBy": self._users.author(issue.get("updater") or issue.get("reporter"))[0],
            **self._prepare_status(issue, plan),
            "type": plan.type_by_value.get(base_name(single_value(issue, plan.roles.get("type"))), plan.default_type),
            "priority": PRIORITY_KEYS.get(base_name(single_value(issue, plan.roles.get("priority"))), DEFAULT_PRIORITY),
            "description": rewrite_file_references(issue.get("description"), files) or None,
            **self._prepare_people(issue, plan),
            "tags": list(dict.fromkeys(tag.get("name") for tag in issue.get("tags") or [] if tag.get("name"))),
            "votedBy": self._users.emails((issue.get("voters") or {}).get("original")),
            "components": value_names(field_value(issue, plan.roles.get("components"))),
            "fixVersions": value_names(field_value(issue, plan.roles.get("fixVersions"))),
            "affectedVersions": value_names(field_value(issue, plan.roles.get("affectedVersions"))),
            "deadline": ms_to_date(field_value(issue, plan.roles.get("deadline"))),
            "storyPoints": story_points if isinstance(story_points, (int, float)) and not isinstance(story_points, bool) else None,
            "originalEstimation": estimation * 60 * 1000 if estimation else None,
            "spent": spent * 60 * 1000 if spent else None,
            "comments": comments,
            "attachments": [
                {
                    "filename": file_name(file),
                    "name": file.get("name"),
                    "createdAt": ms_to_tracker_datetime(file.get("created")),
                    "createdBy": self._users.author(file.get("author"))[0],
                }
                for file in files
            ],
            "worklogs": self._prepare_worklogs(issue),
            "links": links,
            "boards": context.board_plan.boards_by_issue.get(issue["id"], []),
            "sprint": context.board_plan.sprints_by_issue.get(issue["id"], []),
            **self._prepare_local_fields(issue, plan),
        }
        if is_restricted(issue):
            prepared_data["components"] = prepared_data["components"] + [plan.restricted_component]
            prepared_data["access"] = self._visibility_users(issue, issue_key)

        self._download_attachments(issue, files, dump_path, issue_key)
        self._clean_and_save_data(prepared_data, dump_path)

    def _visibility_users(self, issue: Dict[str, Any], issue_key: str) -> List[str]:
        """E-mails of the users an issue is visible to: users of its groups at the time of the export. Banned users are
        skipped; users without an e-mail are skipped and reported."""
        visibility = issue.get("visibility") or {}
        users = list(visibility.get("permittedUsers") or [])
        for group in visibility.get("permittedGroups") or []:
            members = self._fetcher.group_members(group)
            if members is None:
                # an issue without the users of its group loses their access: it is exported by a next run
                raise Exception(f"users of group {group.get('name')} of the issue visibility are not loaded")
            users += members
        active = [
            user for user in users
            if not user.get("banned") and not (self._fetcher.user(user.get("login") or "") or {}).get("banned")
        ]
        return self._users.emails(active)

    def _prepare_status(self, issue: Dict[str, Any], plan: QueuePlan) -> Dict[str, Any]:
        """Status of the state; an issue in a resolved state gets its resolution at the time of resolving."""
        state = single_value(issue, plan.roles.get("status"))
        status = plan.status_by_state.get(base_name(state)) if state else None
        if state and not status:
            logger.warning(
                f"State {state.get('name')} of issue {issue.get('idReadable')} is not a value of the state field of its "
                f"project, the issue gets the initial status"
            )
        result: Dict[str, Any] = {"status": status or plan.initial_status}
        resolution = plan.resolution_by_state.get(base_name(state)) if state else None
        if resolution:
            resolved = [a for a in issue.get("activities") or [] if (a.get("category") or {}).get("id") == "IssueResolvedCategory"]
            resolver = resolved[-1].get("author") if resolved else issue.get("updater")
            result.update({
                "resolution": resolution,
                "resolvedAt": ms_to_tracker_datetime(issue.get("resolved") or issue.get("updated")),
                "resolvedBy": self._users.author(resolver)[0],
            })
        return result

    def _prepare_people(self, issue: Dict[str, Any], plan: QueuePlan) -> Dict[str, Any]:
        """The first assignee is the assignee; other assignees and the watchers are followers."""
        assignees = self._users.emails(value_list(field_value(issue, plan.roles.get("assignee"))))
        assignee = assignees[0] if assignees else None
        watchers = [watcher.get("user") for watcher in (issue.get("watchers") or {}).get("issueWatchers") or []]
        followers = assignees[1:] + self._users.emails(watchers)
        return {
            "assignee": assignee,
            "followers": [email for email in dict.fromkeys(followers) if email != assignee],
        }

    def _prepare_comments(self, issue: Dict[str, Any], files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """A comment visible to a part of users in YouTrack starts with the list of them; files of a comment are linked
        at its end unless its text shows them."""
        files_by_comment: Dict[str, List[Dict[str, Any]]] = {}
        for file in files:
            comment_id = (file.get("comment") or {}).get("id")
            if comment_id:
                files_by_comment.setdefault(comment_id, []).append(file)
        result = []
        for comment in sorted(issue.get("comments") or [], key=lambda c: (c.get("created") or 0, str(c.get("id")))):
            if comment.get("deleted"):
                continue
            text = rewrite_file_references(comment.get("text"), files, comment.get("id")).strip()
            unlinked = [file for file in files_by_comment.get(comment.get("id"), []) if file_link(file) not in text]
            if not text and not unlinked:
                logger.warning(f"Comment {comment.get('id')} has no text and files, it is not exported")
                continue
            author, note = self._users.author(comment.get("author"))
            parts = [note, text, "\n".join(file_link(f) for f in unlinked)]
            text = "\n\n".join(part for part in parts if part)
            data = {"text": text, "createdBy": author, "createdAt": ms_to_tracker_datetime(comment.get("created"))}
            if comment.get("updated") and comment.get("updated") != comment.get("created"):
                data["updatedBy"] = author
                data["updatedAt"] = ms_to_tracker_datetime(comment.get("updated"))
            result.append(data)
        return result

    def _prepare_worklogs(self, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """A work item is dated by its day: its start is midnight UTC, as YouTrack stores it."""
        result = []
        for item in sorted(issue.get("workItems") or [], key=lambda i: (i.get("date") or 0, str(i.get("id")))):
            minutes = (item.get("duration") or {}).get("minutes") or 0
            if minutes <= 0:
                logger.warning(f"Work item {item.get('id')} has no duration, it is not exported")
                continue
            author, note = self._users.author(item.get("author") or item.get("creator"))
            work_type = (item.get("type") or {}).get("name")
            notes = [note, WORK_TYPE_NOTE.format(name=work_type) if work_type else None, item.get("text")]
            start = ms_to_tracker_datetime(item.get("date"))
            result.append({
                "start": start,
                "duration": minutes * 60 * 1000,
                "comment": "\n".join(n for n in notes if n),
                "createdAt": ms_to_tracker_datetime(item.get("created")) or start,
                "createdBy": author,
            })
        return result

    def _prepare_links(self, issue: Dict[str, Any], context: IssueContext, created_by: Optional[str]) -> List[Dict[str, Any]]:
        """Links to issues of the export, each pair once: from the issue that is earlier in the export. The time and
        the author of a link come from the history of the issue, else the time is the later creation of the two
        issues."""
        own_key = context.key_by_issue_id[issue["id"]]
        added = self._link_history(issue)
        links: Dict[str, Dict[str, Any]] = {}
        for link in issue.get("links") or []:
            link_type = base_name(link.get("linkType"))
            relationship = LINK_RELATIONSHIPS.get((link_type, link.get("direction")), RELATES)
            for other in link.get("issues") or []:
                other_key = context.key_by_issue_id.get(other.get("id"))
                if not other_key or other_key == own_key or _key_order(other_key) < _key_order(own_key):
                    continue
                if other_key in links:
                    continue
                timestamp, author = added.get(other.get("idReadable"), (None, None))
                links[other_key] = {
                    "issue": other_key,
                    "relationship": relationship,
                    "createdAt": ms_to_tracker_datetime(timestamp) or latest_tracker_datetime([
                        ms_to_tracker_datetime(issue.get("created")),
                        ms_to_tracker_datetime(context.created_by_issue_id.get(other.get("id"))),
                    ]),
                    "createdBy": self._users.author(author)[0] if author else created_by,
                }
        return list(links.values())

    @staticmethod
    def _link_history(issue: Dict[str, Any]) -> Dict[str, Tuple[int, Dict[str, Any]]]:
        """Readable id of a linked issue -> time and author of the latest addition of a link to it."""
        result: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        for activity in issue.get("activities") or []:
            if (activity.get("category") or {}).get("id") != "LinksCategory":
                continue
            for added in activity.get("added") or []:
                if isinstance(added, dict) and added.get("idReadable"):
                    result[added["idReadable"]] = (activity.get("timestamp"), activity.get("author"))
        return result

    def _prepare_local_fields(self, issue: Dict[str, Any], plan: QueuePlan) -> Dict[str, Any]:
        result = {}
        for issue_field in issue.get("customFields") or []:
            local_field = plan.fields.get((issue_field.get("projectCustomField") or {}).get("id"))
            if local_field is None:
                continue
            value = self._field_value(local_field, issue_field.get("value"))
            if value not in (None, "", []):
                result[f"local_{local_field.key}"] = value
        return result

    def _field_value(self, local_field: LocalField, value: Any) -> Any:
        if value is None:
            return None
        type_id = field_type_id(local_field.project_field)
        kind = type_id.split("[")[0]
        if kind == "user":
            emails = self._users.emails(value_list(value))
            return emails if is_multi(local_field.project_field) else next(iter(emails), None)
        if kind in ("enum", "state", "ownedField", "version", "build", "group"):
            names = value_names(value)
            return names if is_multi(local_field.project_field) else next(iter(names), None)
        if type_id == "period":
            return period_minutes(value)
        if type_id == "date":
            return ms_to_date(value)
        if type_id == "date and time":
            return ms_to_tracker_datetime(value)
        if type_id == "text":
            return value.get("text") if isinstance(value, dict) else value
        if type_id in ("integer", "float") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            logger.warning(f"Value {value} of field {local_field.name} is not a number, it is not exported")
            return None
        if isinstance(value, (dict, list)):
            logger.warning(f"Value {value} of field {local_field.name} is not exported")
            return None
        return value

    def _download_attachments(self, issue: Dict[str, Any], files: List[Dict[str, Any]], issue_path: str, issue_key: str) -> None:
        if not files:
            return
        attachments_path = os.path.join(issue_path, "attachments")
        os.makedirs(attachments_path, exist_ok=True)
        for file in files:
            filename = file_name(file)
            file_path = os.path.join(attachments_path, filename)
            size = file.get("size")
            # a file of a previous export is kept when its size matches the size in YouTrack
            if size is not None and os.path.isfile(file_path):
                if os.path.getsize(file_path) == size:
                    continue
                os.remove(file_path)
            try:
                if not file.get("url"):
                    raise DownloadError("no file url")
                self._client.download_file(file["url"], file_path, expected_size=size)
            except Exception as exc:
                # the traceback of a download error may hold the signed url
                logger.error(f"Failed to download {filename} of {issue_key}: {exc}")
                self.failed_items.append((issue_key, f"file {filename}", str(exc)))


def _key_order(issue_key: str) -> Tuple[str, int]:
    queue, _, number = issue_key.rpartition("-")
    return queue, int(number) if number.isdigit() else 0
