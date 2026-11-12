import logging
from functools import lru_cache
from typing import Any, Dict, List, Iterable, Optional

from .bitrix_client import BitrixApiError, SimpleBitrixClient

logger = logging.getLogger(__name__)


class BitrixDataFetcher:
    _CHAT_PAGE_SIZE = 50
    # parent ids per request of subtasks
    _SUBTASKS_CHUNK = 50
    # errors of disk.file.get about the file itself; other errors (scope, credentials) fail the request
    _FILE_UNAVAILABLE = ("ERROR_NOT_FOUND", "ACCESS_DENIED")

    def __init__(self, client: SimpleBitrixClient):
        self._client = client

    def fetch_projects_by_ids(self, ids: Iterable[str]) -> Any:
        """Groups from ``socialnetwork.api.workgroup.get`` with ``DATE_CREATE`` from ``sonet_group.get``.

        ``workgroup.get`` returns the creation date in the portal locale format; ``sonet_group.get`` returns ISO 8601.
        """
        try:
            ids = [str(project_id) for project_id in ids]
            if not ids:
                return []
            results = self._batch_skipping(
                "socialnetwork.api.workgroup.get", [{"params": {"groupId": project_id}} for project_id in ids]
            )
            projects = self._skip_empty(results, ids, "project")
            created = {
                str(group["ID"]): group.get("DATE_CREATE")
                for group in self._client.get_list("sonet_group.get", {"FILTER": {"ID": ids}, "IS_ADMIN": "Y"})
            }
            for project in projects:
                project["DATE_CREATE"] = created.get(str(project["ID"]))
            return projects

        except Exception:
            logger.exception(f"Failed to fetch projects by ids: {ids}")
            return None

    def fetch_project_ids_by_filter(self, filter: Dict[str, Any]) -> Optional[List[str]]:
        try:
            return [
                str(project["id"])
                for project in self._client.get_list(
                    "socialnetwork.api.workgroup.list",
                    {"select": ["ID"], "filter": filter, "order": {"ID": "ASC"}},
                    key="workgroups",
                )
            ]
        except Exception:
            logger.exception(f"Failed to fetch projects by filter: {filter}")
            return None

    def fetch_task_ids_by_filter(self, filter: Dict[str, Any]) -> Optional[List[str]]:
        try:
            return [
                str(task["id"])
                for task in self._client.get_list(
                    "tasks.task.list", {"select": ["ID"], "filter": filter, "order": {"ID": "ASC"}}, key="tasks"
                )
            ]
        except Exception:
            logger.exception(f"Failed to fetch tasks by filter: {filter}")
            return None

    def fetch_subtasks(self, parent_ids: List[str]) -> Optional[Dict[str, str]]:
        """Parent task id by subtask id for subtasks of ``parent_ids``."""
        subtasks: Dict[str, str] = {}
        try:
            for start in range(0, len(parent_ids), self._SUBTASKS_CHUNK):
                for task in self._client.get_list(
                    "tasks.task.list",
                    {
                        "select": ["ID", "PARENT_ID"],
                        "filter": {"PARENT_ID": parent_ids[start:start + self._SUBTASKS_CHUNK]},
                        "order": {"ID": "ASC"},
                    },
                    key="tasks",
                ):
                    subtasks[str(task["id"])] = str(task["parentId"])
        except Exception:
            logger.exception(f"Failed to fetch subtasks of {len(parent_ids)} tasks from task {parent_ids[0]}")
            return None
        return subtasks

    def fetch_tasks_by_ids(self, task_ids: List[str]) -> Any:
        try:
            select_fields = [
                "ID",
                "TITLE",
                "PARENT_ID",
                "DESCRIPTION",
                "PRIORITY",
                "STATUS",
                "GROUP_ID",
                "STAGE_ID",
                "CREATED_BY",
                "CREATED_DATE",
                "CHANGED_BY",
                "CHANGED_DATE",
                "RESPONSIBLE_ID",
                "CLOSED_BY",
                "CLOSED_DATE",
                "AUDITORS",
                "ACCOMPLICES",
                "DEADLINE",
                "CHECKLIST",
                "FLOW_ID",
                "TAGS",
                "TIME_ESTIMATE",
                "START_DATE_PLAN",
                "END_DATE_PLAN",
                "CHAT_ID",
                "UF_TASK_WEBDAV_FILES",
                "UF_*",
            ]
            results = self._batch_skipping(
                "tasks.task.get",
                [{"taskId": task_id, "select": select_fields} for task_id in task_ids],
            )
            return self._skip_empty([(result or {}).get("task") for result in results], task_ids, "task")
        except Exception:
            logger.exception(f"Failed to fetch tasks by ids: {task_ids}")
            return None

    def fetch_task_fields(self) -> Any:
        try:
            return self._client.call("tasks.task.getFields")["fields"]
        except Exception:
            logger.exception("Failed to fetch user fields")
            return None

    def fetch_task_user_fields(self) -> Any:
        """Task user field definitions: type, ``MULTIPLE`` and ``LIST`` values of enumerations."""
        try:
            return self._client.get_list("task.item.userfield.getlist", {"ORDER": {"ID": "ASC"}})
        except Exception:
            logger.exception("Failed to fetch user field definitions")
            return None

    def fetch_tasks_comments(self, task_ids: List[str]) -> Any:
        try:
            return self._fetch_by_task_ids("task.commentitem.getlist", task_ids)
        except Exception:
            logger.exception(f"Failed to fetch tasks' comments by ids: {task_ids}")
            return None

    def fetch_tasks_results(self, task_ids: List[str]) -> Any:
        """Results of tasks by task id; ``files`` of a result are attached object ids. A task whose results the API
        rejects gets no results: they complement the task, which is dumped without them."""
        try:
            results = self._batch_skipping("tasks.task.result.list", [{"taskId": task_id} for task_id in task_ids])
            return {task_id: [] if result is None else result for task_id, result in zip(task_ids, results)}
        except Exception:
            logger.exception(f"Failed to fetch tasks' results by ids: {task_ids}")
            return None

    def fetch_tasks_worklogs(self, task_ids: List[str]) -> Any:
        """Time records by task id; ``task.elapseditem.getlist`` returns all records of a task in one response."""
        try:
            return self._fetch_by_task_ids("task.elapseditem.getlist", task_ids)
        except Exception:
            logger.exception(f"Failed to fetch tasks' worklogs by ids: {task_ids}")
            return None

    def fetch_chat_messages(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """All messages of a task chat, oldest first.

        ``im.dialog.messages.get`` returns up to 50 messages from the newest; ``LAST_ID`` pages back.
        """
        try:
            messages: Dict[int, Dict[str, Any]] = {}
            last_id = None
            while True:
                params: Dict[str, Any] = {"DIALOG_ID": f"chat{chat_id}", "LIMIT": self._CHAT_PAGE_SIZE}
                if last_id is not None:
                    params["LAST_ID"] = last_id
                page = self._client.call("im.dialog.messages.get", params) or {}
                page_messages = [m for m in page.get("messages") or [] if int(m["id"]) not in messages]
                for message in page_messages:
                    messages[int(message["id"])] = message
                if len(page.get("messages") or []) < self._CHAT_PAGE_SIZE or not page_messages:
                    break
                last_id = min(int(message["id"]) for message in page_messages)
            return {"messages": [messages[message_id] for message_id in sorted(messages)]}
        except Exception:
            logger.exception(f"Failed to fetch messages of chat {chat_id}")
            return None

    def fetch_disk_files(self, file_ids: List[str]) -> Any:
        """Disk files by id: ``NAME``, ``SIZE``, ``DOWNLOAD_URL``. A file the API reports as not found or not
        accessible maps to ``None``; any other failure returns ``None`` for the whole call."""
        try:
            files = self._batch_skipping(
                "disk.file.get", [{"id": file_id} for file_id in file_ids], skipped_codes=self._FILE_UNAVAILABLE
            )
            return dict(zip(file_ids, files))
        except Exception:
            logger.exception(f"Failed to fetch disk files by ids: {file_ids}")
            return None

    def fetch_kanban_stages(self, group_id: str) -> Any:
        try:
            return [self._client.call("task.stages.get", {"entityId": group_id})]
        except Exception:
            logger.exception("Failed to fetch kanban stages")
            return None

    # page size of the scrum lists: they take start in steps of 50 and return no next
    _SCRUM_PAGE_SIZE = 50

    def _get_scrum_list(self, method: str, params: Dict[str, Any]) -> List[Any]:
        """All records of a scrum list, page after page while a page is full."""
        items: List[Any] = []
        seen_ids = set()
        start = 0
        while True:
            page = self._client.call(method, dict(params, order={"ID": "ASC"}, start=start)) or []
            if not isinstance(page, list):
                raise BitrixApiError(f"{method}: expected a list, got {type(page).__name__}")
            page_ids = {str(item.get("id")) for item in page if isinstance(item, dict)}
            if page and page_ids <= seen_ids:
                raise BitrixApiError(f"{method}: pagination does not advance")
            seen_ids |= page_ids
            items.extend(page)
            if len(page) < self._SCRUM_PAGE_SIZE:
                return items
            start += self._SCRUM_PAGE_SIZE

    def fetch_scrum(self, group_id: str) -> Any:
        """Backlog id, sprints with the stages of their boards, and epics with their files of a scrum group. The backlog
        id is ``None`` if the backlog is not returned."""
        try:
            try:
                backlog = self._client.call("tasks.api.scrum.backlog.get", {"id": group_id}) or {}
            except BitrixApiError as e:
                if not e.rejected:
                    raise
                logger.warning(f"Backlog of scrum group {group_id} is not returned: {e}")
                backlog = {}
            sprints = self._get_scrum_list("tasks.api.scrum.sprint.list", {"filter": {"GROUP_ID": group_id}})
            for sprint in sprints:
                sprint["stages"] = self._client.call("tasks.api.scrum.kanban.getStages", {"sprintId": sprint["id"]}) or []
            epic_ids = [
                str(epic["id"])
                for epic in self._get_scrum_list("tasks.api.scrum.epic.list", {"filter": {"GROUP_ID": group_id}})
            ]
            epics = self._client.batch("tasks.api.scrum.epic.get", [{"id": epic_id} for epic_id in epic_ids])
            return {
                "backlog_id": backlog.get("id"),
                "sprints": sprints,
                "epics": self._skip_empty(epics, epic_ids, "epic"),
            }
        except Exception:
            logger.exception(f"Failed to fetch sprints and epics of scrum group {group_id}")
            return None

    # error of tasks.api.scrum.task.get for a task that is not an item of a scrum backlog or sprint
    _SCRUM_TASK_NOT_FOUND = "Task not found"

    def fetch_scrum_tasks(self, task_ids: List[str]) -> Any:
        """Scrum data of tasks of scrum groups: ``entityId`` (sprint or backlog), ``storyPoints``, ``epicId``. A task
        that is not an item of the backlog or a sprint maps to ``{}``, a task rejected otherwise maps to ``None``; a
        failed request returns ``None``."""
        try:
            try:
                results = self._client.batch("tasks.api.scrum.task.get", [{"id": task_id} for task_id in task_ids])
            except BitrixApiError as e:
                if not e.rejected or e.results is None:
                    raise
                results = e.results
                for index, failure in e.failures.items():
                    if self._SCRUM_TASK_NOT_FOUND in str(failure.get("error_description")):
                        logger.warning(f"Task {task_ids[index]} is not an item of a scrum backlog or sprint")
                        results[index] = {}
                    else:
                        logger.warning(f"Scrum data of task {task_ids[index]} is not returned: {failure}")
            return dict(zip(task_ids, results))
        except Exception:
            logger.exception(f"Failed to fetch scrum data of tasks: {task_ids}")
            return None

    def fetch_departments(self) -> Any:
        try:
            return self._client.get_list("department.get", {"sort": "ID", "order": "ASC"})
        except Exception:
            logger.exception("Failed to fetch departments")
            return None

    def fetch_current_user(self) -> Any:
        try:
            return self._client.call("user.current")
        except Exception:
            logger.exception("Failed to fetch current user")
            return None

    def fetch_users(self, users: List[str]) -> Any:
        try:
            return self._client.get_list("user.get", {"ID": users, "sort": "ID", "order": "ASC"})
        except Exception:
            logger.exception(f"Failed to fetch users: {users}")
            return None

    def fetch_attachments(self, project_id: str) -> Any:
        files = []
        folders_to_process = []

        storages = self._client.get_list(
            "disk.storage.getlist", {"filter": {"ENTITY_TYPE": "group", "ENTITY_ID": project_id}}
        )

        def process_items(items):
            for item in items:  # type: ignore
                if item["TYPE"] == "folder":
                    folders_to_process.append(item["ID"])
                elif item["TYPE"] == "file":
                    files.append(item)

        # Without an explicit order, pages of a folder listing overlap.
        order = {"ID": "ASC"}
        for children in self._client.batch(
            "disk.storage.getchildren", [{"id": storage["ID"], "order": order} for storage in storages]
        ):
            process_items(children or [])

        while folders_to_process:
            current_batch = folders_to_process[:]
            folders_to_process = []

            for children in self._client.batch(
                "disk.folder.getchildren", [{"id": folder_id, "order": order} for folder_id in current_batch]
            ):
                process_items(children or [])

        return files

    def fetch_attachments_by_ids(self, attachment_ids: List[str]) -> Any:
        try:
            return self._batch_skipping(
                "disk.attachedObject.get",
                [{"id": attachment_id} for attachment_id in attachment_ids],
                skipped_codes=self._FILE_UNAVAILABLE,
            )
        except Exception:
            logger.exception(f"Failed to fetch attachments by ids: {attachment_ids}")
            return None

    @lru_cache(maxsize=None)
    def fetch_flow_by_id(self, flow_id: str) -> Any:
        try:
            return self._client.call("tasks.flow.Flow.get", {"flowId": flow_id})
        except Exception:
            logger.exception(f"Failed to fetch flow by ids: {flow_id}")
            return None

    @property
    def base_url(self) -> str:
        return self._client.portal_url

    def _skip_empty(self, results: List[Any], ids: List[str], kind: str) -> List[Any]:
        for object_id, result in zip(ids, results):
            if not result:
                logger.warning(f"Bitrix24 returned no data for {kind} {object_id}, skipping it")
        return [result for result in results if result]

    def _fetch_by_task_ids(self, method: str, task_ids: List[str]) -> Dict[str, Any]:
        results = self._batch_skipping(method, [{"TASKID": task_id} for task_id in task_ids])
        return dict(zip(task_ids, results))

    def _batch_skipping(
        self, method: str, items: List[Dict[str, Any]], skipped_codes: Optional[Iterable[str]] = None
    ) -> List[Any]:
        """Results of ``batch`` with ``None`` for a command the API rejects, with a warning. With ``skipped_codes``,
        a rejection with another error code raises."""
        try:
            return self._client.batch(method, items)
        except BitrixApiError as e:
            if not e.rejected or e.results is None:
                raise
            for index, failure in e.failures.items():
                if skipped_codes is not None and failure.get("error") not in skipped_codes:
                    raise
                params = {key: value for key, value in items[index].items() if key != "select"}
                logger.warning(f"{method} {params} is rejected: {failure.get('error')} {failure.get('error_description')}")
            return e.results
