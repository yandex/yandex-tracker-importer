import glob
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
import yaml
from tqdm import tqdm
from typing import Any, Dict, Callable, List, Optional, Set

from .base_dumper import BaseDumper, UsersFetchError
from .bitrix_client import SimpleBitrixClient
from .queues_dumper import QueuesDumper
from .utils import parse_datetime, to_tracker_datetime
from .workflow import (
    INITIAL_STATUS,
    RESOLUTION_BY_BITRIX_STATUS,
    SPRINT_STATUS_BY_BITRIX_STATUS,
    STATUS_BY_BITRIX_STATUS,
    board_columns,
)


log = logging.getLogger(__name__)

# umask of the process, read once: reading it sets it for a moment
_UMASK = os.umask(0)
os.umask(_UMASK)


class TasksToIssuesDumper(BaseDumper):
    READABLE_DATE_FORMAT = "%d-%m-%Y %H:%M:%S"
    _priority_map = {
        "0": "minor",
        "1": "normal",
        "2": "critical",
    }
    _image_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".tif",
        ".tiff",
        ".avif",
        ".jfif",
        ".pjpeg",
        ".pjp",
        ".webp",
    }

    def __init__(self, client: SimpleBitrixClient, dest_path: str):
        super().__init__(client, dest_path)
        self._queues_dumper = QueuesDumper(client, dest_path)
        self._kanban_statuses_cache = dict()
        self._used_projects = set()
        self._user_fields_cache = self._get_user_fields()
        self._used_user_fields: Set[str] = set()
        # task id -> key of a task dumped in this run
        self._task_id_to_key_cache = dict()
        # task id -> key of a task dumped by an earlier run; such a task is dumped again when requested
        self._earlier_task_keys: Dict[str, str] = dict()
        self._task_created_at: Dict[str, str] = dict()
        # disk file id -> disk file, for references to files the task holds as copies
        self._disk_files: Dict[str, Dict[str, Any]] = dict()
        # disk file ids of references already reported as unresolved
        self._unresolved_references: Set[str] = set()
        # task id -> issue.yaml paths in data/queues, built on the first lookup of a parent from an earlier run
        self._dumped_tasks_index: Optional[Dict[str, List[str]]] = None
        # group id -> group, sprints and epics of a scrum group; empty for a group that is not a scrum
        self._scrum_groups: Dict[str, Dict[str, Any]] = dict()
        # board id -> board of a scrum group for boards.yaml
        self._boards: Dict[str, Dict[str, Any]] = dict()
        # groups whose epics are dumped
        self._dumped_epic_groups: Set[str] = set()
        # groups whose scrum data or epics failed: not requested again in this run
        self._failed_scrum_groups: Set[str] = set()
        self._failed_epic_groups: Set[str] = set()
        # task id -> id of its parent task that is not exported: the task is dumped without the link to the parent
        self._unlinked_tasks: Dict[str, str] = dict()

    @property
    def _filename(self) -> str:
        return "issue.yaml"

    def dump_by_filter(self, tasks_filter: Dict[str, Any]) -> Dict[str, bool]:
        """Dump tasks matching the filter; return the dump result by task id, ``False`` for every task not dumped."""
        if self._user_fields_cache is None:
            return {"task user field definitions": False}

        task_ids = self._fetcher.fetch_task_ids_by_filter(tasks_filter)
        if task_ids is None:
            return {f"tasks by filter {tasks_filter}": False}

        tasks = self._get_tasks(task_ids)
        if tasks is None:
            return {task_id: False for task_id in task_ids}

        returned_ids = {str(task["id"]) for task in tasks}
        missing_ids = {task_id for task_id in task_ids if task_id not in returned_ids}
        results = {task_id: False for task_id in missing_ids}

        try:
            self._preload_users_to_cache(tasks)
        except UsersFetchError:
            log.exception("Failed to preload users, they are requested per task")

        results.update(self._dump_tasks(tasks, missing_ids))
        results.update({f"epics of group {group_id}": False for group_id in sorted(self._failed_epic_groups)})
        return results

    def relink_tasks(self, parent_ids: List[str]) -> Dict[str, bool]:
        """Dump again the subtasks dumped without the link to a parent task dumped later: in this run before the
        parent, or by an earlier run, if the parent is one of ``parent_ids``. Return the dump result by task id; a task
        that fails keeps its earlier directory and is not dumped again in this run: the next run dumps the group of
        the parent again and finds the task among the subtasks of the parent."""
        results: Dict[str, bool] = {}
        parents = {}
        if parent_ids:
            subtasks = self._fetcher.fetch_subtasks(parent_ids)
            if subtasks is None:
                results[f"subtasks of {len(parent_ids)} tasks from task {parent_ids[0]}"] = False
            else:
                for task_id, parent_id in subtasks.items():
                    if task_id in self._task_id_to_key_cache:
                        continue
                    try:
                        if self._dumped_without_parent_link(task_id, parent_id):
                            parents[task_id] = parent_id
                    except Exception:
                        log.exception(f"Failed to check the link of task {task_id} to its parent task {parent_id}")
                        results[task_id] = False
        parents.update(
            (task_id, parent_id)
            for task_id, parent_id in self._unlinked_tasks.items()
            if parent_id in self._task_id_to_key_cache
        )
        if not parents:
            return results
        log.info(f"Tasks {', '.join(parents)} are dumped again with the link to the parent task")
        # tasks in the cache are not dumped again
        keys = {task_id: self._task_id_to_key_cache.pop(task_id, None) for task_id in parents}
        tasks = self._get_tasks(list(parents))
        if tasks is None:
            results.update({task_id: False for task_id in parents})
        else:
            returned_ids = {str(task["id"]) for task in tasks}
            results.update({task_id: False for task_id in parents if task_id not in returned_ids})
            results.update(self._dump_tasks(tasks))
        for task_id in parents:
            self._unlinked_tasks.pop(task_id, None)
            if not results.get(task_id) and keys.get(task_id):
                self._task_id_to_key_cache[task_id] = keys[task_id]
        return results

    def _dumped_without_parent_link(self, task_id: str, parent_id: str) -> bool:
        """Whether a task dumped by an earlier run has no link to its parent task. Raises if its ``issue.yaml`` is not
        readable."""
        if self._find_dumped_task(task_id) is None:
            return False
        parent_key = self._task_id_to_key_cache.get(parent_id) or self._earlier_task_keys.get(parent_id)
        path = self._dumped_tasks_index[task_id][0]
        with open(path, encoding="utf-8") as f:
            issue = yaml.safe_load(f) or {}
        return not any(
            isinstance(link, dict) and link.get("relationship") == "is subtask for" and link.get("issue") == parent_key
            for link in issue.get("links") or []
        )

    @property
    def unlinked_tasks(self) -> Dict[str, str]:
        return self._unlinked_tasks

    def _dump_tasks(self, tasks: List[Dict[str, Any]], missing_ids: Optional[Set[str]] = None) -> Dict[str, bool]:
        """Dump tasks, a parent before its subtasks.

        A subtask fails if its parent failed to dump or is requested but not returned by the API (``missing_ids``).
        """
        tasks_dict = {str(task["id"]): task for task in tasks}
        missing_ids = missing_ids or set()
        res_dump = {}

        def dump_task(task_id: str, update_callback: Callable[[], None]) -> None:
            task_data = tasks_dict[task_id]
            try:
                success = True
                if task_data.get("flowId"):
                    success = self._queues_dumper.dump_by_flow_id(task_data["flowId"])
                else:
                    success = self._queues_dumper.dump_default_queue()

                if not success:
                    raise Exception(f"Failed to dump queue for task: {task_data["title"]}")

                self._load_chat(task_data)
                for related in ("comments", "worklogs", "results", "chat", "scrum"):
                    if task_data.get(related) is None:
                        raise Exception(f"Failed to fetch {related} of task {task_id}")
                task_data["chat"] = self._without_result_sources(task_data["chat"], task_data["results"])

                parent_id = self._parent_id(task_data)
                if parent_id in missing_ids:
                    raise Exception(f"Parent task {parent_id} is not returned by the API")
                if parent_id in tasks_dict and parent_id not in self._task_id_to_key_cache:
                    if parent_id not in res_dump:
                        dump_task(parent_id, update_callback)
                    if not res_dump.get(parent_id):
                        raise Exception(f"Parent task {parent_id} is not dumped")

                self._dump_epics(task_data)
                attachments = self._collect_attachments(task_data)
                issue_data = self._prepare_issue_data(task_data, attachments)

                queue_key = issue_data["key"].split("-")[0]
                dump_path = os.path.join(self._dest_path, "queues", queue_key, issue_data["key"])

                self._save_with_attachments(issue_data, attachments, dump_path)
                self._used_user_fields.update(key for key in issue_data if key in self._user_fields_cache)
                log.info(f"Dumped issue: '{task_data["title"]}'")
                self._task_id_to_key_cache[task_id] = issue_data["key"]
                self._task_created_at[task_id] = task_data["createdDate"]
                res_dump[task_id] = True
            except Exception:
                log.exception(f"Failed to dump task {task_id}: '{task_data.get("title")}'")
                res_dump[task_id] = False
            finally:
                # release the chat of a processed task
                task_data.pop("chat", None)

            update_callback()

        with tqdm(total=len(tasks), desc="Dumping tasks") as pbar:
            for task_id in tasks_dict:
                if task_id not in res_dump and task_id not in self._task_id_to_key_cache:
                    dump_task(task_id, update_callback=pbar.update)

        return res_dump

    def _save_with_attachments(
        self, issue_data: Dict[str, Any], attachments: Dict[str, Dict[str, Any]], dump_path: str
    ) -> None:
        """Assemble the directory of an issue, ``issue.yaml`` and attachments, in a temporary directory next to the data
        directory and put it in place of ``dump_path``: a failed download leaves no directory of the issue for the
        import, and the directory of an earlier run is replaced whole or kept."""
        temp_path = tempfile.mkdtemp(prefix=".issue-", dir=os.path.dirname(os.path.abspath(self._dest_path)))
        # mkdtemp creates the directory for its owner only; the issue directory gets the usual permissions
        os.chmod(temp_path, 0o777 & ~_UMASK)
        backup_path = temp_path + ".previous"
        try:
            if attachments:
                self._download_all_attachments_threaded(list(attachments.values()), os.path.join(temp_path, "attachments"))
            self._clean_and_save_data(issue_data, temp_path)
            os.makedirs(os.path.dirname(dump_path), exist_ok=True)
            if os.path.exists(dump_path):
                os.replace(dump_path, backup_path)
            try:
                os.replace(temp_path, dump_path)
            except OSError:
                if os.path.exists(backup_path):
                    os.replace(backup_path, dump_path)
                raise
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)
            shutil.rmtree(backup_path, ignore_errors=True)

    def _load_chat(self, task_data: Dict[str, Any]) -> None:
        """Fetch the chat of a task when the task is dumped; ``None`` marks a failed request."""
        if "chat" in task_data:
            return
        chat_id = task_data.get("chatId")
        has_chat = chat_id and str(chat_id) != "0"
        task_data["chat"] = self._fetcher.fetch_chat_messages(chat_id) if has_chat else {"messages": []}

    def _get_tasks(self, task_ids: List[str]) -> Optional[List[Dict[str, Any]]]:
        tasks = self._fetcher.fetch_tasks_by_ids(task_ids)
        if tasks is None:
            return None

        task_ids = [str(task["id"]) for task in tasks]

        comments_data = self._fetcher.fetch_tasks_comments(task_ids)
        if comments_data is None:
            return None

        worklogs_data = self._fetcher.fetch_tasks_worklogs(task_ids)
        if worklogs_data is None:
            return None

        results_data = self._fetcher.fetch_tasks_results(task_ids)
        if results_data is None:
            return None

        for task in tasks:
            # None marks a failed request for this task; the task fails to dump
            task["comments"] = comments_data.get(str(task["id"]))
            task["worklogs"] = worklogs_data.get(str(task["id"]))
            task["results"] = results_data.get(str(task["id"]))

        self._add_scrum_data(tasks)
        return tasks

    def _prepare_issue_data(self, task_data: Dict[str, Any], attachments: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        project_id = task_data["groupId"] if task_data["groupId"] != "0" else None

        issue_data = {
            "key": self._get_issue_key(task_data),
            "summary": task_data["title"],
            "description": self._prepare_description(task_data["description"], attachments),
            "type": "task",
            "status": self._map_status(task_data),
            **self._prepare_resolution(task_data),
            "kanbanStatus": self._map_kanban_status(task_data),
            "priority": self._priority_map.get(task_data["priority"]),
            "createdBy": self._get_user(task_data["createdBy"]),
            "createdAt": self._datetime_to_iso(task_data["createdDate"]),
            "updatedBy": self._get_user(task_data["changedBy"]),
            "updatedAt": self._datetime_to_iso(task_data["changedDate"]),
            "assignee": self._get_user(task_data["responsibleId"]),
            "followers": [self._get_user(auditor) for auditor in task_data["auditors"]],
            "accomplices": [self._get_user(user_id) for user_id in task_data["accomplices"]],
            "deadline": self._datetime_to_date(task_data["deadline"]),
            "start": self._datetime_to_date(task_data.get("startDatePlan")),
            "end": self._datetime_to_date(task_data.get("endDatePlan")),
            "originalEstimation": self._prepare_estimation(task_data.get("timeEstimate")),
            "worklogs": self._prepare_worklogs(task_data.get("worklogs") or []),
            "tags": self._prepare_tags(task_data.get("tags")),
            "checklistItems": self._prepare_checklist_items_data(task_data["checklist"], task_data),
            "comments": self._prepare_comments_data(task_data["comments"], attachments)
            + self._prepare_chat_comments(task_data.get("chat") or {}, attachments)
            + self._prepare_result_comments(task_data.get("results") or [], attachments),
            "project": {"primary": self._project_dump_id(project_id) if project_id else None},
            "attachments": [
                {
                    "name": attachment["NAME"],
                    "filename": self._get_attachment_filename(attachment),
                    "createdAt": self._datetime_to_iso(attachment["CREATE_TIME"]),
                    "createdBy": self._get_user(attachment["CREATED_BY"]),
                }
                for attachment in attachments.values()
            ],
            **self._prepare_user_fields(task_data),
            **self._prepare_scrum_fields(task_data),
            "links": (self._prepare_links(task_data) or []) + self._prepare_epic_link(task_data),
        }

        issue_data["updatedAt"] = self._latest_datetime(
            [issue_data["updatedAt"]]
            + [comment["createdAt"] for comment in issue_data["comments"]]
            + [attachment["createdAt"] for attachment in issue_data["attachments"]]
            + [link["createdAt"] for link in issue_data["links"] or []]
        )

        if project_id:
            self._used_projects.add(project_id)

        return issue_data

    @staticmethod
    def _latest_datetime(values: List[Optional[str]]) -> Optional[str]:
        """The latest of dates in the Tracker format: the import rejects a comment, file or link later than the
        ``updatedAt`` of its issue."""
        values = [value for value in values if value]
        return max(values, key=parse_datetime) if values else None

    @staticmethod
    def _prepare_tags(tags: Any) -> List[str]:
        """Tag titles; ``tasks.task.get`` returns tags as ``{id: {"id", "title"}}``."""
        if isinstance(tags, dict):
            tags = list(tags.values())
        return [tag.get("title") if isinstance(tag, dict) else str(tag) for tag in tags or [] if tag]

    @staticmethod
    def _prepare_estimation(seconds: Any) -> Optional[int]:
        """Estimation in milliseconds; Bitrix24 stores ``TIME_ESTIMATE`` in seconds, 0 means none."""
        return int(seconds) * 1000 if seconds and int(seconds) > 0 else None

    def _prepare_worklogs(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Time records: start is the date the time is recorded for, duration in milliseconds, author is ``USER_ID``;
        the creation time is the timer stop or, for a manual record, the moment of entry."""
        worklogs = []
        for record in records:
            if int(record.get("SECONDS") or 0) <= 0:
                continue
            if not record.get("CREATED_DATE"):
                log.warning(f"Time record {record.get('ID')} of task {record.get('TASK_ID')} has no date, skipped")
                continue
            worklogs.append({
                "start": self._datetime_to_iso(record["CREATED_DATE"]),
                "duration": int(record["SECONDS"]) * 1000,
                "comment": unescape(record.get("COMMENT_TEXT") or "") or None,
                "createdAt": self._record_creation_time(record),
                "createdBy": self._get_user(record.get("USER_ID")),
            })
        return worklogs

    def _record_creation_time(self, record: Dict[str, Any]) -> Optional[str]:
        for key in ("DATE_STOP", "DATE_START"):
            try:
                created_at = self._datetime_to_iso(record.get(key))
            except (TypeError, ValueError):
                log.warning(f"Time record {record.get('ID')} of task {record.get('TASK_ID')} has an invalid {key}")
                continue
            if created_at:
                return created_at
        return None

    # system message of a result made from a chat message: «… добавил [URL=…entityId=<result id>]…[/URL] на основании
    # [CONTEXT=chat<chat id>/<message id>]сообщения[/CONTEXT]»
    _RESULT_SOURCE_RE = re.compile(r"chatAction=openResult&entityId=(\d+)\].*?\[CONTEXT=chat\d+/(\d+)\]", re.DOTALL)

    def _without_result_sources(self, chat: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Chat without the messages results are made from: Bitrix24 copies such a message to its result."""
        result_ids = {str(result["id"]) for result in results}
        sources = {
            message_id
            for message in chat.get("messages") or []
            if str(message.get("author_id") or "0") == "0"
            for result_id, message_id in self._RESULT_SOURCE_RE.findall(message.get("text") or "")
            if result_id in result_ids
        }
        if not sources:
            return chat
        return dict(chat, messages=[message for message in chat["messages"] if str(message.get("id")) not in sources])

    def _chat_comment_messages(self, chat: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Messages written by users: system messages have author 0, a deleted message has ``IS_DELETED`` Y."""
        return [
            message
            for message in chat.get("messages") or []
            if str(message.get("author_id") or "0") != "0" and (message.get("params") or {}).get("IS_DELETED") != "Y"
        ]

    def _prepare_chat_comments(self, chat: Dict[str, Any], attachments: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        comments = []
        for message in self._chat_comment_messages(chat):
            text = self._custom_markup_to_markdown(message.get("text") or "", attachments, chat=True)
            links = [
                self._attachment_link(attachments[self._chat_file_key(file_id)])
                for file_id in (message.get("params") or {}).get("FILE_ID") or []
                if self._chat_file_key(file_id) in attachments
            ]
            text = "\n".join(part for part in [text.strip(), *links] if part)
            if not text:
                continue
            comments.append({
                "createdBy": self._get_user(message["author_id"]),
                "createdAt": self._datetime_to_iso(message["date"]),
                "text": text,
            })
        return comments

    def _prepare_result_comments(
        self, results: List[Dict[str, Any]], attachments: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Results of a task as comments headed «Результат от 10.11.2026 18:12», with the time
        as Bitrix24 returns it; files of a result are links to attachments."""
        comments = []
        for result in sorted(results, key=lambda result: parse_datetime(result["createdAt"])):
            header = f"**Результат от {parse_datetime(result['createdAt']):%d.%m.%Y %H:%M}**"
            text = self._custom_markup_to_markdown(result.get("text") or "", attachments)
            links = [
                self._attachment_link(attachments[self._result_file_key(attachment_id)])
                for attachment_id in result.get("files") or []
                if self._result_file_key(attachment_id) in attachments
            ]
            comments.append({
                "createdBy": self._get_user(result["createdBy"]),
                "createdAt": self._datetime_to_iso(result["createdAt"]),
                "text": "\n\n".join(part for part in [header, text.strip(), "\n".join(links)] if part),
            })
        return comments

    @staticmethod
    def _result_file_key(attachment_id: Any) -> str:
        # files of results are attached object ids; the prefix keeps them apart from other attachments
        return f"r{attachment_id}"

    @staticmethod
    def _chat_file_key(file_id: Any) -> str:
        # chat files are disk files; the prefix keeps their ids apart from attached object ids
        return f"f{file_id}"

    @staticmethod
    def _parent_id(task_data: Dict[str, Any]) -> Optional[str]:
        parent_id = task_data.get("parentId")
        return str(parent_id) if parent_id and str(parent_id) != "0" else None

    def _map_status(self, task_data: Dict[str, Any]) -> Optional[str]:
        status = STATUS_BY_BITRIX_STATUS.get(str(task_data.get("status")))
        if status is None:
            log.warning(f"Task {task_data['id']} has unknown status {task_data.get('status')}, the initial status is used")
        return status

    def _prepare_resolution(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """Resolution of a completed or declined task. Tracker requires the resolution, its time and author together."""
        resolution = RESOLUTION_BY_BITRIX_STATUS.get(str(task_data.get("status")))
        if resolution is None:
            return {}
        resolved_at = self._datetime_to_iso(task_data.get("closedDate") or task_data.get("changedDate"))
        resolved_by = self._get_user(task_data.get("closedBy")) or self._get_user(task_data.get("changedBy"))
        if not resolved_at or not resolved_by:
            log.warning(f"Task {task_data['id']} is closed, but its closing date or author is unknown: it is not resolved")
            return {}
        return {"resolution": resolution, "resolvedAt": resolved_at, "resolvedBy": resolved_by}

    def _prepare_links(self, task_data: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Link to the parent task: author of the subtask, the later of the creation dates of the subtask and parent."""
        parent_id = self._parent_id(task_data)
        if parent_id is None:
            return None
        parent_key = self._task_id_to_key_cache.get(parent_id) or self._find_dumped_task(parent_id)
        if parent_key is None:
            log.warning(f"Parent task {parent_id} of task {task_data['id']} is not exported, the link is skipped")
            self._unlinked_tasks[str(task_data["id"])] = parent_id
            return None
        self._unlinked_tasks.pop(str(task_data["id"]), None)
        created = [task_data["createdDate"], self._task_created_at.get(parent_id)]
        return [
            {
                "issue": parent_key,
                "relationship": "is subtask for",
                "createdAt": self._datetime_to_iso(max((d for d in created if d), key=parse_datetime)),
                "createdBy": self._get_user(task_data["createdBy"]),
            }
        ]

    def _find_dumped_task(self, task_id: str) -> Optional[str]:
        """Key of a task dumped by an earlier run into the same directory, with its creation date remembered.

        Raises if the task is found but its ``issue.yaml`` is unreadable or it is in several queues.
        """
        if task_id in self._earlier_task_keys:
            return self._earlier_task_keys[task_id]
        if self._dumped_tasks_index is None:
            self._dumped_tasks_index = {}
            pattern = os.path.join(glob.escape(self._dest_path), "queues", "*", "*", self._filename)
            for path in glob.glob(pattern):
                # epics are numbered apart from tasks
                if os.path.basename(os.path.dirname(os.path.dirname(path))) == QueuesDumper.EPIC_QUEUE_KEY:
                    continue
                dumped_id = os.path.basename(os.path.dirname(path)).rsplit("-", 1)[-1]
                self._dumped_tasks_index.setdefault(dumped_id, []).append(path)

        paths = self._dumped_tasks_index.get(task_id, [])
        if not paths:
            return None
        if len(paths) > 1:
            raise Exception(f"Task {task_id} is dumped earlier into several queues: {sorted(paths)}")
        try:
            with open(paths[0], encoding="utf-8") as f:
                issue = yaml.safe_load(f) or {}
            key, created_at = issue["key"], issue["createdAt"]
            if not isinstance(key, str):
                raise TypeError("key is not a string")
            parse_datetime(created_at)
        except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as e:
            raise Exception(f"Task {task_id} dumped earlier is not readable from {paths[0]}: {e!r}") from None
        self._earlier_task_keys[task_id] = key
        self._task_created_at[task_id] = created_at
        return key

    def _get_issue_key(self, task_data: Dict[str, Any]) -> str:
        queue_key = self.DEFAULT_QUEUE_KEY
        if task_data.get("flowId"):
            queue_key = self._queues_dumper.get_queue_key_by_flow_id(task_data["flowId"])
        task_id = task_data["id"]

        return f"{queue_key}-{task_id}"

    def _get_replace_attachment_link_method(self, attachments: Dict[str, Any]) -> Callable[[re.Match], str]:

        def replace_attachment_link(link: re.Match) -> str:
            attachment = self._resolve_disk_reference(link.group(1), attachments)
            return f"{self._attachment_link(attachment)}\n" if attachment else link.group(0)

        return replace_attachment_link

    @staticmethod
    def _lists_to_markdown(text: str) -> str:
        """``[LIST]`` / ``[LIST=1]`` with ``[*]`` items as Markdown lists; nested lists are indented."""
        pattern = re.compile(r"\[LIST(=1)?\]((?:(?!\[LIST).)*?)\[/LIST\]", re.DOTALL | re.IGNORECASE)

        def replace_list(match: re.Match) -> str:
            preamble, *items = [item.strip() for item in re.split(r"\[\*\]", match.group(2))]
            lines = [preamble] if preamble else []
            for number, item in enumerate(items, start=1):
                marker = f"{number}. " if match.group(1) else "- "
                first, *rest = item.split("\n") if item else [""]
                lines.append(marker + first.strip())
                lines.extend(" " * len(marker) + line for line in rest if line.strip())
            # a blank line ends the list: text after it is not a continuation of the last item
            return "\n" + "\n".join(lines) + "\n\n"

        previous = None
        while previous != text:
            previous, text = text, pattern.sub(replace_list, text)
        return text

    def _custom_markup_to_markdown(
        self,
        text: str,
        attachments: Optional[Dict[str, Dict[str, Any]]] = None,
        reference_pattern: str = r"n?\d+",
        chat: bool = False,
    ) -> str:
        """Bitrix24 BBCode (tags in any case) as Markdown; the content of ``[CODE]`` is kept as is.

        With ``attachments``, ``[DISK FILE ID=<reference_pattern>]`` outside code becomes a link to the attachment.
        ``chat`` enables the chat quote format.
        """
        flags = re.DOTALL | re.MULTILINE | re.IGNORECASE
        text = unescape(text or "").replace("\r\n", "\n")

        code_blocks: List[str] = []

        def keep_code(match: re.Match) -> str:
            code_blocks.append(match.group(1).strip("\n"))
            return f"\n\x00{len(code_blocks) - 1}\x00\n"

        text = re.sub(r"[ \t]*\[CODE\](.*?)\[/CODE\][ \t]*", keep_code, text, flags=flags)
        # leading spaces of a line make a Markdown code block
        text = re.sub(r"^[ \t]+", "", text, flags=re.MULTILINE)

        def replace_user(match: re.Match) -> str:
            return f"@{self._get_user(match.group(1))}"

        text = re.sub(r"\[USER=(\d+)\](.*?)\[/USER\]", replace_user, text, flags=flags)
        text = re.sub(r"\[URL=(.*?)\](.*?)\[/URL\]", r"[\2](\1)", text, flags=flags)
        text = re.sub(r"\[URL\](.*?)\[/URL\]", r"<\1>", text, flags=flags)
        text = re.sub(r"\[IMG[^\]]*\](.*?)\[/IMG\]", r"![](\1)", text, flags=flags)
        text = self._lists_to_markdown(text)

        tags_to_replace = [
            (r"\[B\](.*?)\[/B\]", "**"),
            (r"\[I\](.*?)\[/I\]", "*"),
            (r"\[U\](.*?)\[/U\]", ""),  # not presented in markdown
            (r"\[S\](.*?)\[/S\]", "~~"),
            (r"\[COLOR=#[0-9a-fA-F]{6}\](.*?)\[/COLOR\]", ""),  # not presented in markdown
            (r"\[FONT=\"[^\"]+\"\](.*?)\[/FONT\]", ""),  # not presented in markdown
            (r"\[SIZE=\d+pt\](.*?)\[/SIZE\]", ""),  # not presented in markdown
            (r"\[SPOILER\](.*?)\[/SPOILER\]", ""),  # not presented in markdown
        ]

        for tag_re, new_tag_mark in tags_to_replace:

            def replace_tag(match: re.Match) -> str:
                text = match.group(1)
                if text.endswith("\n"):
                    return f"{new_tag_mark}{text[:-1]}{new_tag_mark}\n"
                return f"{new_tag_mark}{text}{new_tag_mark}"

            text = re.sub(tag_re, replace_tag, text, flags=flags)

        text = re.sub(r"\[P\](.*?)\[/P\]", r"\1\n\n", text, flags=flags)

        # transform inline video to url
        text = re.sub(r"\[VIDEO.*?]//(.*?)\[/VIDEO\]", r"[\1](\1)\n", text, flags=flags)

        def process_quote(match: re.Match) -> str:
            text = match.group(1).strip("\n").replace("\n", "\n> ")
            return f"\n> {text}\n"

        text = re.sub(r"\[QUOTE\](.*?)\[/QUOTE\]", process_quote, text, flags=flags)
        if chat:
            # a quote in a chat message is framed by lines of dashes
            text = re.sub(r"^-{20,}\n(.*?)\n-{20,}$", process_quote, text, flags=flags)

        def process_table(match):
            table_content = match.group(1)
            rows = re.findall(r"\[TR\](.*?)\[/TR\]", table_content, flags=flags)
            markdown_table = "\n\n"

            for i, row in enumerate(rows):
                cells = re.findall(r"\[TD\](.*?)\[/TD\]", row, flags=flags)
                markdown_table += "| " + " | ".join(cell.strip() for cell in cells) + " |\n"
                if i == 0:  # add header separator after first row
                    markdown_table += "| " + " | ".join(["---"] * len(cells)) + " |\n"

            return markdown_table

        text = re.sub(r"\[TABLE\](.*?)\[/TABLE\]", process_table, text, flags=flags)

        if attachments is not None:
            text = re.sub(
                rf"\[DISK FILE ID=({reference_pattern})\]", self._get_replace_attachment_link_method(attachments), text
            )

        text = text.strip()
        text = re.sub(r"[ \t]+\n", "\n", text)  # trailing spaces break the YAML block representation
        text = re.sub(r"\n{3,}", "\n\n", text)

        def restore_code(match: re.Match) -> str:
            # a block inside a quote or a list item keeps its quote marks and indent on every line
            prefix = match.group(1)
            continuation = "".join(char if char in "> \t" else " " for char in prefix)
            code_lines = code_blocks[int(match.group(2))].split("\n")
            lines = [prefix + "```"]
            lines += [continuation + line if line else continuation.rstrip() for line in code_lines]
            # another block on the same line, as in a table row, follows without the line prefix
            suffix = re.sub(r"\x00(\d+)\x00", lambda inner: "```\n" + code_blocks[int(inner.group(1))] + "\n```", match.group(3))
            lines.append(continuation + "```" + suffix)
            return "\n".join(lines)

        text = re.sub(r"^([^\n\x00]*?)\x00(\d+)\x00([^\n]*)$", restore_code, text, flags=re.MULTILINE)

        return text

    def _prepare_description(self, description: str, attachments: Dict[str, Dict[str, Any]]) -> str:
        if not description:
            return None

        return self._custom_markup_to_markdown(description, attachments)

    def _map_kanban_status(self, task_data: Dict[str, Any]) -> Optional[str]:
        """Stage title: of the sprint board for a task of a sprint, of the group board otherwise. Stage 0 maps to the
        stage with the lowest sort; a task of a scrum backlog has no stage."""
        group_id = str(task_data.get("groupId") or "0")
        if group_id == "0":
            return None

        if task_data.get("scrum"):
            sprint = self._task_sprint(task_data)
            if sprint is None:
                return None
            stages = [
                {"id": str(stage.get("id")), "title": stage.get("name"), "sort": stage.get("sort")}
                for stage in sprint.get("stages") or []
            ]
        else:
            if group_id not in self._kanban_statuses_cache:
                stages_response = self._fetcher.fetch_kanban_stages(group_id)
                self._kanban_statuses_cache[group_id] = None if stages_response is None else stages_response[0] or {}
            if self._kanban_statuses_cache[group_id] is None:
                raise Exception(f"Failed to fetch kanban stages for group: {group_id}")
            stages = [
                {"id": str(stage_id), "title": stage.get("TITLE"), "sort": stage.get("SORT")}
                for stage_id, stage in self._kanban_statuses_cache[group_id].items()
            ]

        stage_id = str(task_data.get("stageId") or "0")
        if stage_id == "0":
            first = min(stages, key=lambda stage: int(stage["sort"] or 0), default=None)
            return first["title"] if first else None
        return next((stage["title"] for stage in stages if stage["id"] == stage_id), None)

    def _add_scrum_data(self, tasks: List[Dict[str, Any]]) -> None:
        """Set ``scrum`` of every task: ``entityId``, ``storyPoints`` and ``epicId`` for a task of a scrum group, empty for
        other tasks, ``None`` if not fetched; a task with ``None`` fails to dump."""
        scrum_task_ids = set()
        for task in tasks:
            group_id = str(task.get("groupId") or "0")
            group = self._scrum_group(group_id) if group_id != "0" else {}
            if group:
                scrum_task_ids.add(str(task["id"]))
            else:
                task["scrum"] = None if group is None else {}
        if not scrum_task_ids:
            return
        scrum_tasks = self._fetcher.fetch_scrum_tasks(sorted(scrum_task_ids)) or {}
        for task in tasks:
            if str(task["id"]) in scrum_task_ids:
                task["scrum"] = scrum_tasks.get(str(task["id"]))

    def _scrum_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        """Group, sprints and epics of a scrum group; empty for a group that is not a scrum, ``None`` if not fetched."""
        if group_id in self._scrum_groups:
            return self._scrum_groups[group_id]
        if group_id in self._failed_scrum_groups:
            return None
        try:
            groups = self._fetcher.fetch_projects_by_ids([group_id])
            if groups is None:
                raise Exception(f"Failed to fetch group {group_id}")
            if not groups:
                raise Exception(f"Group {group_id} is not returned by the API")
            if groups[0].get("TYPE") != "scrum":
                self._scrum_groups[group_id] = {}
                return {}
            scrum = self._fetcher.fetch_scrum(group_id)
            if scrum is None:
                raise Exception(f"Failed to fetch sprints and epics of group {group_id}")
            scrum["group"] = groups[0]
            board = self._prepare_board(scrum)
        except Exception:
            log.exception(f"Failed to prepare scrum data of group {group_id}, its tasks are not dumped")
            self._failed_scrum_groups.add(group_id)
            return None
        self._scrum_groups[group_id] = scrum
        self._boards[board["id"]] = board
        return scrum

    def _board_id(self, group_id: Any) -> str:
        return self._project_dump_id(group_id)

    def _sprint_id(self, sprint_id: Any) -> str:
        return f"{self.PROJECT_ID_PREFIX}sprint-{sprint_id}"

    @staticmethod
    def _epic_key(epic_id: Any) -> str:
        return f"{QueuesDumper.EPIC_QUEUE_KEY}-{epic_id}"

    def _prepare_board(self, scrum: Dict[str, Any]) -> Dict[str, Any]:
        """Board of a scrum group: sprints, backlog and story points; columns by status. Tasks of the project of the
        group join the board, epics do not."""
        group = scrum["group"]
        group_id = str(group["ID"])
        return {
            "id": self._board_id(group_id),
            "name": group["NAME"],
            "sprintsAvailable": True,
            "estimateBy": "storyPoints",
            "columns": board_columns(),
            "autoFilters": {
                "addFilter": {
                    "enabled": True,
                    "fieldValues": {
                        "project": [{"fixed": self._project_dump_id(group_id), "invert": False}],
                        "type": [{"fixed": QueuesDumper.DEFAULT_TYPE, "invert": False}],
                    },
                }
            },
            "sprints": [
                {
                    "id": self._sprint_id(sprint["id"]),
                    "name": sprint["name"],
                    "status": SPRINT_STATUS_BY_BITRIX_STATUS.get(sprint.get("status")),
                    "startDateTime": self._datetime_to_iso(sprint.get("dateStart")),
                    "endDateTime": self._datetime_to_iso(sprint.get("dateEnd")),
                }
                for sprint in scrum["sprints"]
            ],
            "createdBy": self._get_user(group.get("OWNER_ID")),
        }

    def _task_sprint(self, task_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Sprint of a task of a scrum group; ``None`` for a task of the backlog."""
        entity_id = str((task_data.get("scrum") or {}).get("entityId") or "0")
        sprints = self._scrum_groups.get(str(task_data.get("groupId")), {}).get("sprints") or []
        return next((sprint for sprint in sprints if str(sprint["id"]) == entity_id), None)

    def _prepare_scrum_fields(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """Board, sprint and story points of a task of a scrum group."""
        scrum = task_data.get("scrum")
        if not scrum:
            return {}
        sprint = self._task_sprint(task_data)
        backlog_id = self._scrum_groups[str(task_data["groupId"])].get("backlog_id")
        if sprint is None and backlog_id is not None and str(scrum.get("entityId")) != str(backlog_id):
            log.warning(f"Sprint {scrum.get('entityId')} of task {task_data['id']} is not returned by the API")
        return {
            "storyPoints": self._story_points(scrum.get("storyPoints"), task_data["id"]),
            "boards": [self._board_id(task_data["groupId"])],
            "sprint": [self._sprint_id(sprint["id"])] if sprint else None,
        }

    @staticmethod
    def _story_points(value: Any, task_id: Any) -> Optional[Any]:
        if value in (None, ""):
            return None
        try:
            points = Decimal(str(value))
        except InvalidOperation:
            points = None
        if points is None or not points.is_finite():
            log.warning(f"Task {task_id} has story points {value!r} that are not a number, they are skipped")
            return None
        return int(points) if points == points.to_integral_value() else float(points)

    def _prepare_epic_link(self, task_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Link to the epic of a task of a scrum group: author of the task, the later of the creation dates of the task
        and the epic."""
        epic_id = str((task_data.get("scrum") or {}).get("epicId") or "0")
        if epic_id == "0":
            return []
        scrum = self._scrum_groups.get(str(task_data["groupId"])) or {}
        if epic_id not in {str(epic["id"]) for epic in scrum.get("epics") or []}:
            log.warning(f"Epic {epic_id} of task {task_data['id']} is not returned by the API, the link is skipped")
            return []
        created = [task_data["createdDate"], scrum["group"].get("DATE_CREATE")]
        return [
            {
                "issue": self._epic_key(epic_id),
                "relationship": "has epic",
                "createdAt": self._datetime_to_iso(max((d for d in created if d), key=parse_datetime)),
                "createdBy": self._get_user(task_data["createdBy"]),
            }
        ]

    def _dump_epics(self, task_data: Dict[str, Any]) -> None:
        """Dump the epics of the scrum group of a task once. Raises if they fail to dump and the task has an epic."""
        group_id = str(task_data.get("groupId") or "0")
        scrum = self._scrum_groups.get(group_id)
        has_epic = str((task_data.get("scrum") or {}).get("epicId") or "0") != "0"
        if group_id in self._failed_epic_groups:
            if has_epic:
                raise Exception(f"Epics of group {group_id} are not dumped")
            return
        if not scrum or group_id in self._dumped_epic_groups:
            return
        try:
            if scrum["epics"] and not self._queues_dumper.dump_epic_queue():
                raise Exception("Failed to dump the queue of epics")
            for epic in scrum["epics"]:
                attachments = self._epic_attachments(epic)
                issue_data = self._prepare_epic_data(epic, scrum, attachments)
                dump_path = os.path.join(self._dest_path, "queues", QueuesDumper.EPIC_QUEUE_KEY, issue_data["key"])
                self._save_with_attachments(issue_data, attachments, dump_path)
                log.info(f"Dumped epic: '{epic['name']}'")
            self._dumped_epic_groups.add(group_id)
        except Exception:
            log.exception(f"Failed to dump epics of group {group_id}")
            self._failed_epic_groups.add(group_id)
            if has_epic:
                raise

    def _epic_attachments(self, epic: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        file_ids = ((epic.get("files") or {}).get("VALUE")) or []
        if not file_ids:
            return {}
        attachments = self._fetcher.fetch_attachments_by_ids([str(file_id) for file_id in file_ids])
        if attachments is None:
            raise Exception(f"Failed to fetch attachments of epic {epic['id']}")
        return {attachment["ID"]: attachment for attachment in attachments if attachment}

    def _prepare_epic_data(
        self, epic: Dict[str, Any], scrum: Dict[str, Any], attachments: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Epic of a scrum group as an issue of type epic. Bitrix24 keeps no dates of an epic: the creation date is the
        creation date of the group."""
        group = scrum["group"]
        group_id = str(group["ID"])
        created_at = self._datetime_to_iso(group.get("DATE_CREATE"))
        if not created_at:
            raise Exception(f"Creation date of group {group_id} is not returned by the API")
        created_by = self._get_user(epic.get("createdBy"))
        issue_data = {
            "key": self._epic_key(epic["id"]),
            "summary": epic["name"],
            "description": self._prepare_description(self._html_to_bbcode(epic.get("description") or ""), attachments),
            "type": QueuesDumper.EPIC_TYPE,
            "status": INITIAL_STATUS,
            "createdBy": created_by,
            "createdAt": created_at,
            "updatedBy": self._get_user(epic.get("modifiedBy")) or created_by,
            "project": {"primary": self._project_dump_id(group_id)},
            "attachments": [
                {
                    "name": attachment["NAME"],
                    "filename": self._get_attachment_filename(attachment),
                    "createdAt": self._datetime_to_iso(attachment["CREATE_TIME"]),
                    "createdBy": self._get_user(attachment["CREATED_BY"]),
                }
                for attachment in attachments.values()
            ],
        }
        issue_data["updatedAt"] = self._latest_datetime(
            [created_at] + [attachment["createdAt"] for attachment in issue_data["attachments"]]
        )
        self._used_projects.add(group_id)
        return issue_data

    @staticmethod
    def _html_to_bbcode(text: str) -> str:
        """The description of an epic is HTML: line breaks, paragraphs, lists, emphasis and links become BBCode, other
        tags are dropped."""
        flags = re.IGNORECASE | re.DOTALL
        text = re.sub(r"<br\s*/?>|</(p|div)>", "\n", text, flags=flags)
        text = re.sub(r"<ul(\s[^>]*)?>", "[LIST]", text, flags=flags)
        text = re.sub(r"<ol(\s[^>]*)?>", "[LIST=1]", text, flags=flags)
        text = re.sub(r"</(ul|ol)>", "[/LIST]", text, flags=flags)
        text = re.sub(r"<li(\s[^>]*)?>", "[*]", text, flags=flags)
        for html_tags, bbcode_tag in (("b|strong", "B"), ("i|em", "I"), ("u", "U"), ("s|del|strike", "S")):
            text = re.sub(rf"<({html_tags})(\s[^>]*)?>", f"[{bbcode_tag}]", text, flags=flags)
            text = re.sub(rf"</({html_tags})>", f"[/{bbcode_tag}]", text, flags=flags)
        text = re.sub(r"<a\s[^>]*href=([\"'])(.*?)\1[^>]*>(.*?)</a>", r"[URL=\2]\3[/URL]", text, flags=flags)
        return re.sub(r"<[^>]+>", "", text)

    @property
    def boards(self) -> List[Dict[str, Any]]:
        return list(self._boards.values())

    # Tracker limit of a checklist item text, in UTF-16 code units
    _CHECKLIST_ITEM_LIMIT = 2000
    # Tracker limit of checklist items in an issue
    _CHECKLIST_ITEMS_LIMIT = 100
    _DEFAULT_CHECKLIST_TITLE = re.compile(r"^BX_CHECKLIST_(\d+)$")

    @staticmethod
    def _utf16_length(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def _checklist_title(self, title: str) -> str:
        """Title of a checklist; a default title stored as ``BX_CHECKLIST_<n>`` becomes «Чек-лист <n>»."""
        match = self._DEFAULT_CHECKLIST_TITLE.match(title or "")
        return f"Чек-лист {match.group(1)}" if match else title or ""

    def _prepare_checklist_items_data(
        self, checklist: Dict[str, Any], task_data: Dict[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Items of all checklists in one list, in tree order; nested items follow their parent item.

        A root entry (``parentId`` 0) is a checklist title. With several non-empty checklists an item starts with
        its checklist title. The assignee is the first accomplice (member type ``A``) of the item who is the
        assignee or an accomplice of the task.
        """
        if not checklist:
            return None

        entries = {str(entry.get("id") or key): dict(entry, id=str(entry.get("id") or key)) for key, entry in checklist.items()}
        children: Dict[str, List[Dict[str, Any]]] = {}
        orphan_roots = []
        for entry_id, entry in entries.items():
            parent_id = str(entry.get("parentId")) if entry.get("parentId") is not None else None
            if parent_id == "0" or (parent_id in entries and parent_id != entry_id):
                children.setdefault(parent_id, []).append(entry)
            else:
                orphan_roots.append(entry)

        def order(entry: Dict[str, Any]) -> Any:
            return int(entry.get("sortIndex") or 0), int(entry["id"]) if entry["id"].isdigit() else 0

        def walk(parent_id: str, seen: Set[str]) -> List[Dict[str, Any]]:
            items = []
            for entry in sorted(children.get(parent_id, []), key=order):
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                items.append(entry)
                items.extend(walk(entry["id"], seen))
            return items

        seen: Set[str] = set()
        roots = sorted(children.get("0", []), key=order)
        checklists = []
        for root in roots:
            seen.add(root["id"])
            checklists.append((self._checklist_title(root.get("title")), walk(root["id"], seen)))

        orphans = []
        for entry in sorted(orphan_roots, key=order):
            seen.add(entry["id"])
            orphans += [entry] + walk(entry["id"], seen)

        def in_cycle(entry_id: str) -> bool:
            parent_id = str(entries[entry_id].get("parentId"))
            for _ in range(len(entries)):
                if parent_id == entry_id:
                    return True
                if parent_id not in entries:
                    return False
                parent_id = str(entries[parent_id].get("parentId"))
            return False

        # entries in parent cycles, each followed by its descendants
        unseen = sorted((entry for entry_id, entry in entries.items() if entry_id not in seen), key=order)
        for entry in sorted(unseen, key=lambda entry: not in_cycle(entry["id"])):
            if entry["id"] not in seen:
                seen.add(entry["id"])
                orphans += [entry] + walk(entry["id"], seen)
        if orphans:
            log.warning(
                f"Task {task_data.get('id')}: checklist items {[item['id'] for item in orphans]} have no parent "
                "checklist, exported without a checklist title"
            )

        named = len([items for _, items in checklists if items]) > 1
        task_users = {str(user_id) for user_id in [task_data.get("responsibleId"), *(task_data.get("accomplices") or [])]}
        result = []
        for title, items in checklists + [("", orphans)]:
            prefix = f"{title}: " if named and title else ""
            for item in items:
                text = item.get("title") or ""
                if text and prefix:
                    if self._utf16_length(prefix + text) <= self._CHECKLIST_ITEM_LIMIT:
                        text = prefix + text
                    else:
                        log.warning(f"Task {task_data.get('id')}: checklist item {item['id']} exceeds the length "
                                    "limit with its checklist title, exported without it")
                members = item.get("members") or []
                if isinstance(members, dict):
                    members = [dict(member, id=member.get("id") or member_id) for member_id, member in members.items()]
                accomplice = next(
                    (str(m.get("id")) for m in members if m.get("type") == "A" and str(m.get("id")) in task_users), None
                )
                result.append({
                    "text": text,
                    "checked": item.get("isComplete") in ("Y", True),
                    "assignee": self._get_user(accomplice) if accomplice else None,
                })
        if len(result) > self._CHECKLIST_ITEMS_LIMIT:
            log.warning(f"Task {task_data.get('id')} has {len(result)} checklist items, the import accepts "
                        f"{self._CHECKLIST_ITEMS_LIMIT}")
        return result

    def _prepare_comments_data(
        self, comments_data: List[Dict[str, Any]], attachments: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        prepared_comments = []

        for comment in comments_data:
            # references without "n" in comments are not attached object ids of the task
            text = self._custom_markup_to_markdown(comment["POST_MESSAGE"], attachments, reference_pattern=r"n\d+")
            prepared_comments.append(
                {
                    "createdBy": self._get_user(comment["AUTHOR_ID"]),
                    "createdAt": self._datetime_to_iso(comment["POST_DATE"]),
                    "text": text,
                }
            )

        return prepared_comments

    def _collect_attachments(self, task_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        attachments = dict()

        for comment in task_data.get("comments", []):
            for attach in comment.get("ATTACHED_OBJECTS", {}).values():
                attach.update(
                    {
                        "DOWNLOAD_URL": self._fetcher.base_url + attach["DOWNLOAD_URL"],
                        "ID": attach["FILE_ID"],
                        "CREATE_TIME": comment["POST_DATE"],
                        "CREATED_BY": comment["AUTHOR_ID"],
                    }
                )
                attachments[attach["ID"]] = attach

        if task_data.get("ufTaskWebdavFiles"):
            task_attachments = self._fetcher.fetch_attachments_by_ids(task_data["ufTaskWebdavFiles"])
            if task_attachments is None:
                raise Exception(f"Failed to fetch attachments of task {task_data['id']}")
            for attachment_id, attach in zip(task_data["ufTaskWebdavFiles"], task_attachments):
                if not attach:
                    log.warning(f"Task {task_data['id']}: file {attachment_id} is not available, skipped")
                    continue
                attachments[attach["ID"]] = dict(attach, TASK_FILE=True)

        chat_files = [
            (message, str(file_id))
            for message in self._chat_comment_messages(task_data.get("chat") or {})
            for file_id in (message.get("params") or {}).get("FILE_ID") or []
        ]
        if chat_files:
            files = self._fetcher.fetch_disk_files(sorted({file_id for _, file_id in chat_files}))
            if files is None:
                raise Exception(f"Failed to fetch chat files of task {task_data['id']}")
            for message, file_id in chat_files:
                file = files.get(file_id)
                if not file:
                    log.warning(f"Task {task_data['id']}: chat file {file_id} is not available, skipped")
                    continue
                key = self._chat_file_key(file_id)
                attachments[key] = {
                    "ID": key,
                    "OBJECT_ID": file_id,
                    "NAME": file["NAME"],
                    "DOWNLOAD_URL": file["DOWNLOAD_URL"],
                    "CREATE_TIME": message["date"],
                    "CREATED_BY": message["author_id"],
                }

        result_files = [
            (result, str(attachment_id))
            for result in task_data.get("results") or []
            for attachment_id in result.get("files") or []
        ]
        if result_files:
            files = self._fetcher.fetch_attachments_by_ids([attachment_id for _, attachment_id in result_files])
            if files is None:
                raise Exception(f"Failed to fetch result files of task {task_data['id']}")
            for (result, attachment_id), file in zip(result_files, files):
                if not file:
                    log.warning(f"Task {task_data['id']}: result file {attachment_id} is not available, skipped")
                    continue
                key = self._result_file_key(attachment_id)
                attachments[key] = dict(
                    file, ID=key, CREATE_TIME=result["createdAt"], CREATED_BY=result["createdBy"]
                )

        self._resolve_copy_references(task_data, attachments)
        return attachments

    # name Bitrix24 gives to a copy of a disk file attached to a task of a group
    _COPY_NAME_PREFIX = "Копия "

    def _resolve_copy_references(self, task_data: Dict[str, Any], attachments: Dict[str, Dict[str, Any]]) -> None:
        """Resolve ``[DISK FILE ID=n…]`` of all texts of the task before any text is rendered: resolving renames a
        copy to the original name, and the file names in links must not change afterwards."""
        texts = [task_data.get("description") or ""]
        texts += [comment.get("POST_MESSAGE") or "" for comment in task_data.get("comments") or []]
        texts += [message.get("text") or "" for message in self._chat_comment_messages(task_data.get("chat") or {})]
        texts += [result.get("text") or "" for result in task_data.get("results") or []]
        # references inside code blocks are not rendered as links
        texts = [re.sub(r"\[CODE\].*?\[/CODE\]", "", text, flags=re.DOTALL | re.IGNORECASE) for text in texts]
        for reference in sorted({ref for text in texts for ref in re.findall(r"\[DISK FILE ID=(n\d+)\]", text)}):
            self._resolve_disk_reference(reference, attachments)

    def _resolve_disk_reference(self, reference: str, attachments: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Attachment of ``[DISK FILE ID=<reference>]``: an attached object id, or ``n<disk file id>``.

        A file attached to a task of a group is a copy named «Копия <name>»: a reference to the original file is
        matched by name and size among the task files, if the match is unique, and the attachment gets the original
        name.
        Raises if the original file cannot be requested; an unresolved reference stays in the text.
        """
        if not reference.startswith("n"):
            if reference not in attachments:
                log.warning(f"Attached object {reference} is not among the task attachments, the reference is kept as is")
            return attachments.get(reference)
        file_id = reference[1:]
        for attachment in attachments.values():
            if str(attachment.get("OBJECT_ID") or attachment.get("FILE_ID") or "") == file_id:
                return attachment
        if file_id not in self._disk_files:
            files = self._fetcher.fetch_disk_files([file_id])
            if files is None:
                raise Exception(f"Failed to fetch disk file {file_id}")
            self._disk_files[file_id] = files.get(file_id) or {}
        original = self._disk_files[file_id]
        name = original.get("NAME")
        matches = [
            attachment
            for attachment in attachments.values()
            if name
            and attachment.get("TASK_FILE")
            and attachment["NAME"] in (name, self._COPY_NAME_PREFIX + name)
            and str(attachment.get("SIZE") or "") == str(original.get("SIZE") or "")
        ]
        if len(matches) != 1:
            if file_id not in self._unresolved_references:
                self._unresolved_references.add(file_id)
                log.warning(f"Disk file {file_id} matches {len(matches)} attachments, the reference is kept as is")
            return None
        matches[0]["NAME"] = name
        return matches[0]

    def _attachment_link(self, attachment: Dict[str, Any]) -> str:
        prefix = "!" if os.path.splitext(attachment["NAME"])[1].lower() in self._image_extensions else ""
        return f"{prefix}[{self._get_attachment_filename(attachment)}]()"

    # Bitrix24 user field type -> Tracker schema type of a single value
    _USER_FIELD_SCHEMA_TYPES = {
        "string": "string",
        "url": "uri",
        "integer": "integer",
        "double": "float",
        "date": "date",
        "datetime": "datetime",
        "boolean": "string",
        "enumeration": "string",
    }
    _SYSTEM_USER_FIELDS = {"UF_CRM_TASK", "UF_TASK_WEBDAV_FILES", "UF_MAIL_MESSAGE"}
    _BOOLEAN_VALUES = ["Да", "Нет"]

    def _prepare_user_field_value(self, field: Dict[str, Any], value: Any) -> Any:
        match field["type"]:
            case "datetime":
                return self._datetime_to_iso(value)
            case "date":
                # a date is stored as midnight UTC and returned in the portal offset
                return parse_datetime(value).astimezone(timezone.utc).date().isoformat()
            case "boolean":
                return self._BOOLEAN_VALUES[0] if str(value) in ("1", "True", "Y") else self._BOOLEAN_VALUES[1]
            case "integer":
                return int(Decimal(str(value)))
            case "double":
                return float(value)
            case "enumeration":
                return field["values"].get(str(value))
            case _:
                return str(value)

    def _prepare_user_field_text(self, field: Dict[str, Any], values: List[Any]) -> str:
        if field["type"] == "datetime":
            return "\n".join(self._datetime_to_readable(value) for value in values)
        if field["type"] in ("date", "boolean"):
            return "\n".join(str(self._prepare_user_field_value(field, value)) for value in values)
        return "\n".join(str(value) for value in values)

    def _prepare_user_fields(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        user_fields = {}

        for field_key, field in self._user_fields_cache.items():
            value = task_data.get(field_key)
            if value is None or value == "" or value == []:
                continue
            values = value if isinstance(value, list) else [value]

            if field["type"] == "enumeration":
                unknown = [item for item in values if str(item) not in field["values"]]
                if unknown:
                    log.warning(f"Task {task_data.get('id')}: values {unknown} of field {field_key} are not in its list, skipped")

            if field["type"] == "enumeration" and field["multiple"]:
                prepared = [self._prepare_user_field_value(field, item) for item in values]
            elif field["multiple"]:
                prepared = self._prepare_user_field_text(field, values)
            else:
                prepared = self._prepare_user_field_value(field, values[0])

            if prepared in (None, "", []):
                continue
            user_fields[field_key] = prepared

        return user_fields

    def _get_user_fields(self) -> Optional[Dict[str, Any]]:
        """User fields of tasks by camelCase key: type, multiplicity, title, enumeration values and Tracker schema."""
        definitions = self._fetcher.fetch_task_user_fields()
        if definitions is None:
            return None
        titles = self._fetcher.fetch_task_fields()
        if titles is None:
            return None

        user_fields = dict()
        for definition in definitions:
            name = definition["FIELD_NAME"]
            field_type = definition.get("USER_TYPE_ID")
            if name in self._SYSTEM_USER_FIELDS:
                continue
            if field_type not in self._USER_FIELD_SCHEMA_TYPES:
                log.warning(f"User field {name} of type {field_type} is not supported and is not exported")
                continue

            parts = name.split("_")
            key = parts[0].lower() + "".join(part.capitalize() for part in parts[1:])
            multiple = definition.get("MULTIPLE") == "Y"
            values = {str(item["ID"]): item["VALUE"] for item in definition.get("LIST") or []}
            user_fields[key] = {
                "type": field_type,
                "multiple": multiple,
                "title": (titles.get(name) or {}).get("title") or name,
                "values": values,
                **self._user_field_schema(field_type, multiple, list(values.values())),
            }

        return user_fields

    def _user_field_schema(self, field_type: str, multiple: bool, values: List[str]) -> Dict[str, Any]:
        if field_type == "enumeration":
            options = {"type": "FixedListOptionsProvider", "values": values}
            schema = {"type": "array", "items": "string"} if multiple else {"type": "string"}
            return {"schema": schema, "optionsProvider": options}
        if multiple:
            return {"schema": {"type": "text"}}
        if field_type == "boolean":
            return {
                "schema": {"type": "string"},
                "optionsProvider": {"type": "FixedListOptionsProvider", "values": self._BOOLEAN_VALUES},
            }
        return {"schema": {"type": self._USER_FIELD_SCHEMA_TYPES[field_type]}}

    def _preload_users_to_cache(self, tasks: List[Dict[str, Any]]) -> None:
        user_fields = ["createdBy", "responsibleId", "changedBy", "closedBy", "auditors", "accomplices"]

        user_ids = set()

        for task in tasks:
            for field in user_fields:
                if isinstance(task[field], list):
                    user_ids.update(task[field])
                elif task[field] is not None:
                    user_ids.add(task[field])

        self._load_users(user_ids)

    def _datetime_to_iso(self, datetime_str: str) -> Optional[str]:
        return to_tracker_datetime(datetime_str)

    def _datetime_to_date(self, datetime_str: str) -> Optional[str]:
        if not datetime_str:
            return None
        dt = datetime.fromisoformat(datetime_str)
        return dt.date().isoformat()

    def _datetime_to_readable(self, datetime_str: str) -> Optional[str]:
        if not datetime_str:
            return None
        dt = datetime.fromisoformat(datetime_str)
        return dt.strftime(self.READABLE_DATE_FORMAT)

    @property
    def used_projects(self) -> Set[str]:
        return self._used_projects

    @property
    def user_fields(self) -> Dict[str, Any]:
        """User fields that have a value in at least one dumped task."""
        return {key: field for key, field in (self._user_fields_cache or {}).items() if key in self._used_user_fields}
