import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .weeek_client import SimpleWeeekClient, WeeekApiError

logger = logging.getLogger(__name__)

# WEEEK project statuses: 1 — active, 2 — archived
ARCHIVED_PROJECT_STATUS = 2


class ArchivedProjectError(Exception):
    pass


class WeeekDataFetcher:
    """Reads workspace data, projects, boards, tasks and comments from the WEEEK public API.

    Tasks of a project come with their custom fields, time entries and attachments; comments of a task are
    fetched with a request per task, parallelised over a thread pool.
    """

    def __init__(self, client: SimpleWeeekClient, max_workers: Optional[int] = None):
        self._client = client
        self._max_workers = max_workers or os.cpu_count()
        self._current_user: Optional[Dict[str, Any]] = None
        self._members_by_id: Dict[str, Dict[str, Any]] = {}
        self._tags_by_id: Dict[Any, str] = {}
        self._projects: Optional[List[Dict[str, Any]]] = None
        self._portfolios: Optional[List[Dict[str, Any]]] = None
        self.portfolios_failed = False
        self._contact_names: Optional[Dict[str, str]] = None
        self._contacts_lock = threading.Lock()
        # reference data that is not loaded: tags, portfolios, contacts
        self.failed_reference_data: List[str] = []
        self._timezone = "UTC"

    # -- workspace ------------------------------------------------------------

    def preload_reference_data(self) -> None:
        """The token owner and the members of the workspace are required; tags are optional."""
        self._current_user = (self._client.get("/user/me") or {}).get("user")
        if not self._current_user:
            raise Exception("Failed to fetch the token owner")
        members = (self._client.get("/ws/members") or {}).get("members") or []
        self._members_by_id = {member["id"]: member for member in members if member.get("id")}
        self._timezone = self._user_timezone(self._current_user)
        try:
            tags = (self._client.get("/ws/tags") or {}).get("tags") or []
            self._tags_by_id = {tag["id"]: tag.get("title") for tag in tags}
        except Exception as exc:
            logger.exception("Failed to fetch tags, tasks are exported without them")
            self.failed_reference_data.append(f"tags are not loaded: {exc}")

    @staticmethod
    def _user_timezone(user: Dict[str, Any]) -> str:
        timezone = user.get("timeZone")
        if not timezone:
            logger.warning("The token owner has no time zone, dates are taken in UTC")
            return "UTC"
        try:
            ZoneInfo(timezone)
            return timezone
        except Exception:
            logger.warning(f"Unknown time zone {timezone} of the token owner, dates are taken in UTC")
            return "UTC"

    @property
    def current_user(self) -> Optional[Dict[str, Any]]:
        return self._current_user

    @property
    def timezone(self) -> str:
        """Time zone of the token owner: dates of tasks are converted to days in it."""
        return self._timezone

    def resolve_member(self, user_id: Any) -> Optional[Dict[str, Any]]:
        return self._members_by_id.get(user_id)

    def tag_title(self, tag_id: Any) -> Optional[str]:
        return self._tags_by_id.get(tag_id)

    def contact_name(self, contact_id: str) -> Optional[str]:
        """Name of a CRM contact; contacts are loaded on the first call."""
        with self._contacts_lock:
            if self._contact_names is None:
                self._contact_names = self._load_contact_names()
        return self._contact_names.get(contact_id) or None

    def _load_contact_names(self) -> Dict[str, str]:
        try:
            contacts = (self._client.get("/crm/contacts") or {}).get("contacts") or []
        except Exception as exc:
            logger.exception("Failed to fetch CRM contacts, contact fields are exported with ids")
            self.failed_reference_data.append(f"CRM contacts are not loaded, contact fields hold ids: {exc}")
            return {}
        return {
            contact.get("id"): " ".join(
                part for part in (contact.get("firstName"), contact.get("middleName"), contact.get("lastName")) if part
            )
            for contact in contacts
        }

    # -- projects -------------------------------------------------------------

    def projects(self) -> List[Dict[str, Any]]:
        """Projects of the workspace that are not archived."""
        if self._projects is None:
            self._projects = (self._client.get("/tm/projects") or {}).get("projects") or []
        return self._projects

    def fetch_project(self, project_id: str) -> Dict[str, Any]:
        """A project that is not archived: an archived project has no tasks and boards in the API."""
        for project in self.projects():
            if str(project.get("id")) == str(project_id):
                return project
        try:
            project = (self._client.get(f"/tm/projects/{project_id}") or {}).get("project")
        except WeeekApiError as exc:
            if exc.status_code in (400, 404):
                raise Exception(f"Project {project_id} is not found") from exc
            raise
        if not project:
            raise Exception(f"Project {project_id} is not found")
        if project.get("status") == ARCHIVED_PROJECT_STATUS:
            raise ArchivedProjectError(f"Project {project_id} is archived: restore it from the archive in WEEEK and export again")
        return project

    def portfolios(self) -> List[Dict[str, Any]]:
        """Portfolios of all levels; none when they are not available."""
        if self._portfolios is None:
            try:
                self._portfolios = self._client.get_paginated("/tm/portfolios", "data")
            except Exception as exc:
                logger.exception("Failed to fetch portfolios, projects are exported without them")
                self.failed_reference_data.append(f"portfolios are not loaded: {exc}")
                self.portfolios_failed = True
                self._portfolios = []
        return self._portfolios

    def fetch_boards(self, project_id: str) -> List[Dict[str, Any]]:
        """Boards of a project with their columns in ``columns``."""
        boards = (self._client.get("/tm/boards", params={"projectId": project_id}) or {}).get("boards") or []
        for board in boards:
            board["columns"] = (
                self._client.get("/tm/board-columns", params={"boardId": board["id"]}) or {}
            ).get("boardColumns") or []
        return boards

    # -- tasks ----------------------------------------------------------------

    def fetch_tasks(self, project_id: str) -> List[Dict[str, Any]]:
        """Tasks of a project, completed ones included; deleted tasks are not returned."""
        return self._client.get_paginated(
            "/tm/tasks", "tasks", params={"projectId": project_id}, limit_param="perPage"
        )

    def fetch_comments(self, tasks: List[Dict[str, Any]]) -> None:
        """Sets ``comments`` of every task; a task whose comments are not loaded gets None."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for task, comments in zip(tasks, executor.map(self._task_comments, tasks)):
                task["comments"] = comments

    def _task_comments(self, task: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        try:
            comments = self._client.get_paginated(f"/tm/tasks/{task['id']}/comments", "comments")
            # comments come newest first: a comment sent during the export shifts the pages
            return list({comment.get("id"): comment for comment in comments}.values())
        except Exception:
            logger.exception(f"Failed to fetch comments of task {task['id']}")
            return None

    def fresh_file_url(self, file_id: str) -> Optional[str]:
        """A new signed url of a file: a url of a task lives one hour."""
        return ((self._client.get(f"/ws/attachments/{file_id}") or {}).get("data") or {}).get("url")
