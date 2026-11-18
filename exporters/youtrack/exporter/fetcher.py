import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from .youtrack_client import SimpleYouTrackClient, YouTrackApiError

logger = logging.getLogger(__name__)

USER_FIELDS = "login,fullName,email"
PROJECT_FIELDS = (
    f"id,shortName,name,description,archived,leader({USER_FIELDS}),createdBy({USER_FIELDS}),"
    f"team(id,users({USER_FIELDS}),groups(name))"
)
PROJECT_CUSTOM_FIELD_FIELDS = (
    "id,$type,ordinal,field(id,name,localizedName,fieldType(id,isMultiValue)),"
    f"bundle(id,values(id,name,localizedName,description,ordinal,archived,isResolved,released,releaseDate,owner({USER_FIELDS})))"
)
TIME_TRACKING_FIELDS = "enabled,estimate(id,field(name)),timeSpent(id,field(name))"
VALUE_FIELDS = f"$type,id,name,localizedName,{USER_FIELDS},minutes,presentation,text,isResolved"
ISSUE_FIELDS = (
    "id,idReadable,numberInProject,summary,description,created,updated,resolved,isDraft,project(id),"
    f"reporter({USER_FIELDS}),updater({USER_FIELDS}),tags(name),"
    f"watchers(issueWatchers(user({USER_FIELDS}))),voters(original({USER_FIELDS})),"
    f"visibility($type,permittedGroups($type,id,name),permittedUsers({USER_FIELDS},banned)),"
    f"customFields($type,name,projectCustomField(id,field(id,name)),value({VALUE_FIELDS})),"
    "links(direction,linkType(id,name,sourceToTarget,targetToSource,directed,aggregation),issues(id,idReadable))"
)
COMMENT_FIELDS = (
    f"id,text,created,updated,deleted,author({USER_FIELDS}),"
    f"visibility($type,permittedGroups(name),permittedUsers({USER_FIELDS})),attachments(id)"
)
ATTACHMENT_FIELDS = f"id,name,size,mimeType,created,removed,url,author({USER_FIELDS}),comment(id)"
WORK_ITEM_FIELDS = f"id,date,duration(minutes),text,created,author({USER_FIELDS}),creator({USER_FIELDS}),type(name)"
ACTIVITY_CATEGORIES = "LinksCategory,IssueResolvedCategory"
ACTIVITY_FIELDS = f"timestamp,author({USER_FIELDS}),category(id),added(idReadable),removed(idReadable),targetMember"
ROLE_ASSIGNMENT_FIELDS = (
    f"role(name,permissions(key)),scope($type,project(id)),holder($type,id,name,{USER_FIELDS})"
)
BOARD_FIELDS = (
    f"id,name,owner({USER_FIELDS}),projects(id,shortName),"
    "columnSettings(field(id,name),columns(id,presentation,ordinal,isResolved,wipLimit(max),fieldValues(name))),"
    "sprintsSettings(disableSprints,addNewIssueToKanban,sprintSyncField(id,name)),estimationField(id,name),"
    "sprints(id,name,goal,start,finish,archived)"
)


class ArchivedProjectError(Exception):
    pass


class YouTrackDataFetcher:
    """Reads users, projects, issues and boards from the YouTrack REST API.

    Issues of a project come with their custom fields and links; comments, files, work items and the history of
    links of an issue are fetched with requests per issue, parallelised over a thread pool.
    """

    def __init__(self, client: SimpleYouTrackClient, max_workers: Optional[int] = None):
        self._client = client
        self._max_workers = max_workers or os.cpu_count()
        self._current_user: Optional[Dict[str, Any]] = None
        self._users_by_login: Dict[str, Dict[str, Any]] = {}
        self._projects: Optional[List[Dict[str, Any]]] = None
        self._role_assignments: Optional[List[Dict[str, Any]]] = None
        self._role_assignments_loaded = False
        self._group_members: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        self._group_members_lock = threading.Lock()
        # reference data that is not loaded: users, boards
        self.failed_reference_data: List[str] = []

    # -- users ----------------------------------------------------------------

    def preload_reference_data(self) -> None:
        """The token owner is required; the list of users is optional: users of issues come with their e-mails."""
        self._current_user = self._client.get("users/me", params={"fields": USER_FIELDS})
        if not self._current_user or not self._current_user.get("login"):
            raise Exception("Failed to fetch the token owner")
        try:
            users = self._client.get_all("users", params={"fields": f"{USER_FIELDS},banned"})
            self._users_by_login = {user["login"]: user for user in users if user.get("login")}
        except Exception as exc:
            logger.exception("Failed to fetch users, e-mails are taken from issues")
            self.failed_reference_data.append(f"users are not loaded: {exc}")

    @property
    def current_user(self) -> Optional[Dict[str, Any]]:
        return self._current_user

    def user(self, login: str) -> Optional[Dict[str, Any]]:
        return self._users_by_login.get(login)

    # -- projects -------------------------------------------------------------

    def projects(self) -> List[Dict[str, Any]]:
        if self._projects is None:
            self._projects = self._client.get_all("admin/projects", params={"fields": PROJECT_FIELDS})
        return self._projects

    def fetch_project(self, project_ref: str) -> Dict[str, Any]:
        """A project by its short name or id; the API gives no issues of an archived project."""
        project = next(
            (
                project for project in self.projects()
                if project_ref in (project.get("shortName"), project.get("id"))
            ),
            None,
        )
        if project is None:
            raise Exception(f"Project {project_ref} is not found")
        if project.get("archived"):
            raise ArchivedProjectError(
                f"Project {project_ref} is archived: restore it from the archive in YouTrack and export again"
            )
        return project

    def fetch_custom_fields(self, project_id: str) -> List[Dict[str, Any]]:
        return self._client.get_all(f"admin/projects/{project_id}/customFields", params={"fields": PROJECT_CUSTOM_FIELD_FIELDS})

    def role_assignments(self) -> Optional[List[Dict[str, Any]]]:
        """Roles of users and groups in projects and globally; None when YouTrack does not give them (versions
        before 2026.1)."""
        if not self._role_assignments_loaded:
            self._role_assignments_loaded = True
            try:
                self._role_assignments = self._client.get_all("assignedRoles", params={"fields": ROLE_ASSIGNMENT_FIELDS})
            except Exception as exc:
                logger.warning(f"Roles are not loaded, queues get access of project teams: {exc}")
                if not (isinstance(exc, YouTrackApiError) and exc.status_code == 404):
                    self.failed_reference_data.append(f"roles are not loaded, queues get access of project teams: {exc}")
        return self._role_assignments

    def group_members(self, group: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Users of a group, its subgroups included; None when they are not loaded."""
        group_id = group.get("id")
        with self._group_members_lock:
            if group_id not in self._group_members:
                try:
                    self._group_members[group_id] = self._client.get_all(
                        f"groups/{group_id}/users", params={"fields": f"{USER_FIELDS},banned"}
                    )
                except Exception:
                    logger.exception(f"Failed to fetch users of group {group.get('name')}")
                    self._group_members[group_id] = None
            return self._group_members[group_id]

    def fetch_time_tracking(self, project_id: str) -> Dict[str, Any]:
        return self._client.get(f"admin/projects/{project_id}/timeTrackingSettings", params={"fields": TIME_TRACKING_FIELDS}) or {}

    # -- issues ---------------------------------------------------------------

    def fetch_issues(self, project: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Issues of a project in the order of creation; drafts are not returned."""
        query = f"project: {{{project['shortName']}}} sort by: created asc"
        issues = self._client.get_all("issues", params={"query": query, "fields": ISSUE_FIELDS})
        # duplicates come from shifted pages; the query also matches a project whose name is the short name
        return [
            issue for issue in {issue["id"]: issue for issue in issues}.values()
            if not issue.get("isDraft") and (issue.get("project") or {}).get("id") == project["id"]
        ]

    def fetch_issue_details(self, issues: List[Dict[str, Any]], time_tracking: bool) -> None:
        """Sets ``comments``, ``attachments``, ``workItems`` and ``activities`` of every issue; a collection that is
        not loaded is None. Work items are read only when time tracking of the project is enabled."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            list(executor.map(lambda issue: self._issue_details(issue, time_tracking), issues))

    def _issue_details(self, issue: Dict[str, Any], time_tracking: bool) -> None:
        issue_id = issue["id"]
        issue["comments"] = self._collection(issue, "comments", f"issues/{issue_id}/comments", {"fields": COMMENT_FIELDS})
        issue["attachments"] = self._collection(issue, "files", f"issues/{issue_id}/attachments", {"fields": ATTACHMENT_FIELDS})
        issue["workItems"] = self._collection(
            issue, "work items", f"issues/{issue_id}/timeTracking/workItems", {"fields": WORK_ITEM_FIELDS}
        ) if time_tracking else []
        issue["activities"] = self._collection(
            issue, "history", f"issues/{issue_id}/activities",
            {"categories": ACTIVITY_CATEGORIES, "fields": ACTIVITY_FIELDS, "reverse": "false"},
        )

    def _collection(self, issue: Dict[str, Any], name: str, endpoint: str, params: Dict[str, Any]) -> Optional[List[Any]]:
        try:
            return self._client.get_all(endpoint, params=params)
        except Exception:
            logger.exception(f"Failed to fetch {name} of issue {issue.get('idReadable')}")
            return None

    # -- boards ---------------------------------------------------------------

    def fetch_boards(self, status_field_ids: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
        """Agile boards of the projects whose columns are values of the status field of one of their projects:
        columns of a Tracker board are statuses. `status_field_ids` are the ids of the status fields by project id. A
        sprint has the ids of its issues in ``issueIds``. A board that is not loaded is skipped and reported."""
        try:
            boards = self._client.get_all("agiles", params={"fields": BOARD_FIELDS})
        except Exception as exc:
            logger.exception("Failed to fetch boards, issues are exported without them")
            self.failed_reference_data.append(f"boards are not loaded: {exc}")
            return []
        result = []
        for board in boards:
            project_ids = [project.get("id") for project in board.get("projects") or [] if project.get("id") in status_field_ids]
            if not project_ids:
                continue
            column_field = (board.get("columnSettings") or {}).get("field") or {}
            if not column_field.get("id"):
                logger.warning(f"Board {board.get('name')} has no columns, the board is not exported")
                continue
            if column_field["id"] not in {status_field_ids[p] for p in project_ids}:
                logger.warning(
                    f"Columns of board {board.get('name')} are values of field {column_field.get('name')}, "
                    f"the board is not exported"
                )
                continue
            try:
                for sprint in board.get("sprints") or []:
                    issues = self._client.get_all(
                        f"agiles/{board['id']}/sprints/{sprint['id']}/issues", params={"fields": "id"}
                    )
                    sprint["issueIds"] = [issue["id"] for issue in issues]
            except Exception as exc:
                logger.exception(f"Failed to fetch sprints of board {board.get('name')}")
                self.failed_reference_data.append(f"board {board.get('name')} is not loaded: {exc}")
                continue
            result.append(board)
        return result
