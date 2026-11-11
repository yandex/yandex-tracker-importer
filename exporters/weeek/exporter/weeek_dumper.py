import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .dump_results import (
    ProjectDumpError,
    ProjectDumpResult,
    ProjectDumpSuccess,
    TaskDumpError,
    TaskDumpResult,
    TaskDumpSuccess,
)
from .dumpers import (
    ISSUE_TYPES,
    RESOLUTION_KEY,
    RESOLUTION_NAME,
    BoardDumper,
    PortfolioDumper,
    ProjectDumper,
    ProjectToQueueDumper,
    TaskContext,
    TaskDumper,
    UserResolver,
    entity_dir,
)
from .fetcher import WeeekDataFetcher
from .utils import dump_to_yaml, earliest_tracker_datetime, to_tracker_datetime
from .weeek_client import SimpleWeeekClient

logger = logging.getLogger(__name__)


def check_queue_keys(projects_mapper: Dict[str, str]) -> None:
    """Rejects a queue key of several projects, ignoring case."""
    projects_by_key: Dict[str, List[str]] = defaultdict(list)
    for project_id, queue_key in projects_mapper.items():
        projects_by_key[queue_key.upper()].append(project_id)
    repeated = {key: ids for key, ids in projects_by_key.items() if len(ids) > 1}
    if repeated:
        raise ValueError(
            "projects.cfg: one queue key for several projects: "
            + "; ".join(f"{key} — {', '.join(ids)}" for key, ids in sorted(repeated.items()))
        )


class WeeekDumper:
    def __init__(
        self,
        client: SimpleWeeekClient,
        projects_mapper: Dict[str, str],
        users_mapping: Dict[str, str],
        data_path: str,
        max_workers: Optional[int] = None,
    ):
        self._data_path = data_path
        self._projects_mapper = projects_mapper
        check_queue_keys(projects_mapper)
        self._max_workers = max_workers or os.cpu_count()

        self._fetcher = WeeekDataFetcher(client, max_workers)
        self._fetcher.preload_reference_data()
        self._users = UserResolver(self._fetcher, users_mapping)

        workspace_id = self._fetcher.workspace_id
        self._queue_dumper = ProjectToQueueDumper(workspace_id)
        self._project_dumper = ProjectDumper(workspace_id)
        self._portfolio_dumper = PortfolioDumper(workspace_id)
        self._board_dumper = BoardDumper()
        self._task_dumper = TaskDumper(client, self._fetcher, self._users)
        self._boards: List[Dict[str, Any]] = []
        self._project_created_at: Dict[str, str] = {}
        self._exported_projects: List[Dict[str, Any]] = []
        self._portfolios_by_id: Dict[Any, Dict[str, Any]] = {}

    @property
    def failed_items(self) -> List[Tuple[str, str, str]]:
        return self._task_dumper.failed_items

    @property
    def unknown_users(self) -> Set[str]:
        return self._users.unknown_users

    @property
    def failed_reference_data(self) -> List[str]:
        return self._fetcher.failed_reference_data

    def dump(self) -> Iterator[ProjectDumpResult]:
        self._boards = []
        self._task_dumper.failed_items.clear()
        # projects and their tasks are loaded and numbered first: a subtask and a task of several projects
        # refer to tasks of other projects
        loaded: Dict[str, Any] = {}
        for project_id, queue_key in self._projects_mapper.items():
            try:
                project = self._fetcher.fetch_project(project_id)
                boards = self._fetcher.fetch_boards(project_id)
                tasks = self._fetcher.fetch_tasks(project_id)
                loaded[project_id] = (project, boards, tasks)
            except Exception as exc:
                logger.exception(f"Failed to load project {project_id}")
                loaded[project_id] = exc

        self._load_portfolios([value[0] for value in loaded.values() if not isinstance(value, Exception)])
        for project_id, value in loaded.items():
            if not isinstance(value, Exception):
                loaded[project_id] = (self._inherit_privacy(value[0]), *value[1:])
        # projects are written before tasks: a task refers to the projects of the export
        for project_id, value in loaded.items():
            if isinstance(value, Exception):
                continue
            try:
                self._write_project(*value)
            except Exception as exc:
                logger.exception(f"Failed to dump project {project_id}")
                loaded[project_id] = exc
        exported_ids = [project_id for project_id, value in loaded.items() if not isinstance(value, Exception)]
        tasks_by_project = self._home_tasks(exported_ids, loaded)
        key_by_task_id, created_at_by_task_id, children_by_task_id = self._index_tasks(tasks_by_project)
        exported_project_ids = set(exported_ids)

        for project_id, queue_key in self._projects_mapper.items():
            value = loaded.pop(project_id)
            if isinstance(value, Exception):
                yield ProjectDumpError(project_id, value)
                continue
            project, boards, _ = value
            yield self._dump_project(
                project, queue_key, boards, tasks_by_project[project_id],
                key_by_task_id, created_at_by_task_id, children_by_task_id, exported_project_ids,
            )
        self._dump_portfolios()
        self._dump_boards()
        self._dump_statuses()
        self._dump_resolutions()
        self._dump_issue_types()

    @staticmethod
    def _home_tasks(project_ids: List[str], loaded: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        """Tasks of every project, each task once: a task of several projects of the export belongs to the first
        of its locations among them."""
        exported = set(project_ids)
        tasks_by_id: Dict[Any, Dict[str, Any]] = {}
        for project_id in project_ids:
            for task in loaded[project_id][2]:
                tasks_by_id.setdefault(task["id"], task)
        result: Dict[str, List[Dict[str, Any]]] = {project_id: [] for project_id in project_ids}
        for task in tasks_by_id.values():
            home = next(
                (str(location.get("projectId")) for location in task.get("locations") or []
                 if str(location.get("projectId")) in exported),
                None,
            )
            if home is None:
                # a task listed in a project without its location there is exported in the first such project
                home = next(project_id for project_id in project_ids if task in loaded[project_id][2])
            result[home].append(task)
        return result

    def _index_tasks(
        self, tasks_by_project: Dict[str, List[Dict[str, Any]]]
    ) -> Tuple[Dict[Any, str], Dict[Any, Any], Dict[Any, List[Any]]]:
        """Issue keys, creation times and subtasks of all tasks of the export. Keys are numbered by creation."""
        key_by_task_id, created_at_by_task_id, children = {}, {}, defaultdict(list)
        for project_id, tasks in tasks_by_project.items():
            queue_key = self._projects_mapper[project_id]
            ordered = sorted(tasks, key=lambda task: (not task.get("createdAt"), task.get("createdAt") or "", task["id"]))
            tasks[:] = ordered
            for number, task in enumerate(ordered, start=1):
                key_by_task_id[task["id"]] = f"{queue_key}-{number}"
                created_at_by_task_id[task["id"]] = task.get("createdAt")
        for tasks in tasks_by_project.values():
            for task in tasks:
                if task.get("parentId") in key_by_task_id:
                    children[task["parentId"]].append(task["id"])
        return key_by_task_id, created_at_by_task_id, dict(children)

    def _write_project(self, project: Dict[str, Any], boards: List[Dict[str, Any]], listed_tasks: List[Dict[str, Any]]) -> None:
        """`project.yaml` of a project; its creation time is the creation of its earliest task, else the export time."""
        project_id = str(project.get("id"))
        self._project_created_at[project_id] = (
            earliest_tracker_datetime(task.get("createdAt") for task in listed_tasks)
            or to_tracker_datetime(datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        )
        project_path = os.path.join(self._data_path, "projects", entity_dir(project.get("name"), project_id, "project"))
        self._project_dumper.dump(
            project, project_path, self._project_created_at[project_id], self._users.owner, self._users.project_users(project),
            self._exported_portfolio(project.get("portfolioId"), f"project {project_id}"),
        )
        self._exported_projects.append(project)

    def _dump_project(
        self,
        project: Dict[str, Any],
        queue_key: str,
        boards: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        key_by_task_id: Dict[Any, str],
        created_at_by_task_id: Dict[Any, Any],
        children_by_task_id: Dict[Any, List[Any]],
        exported_project_ids: Set[str],
    ) -> ProjectDumpResult:
        project_id = str(project.get("id"))
        try:
            owner = self._users.owner
            plan = self._queue_dumper.plan(project, queue_key, boards, tasks)
            self._queue_dumper.dump(
                project, plan, os.path.join(self._data_path, "queues", queue_key), lead=owner,
                users=self._users.project_users(project),
            )
            for board in boards:
                self._boards.append(self._board_dumper.prepare(board, plan, single_board=len(boards) == 1))

            self._fetcher.fetch_comments(tasks)
            context = TaskContext(plan, key_by_task_id, created_at_by_task_id, children_by_task_id, exported_project_ids)
            task_results = self._dump_tasks(tasks, os.path.join(self._data_path, "queues", queue_key), context)
            return ProjectDumpSuccess(project_id, queue_key, task_results)
        except Exception as exc:
            logger.exception(f"Failed to dump project {project_id}")
            return ProjectDumpError(project_id, exc)

    def _dump_tasks(self, tasks: List[Dict[str, Any]], queue_path: str, context: TaskContext) -> List[TaskDumpResult]:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [executor.submit(self._dump_task, task, queue_path, context) for task in tasks]
            return [future.result() for future in futures]

    def _dump_task(self, task: Dict[str, Any], queue_path: str, context: TaskContext) -> TaskDumpResult:
        issue_key = context.key_by_task_id[task["id"]]
        try:
            self._task_dumper.dump(task, os.path.join(queue_path, issue_key), issue_key, context)
            return TaskDumpSuccess(str(task["id"]), issue_key)
        except Exception as exc:
            logger.exception(f"Failed to dump task {task['id']}")
            return TaskDumpError(str(task["id"]), exc)

    def _load_portfolios(self, projects: List[Dict[str, Any]]) -> None:
        """Portfolios by id, loaded when a project of the export is in a portfolio."""
        self._portfolios_by_id = {}
        if any(project.get("portfolioId") for project in projects):
            self._portfolios_by_id = {portfolio.get("id"): portfolio for portfolio in self._fetcher.portfolios()}

    def _closed_by_portfolio(self, portfolio_id: Any) -> bool:
        """Whether a portfolio or one of its parents is private: WEEEK closes the nested portfolios and projects of a
        private portfolio, and the API marks only the portfolio itself as private. A portfolio that is not loaded
        counts as private: its privacy is unknown."""
        seen = set()
        while portfolio_id and portfolio_id not in seen:
            seen.add(portfolio_id)
            portfolio = self._portfolios_by_id.get(portfolio_id)
            if portfolio is None or portfolio.get("isPrivate"):
                return True
            portfolio_id = portfolio.get("parentId")
        return False

    def _inherit_privacy(self, project: Dict[str, Any]) -> Dict[str, Any]:
        """The project; a project in a private portfolio is private."""
        if not project.get("isPrivate") and self._closed_by_portfolio(project.get("portfolioId")):
            logger.info(f"Project {project.get('id')} is in a private portfolio, it is exported as private")
            return dict(project, isPrivate=True)
        return project

    def _exported_portfolio(self, portfolio_id: Any, child: str) -> Any:
        """`portfolio_id` when the portfolio is loaded; a missing portfolio is reported unless portfolios are not
        loaded at all."""
        if not portfolio_id:
            return None
        if portfolio_id in self._portfolios_by_id:
            return portfolio_id
        if self._fetcher.portfolios_failed:
            return None
        message = f"portfolio {portfolio_id} of {child} is not found"
        logger.warning(message)
        if message not in self._fetcher.failed_reference_data:
            self._fetcher.failed_reference_data.append(message)
        return None

    def _dump_portfolios(self) -> None:
        """Portfolios of the exported projects and their parents."""
        created_at: Dict[Any, List[str]] = defaultdict(list)
        for project in self._exported_projects:
            portfolio_id, seen = self._exported_portfolio(project.get("portfolioId"), f"project {project.get('id')}"), set()
            while portfolio_id and portfolio_id not in seen:
                seen.add(portfolio_id)
                created_at[portfolio_id].append(self._project_created_at[str(project.get("id"))])
                portfolio_id = self._exported_portfolio(self._portfolios_by_id[portfolio_id].get("parentId"), f"portfolio {portfolio_id}")
        for portfolio_id, dates in created_at.items():
            portfolio = self._portfolios_by_id[portfolio_id]
            path = os.path.join(self._data_path, "portfolios", entity_dir(portfolio.get("name"), portfolio_id, "portfolio"))
            parent_id = portfolio.get("parentId") if portfolio.get("parentId") in self._portfolios_by_id else None
            if self._closed_by_portfolio(portfolio_id):
                portfolio = dict(portfolio, isPrivate=True)
            self._portfolio_dumper.dump(portfolio, path, earliest_tracker_datetime(dates), self._users.owner, parent_id)

    def _dump_boards(self) -> None:
        if self._boards:
            dump_to_yaml(self._project_dumper._clean_empty(self._boards), os.path.join(self._data_path, "boards.yaml"))

    def _dump_statuses(self) -> None:
        dump_to_yaml(
            [
                {"key": key, "name": {"en": status["name"], "ru": status["name"]}, "type": status["type"]}
                for key, status in self._queue_dumper.collected_statuses.items()
            ],
            os.path.join(self._data_path, "statuses.yaml"),
        )

    def _dump_resolutions(self) -> None:
        dump_to_yaml([{"key": RESOLUTION_KEY, "name": RESOLUTION_NAME}], os.path.join(self._data_path, "resolutions.yaml"))

    def _dump_issue_types(self) -> None:
        dump_to_yaml(
            [{"key": key, "name": name} for key, name in ISSUE_TYPES.values()],
            os.path.join(self._data_path, "issue_types.yaml"),
        )
