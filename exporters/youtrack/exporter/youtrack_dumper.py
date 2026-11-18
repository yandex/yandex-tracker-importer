import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .dump_results import (
    IssueDumpError,
    IssueDumpResult,
    IssueDumpSuccess,
    ProjectDumpError,
    ProjectDumpResult,
    ProjectDumpSuccess,
)
from .dumpers import (
    BoardDumper,
    BoardPlan,
    Catalog,
    IssueContext,
    IssueDumper,
    ProjectToQueueDumper,
    QueuePlan,
    UserResolver,
    base_name,
    clean_empty,
    is_restricted,
    queue_access,
    status_field,
)
from .fetcher import YouTrackDataFetcher
from .utils import dump_to_yaml
from .youtrack_client import SimpleYouTrackClient

logger = logging.getLogger(__name__)


def check_queue_keys(projects_mapper: Dict[str, str]) -> None:
    """Rejects a queue key of several projects, ignoring case."""
    projects_by_key: Dict[str, List[str]] = defaultdict(list)
    for project_ref, queue_key in projects_mapper.items():
        projects_by_key[queue_key.upper()].append(project_ref)
    repeated = {key: refs for key, refs in projects_by_key.items() if len(refs) > 1}
    if repeated:
        raise ValueError(
            "projects.cfg: one queue key for several projects: "
            + "; ".join(f"{key} — {', '.join(refs)}" for key, refs in sorted(repeated.items()))
        )


@dataclass
class LoadedProject:
    project: Dict[str, Any]
    queue_key: str
    fields: List[Dict[str, Any]]
    time_tracking: Dict[str, Any]
    issues: List[Dict[str, Any]]
    plan: Optional[QueuePlan] = None


class YouTrackDumper:
    def __init__(
        self,
        client: SimpleYouTrackClient,
        projects_mapper: Dict[str, str],
        users_mapping: Dict[str, str],
        data_path: str,
        max_workers: Optional[int] = None,
        restricted_component: Optional[str] = None,
    ):
        self._data_path = data_path
        self._projects_mapper = projects_mapper
        check_queue_keys(projects_mapper)
        self._max_workers = max_workers or os.cpu_count()

        self._fetcher = YouTrackDataFetcher(client, max_workers)
        self._fetcher.preload_reference_data()
        self._users = UserResolver(self._fetcher, users_mapping)
        if not self._users.owner:
            login = self._fetcher.current_user["login"]
            raise ValueError(f"The token owner {login} has no e-mail: add {login}:<e-mail> to configs/users.cfg")

        self._catalog = Catalog()
        self._queue_dumper = ProjectToQueueDumper(self._catalog, restricted_component)
        self._board_dumper = BoardDumper(self._users)
        self._issue_dumper = IssueDumper(client, self._fetcher, self._users)

    @property
    def failed_items(self) -> List[Tuple[str, str, str]]:
        return self._issue_dumper.failed_items

    @property
    def unknown_users(self) -> Set[str]:
        return self._users.unknown_users

    @property
    def failed_reference_data(self) -> List[str]:
        return self._fetcher.failed_reference_data

    def dump(self) -> Iterator[ProjectDumpResult]:
        self._issue_dumper.failed_items.clear()
        # all projects are loaded and planned first: an issue links issues of other projects, a board shows several
        # projects
        loaded: Dict[str, Any] = {}
        refs_by_id: Dict[str, str] = {}
        for project_ref in self._projects_mapper:
            try:
                project = self._fetcher.fetch_project(project_ref)
                if project["id"] in refs_by_id:
                    raise Exception(f"Project {project_ref} is already in projects.cfg as {refs_by_id[project['id']]}")
                refs_by_id[project["id"]] = project_ref
                loaded[project_ref] = project
            except Exception as exc:
                logger.exception(f"Failed to load project {project_ref}")
                loaded[project_ref] = exc
        settings: Dict[str, Any] = {}
        for project_ref, project in loaded.items():
            if isinstance(project, Exception):
                continue
            try:
                settings[project_ref] = (
                    self._fetcher.fetch_custom_fields(project["id"]), self._fetcher.fetch_time_tracking(project["id"])
                )
            except Exception as exc:
                logger.exception(f"Failed to load project {project_ref}")
                loaded[project_ref] = exc
        status_field_ids = {
            loaded[ref]["id"]: ((status_field(fields) or {}).get("field") or {}).get("id")
            for ref, (fields, _) in settings.items() if not isinstance(loaded[ref], Exception)
        }
        boards = self._fetcher.fetch_boards(status_field_ids)
        for project_ref, queue_key in self._projects_mapper.items():
            if isinstance(loaded[project_ref], Exception):
                continue
            project_boards = [
                board for board in boards
                if any(p.get("id") == loaded[project_ref]["id"] for p in board.get("projects") or [])
            ]
            # fields that hold sprints of the boards of the project and the fields the boards estimate by
            sprint_field_ids = {
                ((board.get("sprintsSettings") or {}).get("sprintSyncField") or {}).get("id") for board in project_boards
            } - {None}
            story_points_names = {base_name(board.get("estimationField")) for board in project_boards} - {""}
            try:
                loaded[project_ref] = self._load_project(
                    loaded[project_ref], queue_key, *settings[project_ref], sprint_field_ids, story_points_names
                )
            except Exception as exc:
                logger.exception(f"Failed to load project {project_ref}")
                loaded[project_ref] = exc
        projects = [value for value in loaded.values() if isinstance(value, LoadedProject)]
        key_by_issue_id = {
            issue["id"]: f"{value.queue_key}-{issue['numberInProject']}" for value in projects for issue in value.issues
        }
        created_by_issue_id = {issue["id"]: issue.get("created") for value in projects for issue in value.issues}
        board_plan = self._plan_boards(boards, projects)

        for project_ref, value in loaded.items():
            if isinstance(value, Exception):
                yield ProjectDumpError(project_ref, value)
                continue
            context = IssueContext(value.plan, key_by_issue_id, created_by_issue_id, board_plan)
            yield self._dump_project(project_ref, value, context)
        self._dump_boards(board_plan)
        self._dump_statuses()
        self._dump_resolutions()
        self._dump_issue_types()

    def _load_project(
        self, project: Dict[str, Any], queue_key: str, fields: List[Dict[str, Any]], time_tracking: Dict[str, Any],
        sprint_field_ids: Set[str], story_points_names: Set[str],
    ) -> LoadedProject:
        issues = self._fetcher.fetch_issues(project)
        value = LoadedProject(project, queue_key, fields, time_tracking, issues)
        value.plan = self._queue_dumper.plan(project, queue_key, fields, time_tracking, sprint_field_ids, story_points_names)
        return value

    def _plan_boards(self, boards: List[Dict[str, Any]], projects: List[LoadedProject]) -> BoardPlan:
        """Boards that show projects of the export; an issue is on a board when it is in a sprint of the board."""
        result = BoardPlan()
        by_project_id = {value.project["id"]: value for value in projects}
        exported_issue_ids = {issue["id"] for value in projects for issue in value.issues}
        now_ms = int(time.time() * 1000)
        for board in boards:
            shown = [by_project_id[p["id"]] for p in board.get("projects") or [] if p.get("id") in by_project_id]
            if not shown:
                continue
            prepared, sprints_by_issue = self._board_dumper.prepare(board, [value.plan for value in shown], now_ms)
            result.boards.append(prepared)
            for sprint in board.get("sprints") or []:
                for issue_id in sprint.get("issueIds") or []:
                    if issue_id in exported_issue_ids and prepared["id"] not in result.boards_by_issue.get(issue_id, []):
                        result.boards_by_issue.setdefault(issue_id, []).append(prepared["id"])
            for issue_id, sprint_ids in sprints_by_issue.items():
                if issue_id in exported_issue_ids:
                    result.sprints_by_issue.setdefault(issue_id, []).extend(sprint_ids)
        return result

    def _dump_project(self, project_ref: str, value: LoadedProject, context: IssueContext) -> ProjectDumpResult:
        project = value.project
        try:
            queue_path = os.path.join(self._data_path, "queues", value.queue_key)
            teams = {(p.get("team") or {}).get("id"): p.get("team") for p in self._fetcher.projects() if p.get("team")}
            access = queue_access(project, self._fetcher.role_assignments(), self._users, teams)
            restricted = any(is_restricted(issue) for issue in value.issues)
            if restricted and value.plan.restricted_component_taken:
                raise Exception(
                    f"Component {value.plan.restricted_component} of the issues visible to a part of users is a "
                    f"Subsystem value: set another name in configs/restricted_component.cfg"
                )
            self._queue_dumper.dump(project, value.plan, value.fields, queue_path, self._users, access, restricted)

            self._fetcher.fetch_issue_details(value.issues, bool(value.time_tracking.get("enabled")))
            issue_results = self._dump_issues(value.issues, queue_path, context)
            return ProjectDumpSuccess(project_ref, value.queue_key, issue_results)
        except Exception as exc:
            logger.exception(f"Failed to dump project {project_ref}")
            return ProjectDumpError(project_ref, exc)

    def _dump_issues(self, issues: List[Dict[str, Any]], queue_path: str, context: IssueContext) -> List[IssueDumpResult]:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [executor.submit(self._dump_issue, issue, queue_path, context) for issue in issues]
            return [future.result() for future in futures]

    def _dump_issue(self, issue: Dict[str, Any], queue_path: str, context: IssueContext) -> IssueDumpResult:
        issue_key = context.key_by_issue_id[issue["id"]]
        try:
            self._issue_dumper.dump(issue, os.path.join(queue_path, issue_key), issue_key, context)
            return IssueDumpSuccess(issue.get("idReadable") or issue["id"], issue_key)
        except Exception as exc:
            logger.exception(f"Failed to dump issue {issue.get('idReadable')}")
            return IssueDumpError(issue.get("idReadable") or issue["id"], exc)

    def _dump_boards(self, board_plan: BoardPlan) -> None:
        """An empty list replaces the boards of a previous export in the same directory."""
        dump_to_yaml(clean_empty(board_plan.boards), os.path.join(self._data_path, "boards.yaml"))

    def _dump_statuses(self) -> None:
        dump_to_yaml(
            [{"key": key, "name": status["name"], "type": status["type"]} for key, status in self._catalog.statuses.items()],
            os.path.join(self._data_path, "statuses.yaml"),
        )

    def _dump_resolutions(self) -> None:
        dump_to_yaml(
            [{"key": key, "name": name} for key, name in self._catalog.resolutions.items()],
            os.path.join(self._data_path, "resolutions.yaml"),
        )

    def _dump_issue_types(self) -> None:
        dump_to_yaml(
            [{"key": key, "name": name} for key, name in self._catalog.issue_types.items()],
            os.path.join(self._data_path, "issue_types.yaml"),
        )
