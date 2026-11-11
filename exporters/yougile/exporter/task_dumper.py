import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from markdownify import markdownify
from tqdm import tqdm

from .api_client import YouGileApi
from .caches import StickerCache, UserCache
from .exceptions import APIRequestFailedException, YouGileAPIException
from .task_placement import task_parents
from .tools import (
    ARCHIVE_TAG,
    PRIORITIES,
    YOUGILE_KEY_FIELD,
    dump_to_yaml,
    clear_data,
    hours_to_ms,
    title_to_key,
    unixtime_to_iso_str,
    unixtime_to_date_str,
)


log = logging.getLogger(__name__)

_UMASK = os.umask(0)
os.umask(_UMASK)

# directories in the data directory: a task being built and the earlier directory of a task being replaced
BUILD_PREFIX = ".issue-"
PREVIOUS_PREFIX = ".previous-"


class TaskDumper:
    DEFAULT_ISSUE_TYPE = "task"
    DEFAULT_PRIORITY = "normal"
    HTML_LINK_PATTERN = re.compile(r"<p><a .*?href=\"(?P<url>.*?)\".*?>(?P<text>.*?)</a></p>")
    EDITED_MARK = "\n(редактировано)"

    def __init__(
        self, client: YouGileApi, sticker_cache: StickerCache, user_cache: UserCache, timezone: str = "UTC"
    ) -> None:
        self.client = client
        self.sticker_cache = sticker_cache
        self.user_cache = user_cache
        self.timezone = timezone
        self.attachment_dumper = AttachmentDumper(client, user_cache)

        self._tasks = dict()
        self._keys = dict()
        self._tracker_parents = dict()
        # the task being dumped, for messages: the YouGile key and the key in the export
        self._task_name = None
        # messages about tasks that failed to dump
        self.failed = []
        self._parents = dict()

    def dump_tasks(self, tasks: List[Dict[str, Any]], keys: Dict[str, str], dump_path: str) -> None:
        """Dump tasks with the keys allocated in their queues.

        Each task has ``board_id`` and ``column_title`` of the column it is placed in and ``parent_id`` of the parent
        task it is placed by, if any.
        """
        self._tasks = {task["id"]: task for task in tasks}
        self._keys = keys
        self._parents = task_parents(tasks)
        self.failed = []
        # IDs of tasks that are not dumped
        self.failed_tasks = set()
        self.attachment_dumper.problems = self.failed
        self._tracker_parents = self._choose_tracker_parents(tasks)

        for task_data in tqdm(tasks, desc="Dumping tasks"):
            issue_key = keys[task_data["id"]]
            try:
                self._dump_task(task_data, issue_key, dump_path)
                log.info(f"The task {task_data.get('idTaskProject')} dumped as {issue_key}")
            except Exception as e:
                name = self._name_of(task_data, issue_key)
                log.exception(f"Failed to dump the task {name}, id {task_data['id']}")
                self.failed.append(f"Task {name}, id {task_data['id']}, is not dumped: {e}")
                self.failed_tasks.add(task_data["id"])

    @staticmethod
    def _name_of(task_data: Dict[str, Any], issue_key: str) -> str:
        """The YouGile key of the task and its key in the export, if they differ."""
        yougile_key = task_data.get("idTaskProject")
        return issue_key if yougile_key == issue_key else f"{yougile_key} ({issue_key})"

    def _dump_task(self, task_data: Dict[str, Any], issue_key: str, dest_path: str) -> None:
        """Build the directory of the task aside and put it in place of the directory of an earlier export: a task
        that fails keeps the directory of the earlier export."""
        dump_dir = os.path.join(dest_path, "queues", issue_key.rsplit("-", 1)[0], issue_key)
        # inside the data directory: the directory of the task is moved within one file system
        build_dir = tempfile.mkdtemp(prefix=BUILD_PREFIX, dir=dest_path)
        # mkdtemp creates the directory for its owner only; the issue directory gets the usual permissions
        os.chmod(build_dir, 0o777 & ~_UMASK)
        # named by the key: an export interrupted between the two moves restores it at the next start
        previous_dir = os.path.join(dest_path, PREVIOUS_PREFIX + issue_key)
        try:
            self._build_task(task_data, issue_key, build_dir)
            os.makedirs(os.path.dirname(dump_dir), exist_ok=True)
            if os.path.exists(dump_dir):
                os.replace(dump_dir, previous_dir)
            os.replace(build_dir, dump_dir)
        finally:
            if os.path.exists(previous_dir):
                if os.path.exists(dump_dir):
                    shutil.rmtree(previous_dir, ignore_errors=True)
                else:
                    os.replace(previous_dir, dump_dir)
            shutil.rmtree(build_dir, ignore_errors=True)

    def _build_task(self, task_data: Dict[str, Any], issue_key: str, dump_dir: str) -> None:
        attachments_path = os.path.join(dump_dir, "attachments")
        yougile_key = task_data.get("idTaskProject")
        self._task_name = self._name_of(task_data, issue_key)
        self.attachment_dumper.start_task(self._task_name)

        issue_data = self._prepare_local_fields(task_data)
        issue_data[YOUGILE_KEY_FIELD["key"]] = yougile_key

        new_description, attachments_data = self._prepare_description_with_attachments(task_data, attachments_path)
        comments_data, comments_attachments = self._prepare_comments_with_attachments(task_data, attachments_path)
        attachments_data.extend(comments_attachments)

        assignee, followers = self._prepare_assignee_and_followers(task_data)
        deadline = task_data.get("deadline") or {}
        # a date without time is stored at noon UTC of the day
        date_zone = self.timezone if deadline.get("withTime") else "UTC"
        time_tracking = task_data.get("timeTracking") or {}

        issue_data.update(
            {
                "key": issue_key,
                "summary": task_data["title"],
                "createdAt": unixtime_to_iso_str(task_data["timestamp"]),
                "createdBy": self.user_cache.get_user_login(task_data.get("createdBy")),
                "deadline": unixtime_to_date_str(deadline.get("deadline"), date_zone),
                "type": self.DEFAULT_ISSUE_TYPE,
                "description": new_description,
                "assignee": assignee,
                "followers": followers,
                "priority": self._prepare_priority(task_data),
                "start": unixtime_to_date_str(deadline.get("startDate"), date_zone),
                "estimation": hours_to_ms(time_tracking.get("plan")),
                "spent": hours_to_ms(time_tracking.get("work")),
                "comments": comments_data,
                "attachments": attachments_data,
                "status": title_to_key(task_data["column_title"]),
                "checklistItems": self._prepare_checklist_data(task_data),
                # an archived task is hidden from its board in YouGile
                "boards": [] if task_data.get("archived") else [task_data["board_id"]],
                "tags": [ARCHIVE_TAG] if task_data.get("archived") else [],
                "links": self._prepare_links_data(task_data),
            }
        )

        # the import rejects a comment, attachment or link dated after updatedAt of its issue
        # (a link — of either issue); updatedAt requires updatedBy
        parents = [self._tasks[parent_id] for parent_id in self._parents.get(task_data["id"], [])]
        moments = (
            [(issue_data["createdAt"], issue_data["createdBy"])]
            + [(comment.get("updatedAt") or comment["createdAt"], comment.get("updatedBy") or comment["createdBy"])
               for comment in comments_data]
            + [(attachment["createdAt"], attachment["createdBy"]) for attachment in attachments_data]
            + [(link["createdAt"], link["createdBy"]) for link in issue_data["links"]]
            + [self._subtask_link_moment(parent, task_data) for parent in parents]
        )
        updated_at, updated_by = max(
            (moment for moment in moments if moment[0]),
            key=lambda moment: moment[0],
            default=(issue_data["createdAt"], issue_data["createdBy"]),
        )
        if updated_by or issue_data["createdBy"]:
            issue_data["updatedAt"], issue_data["updatedBy"] = updated_at, updated_by or issue_data["createdBy"]

        os.makedirs(dump_dir, exist_ok=True)
        dump_to_yaml(clear_data(issue_data), os.path.join(dump_dir, "issue.yaml"))

    def _prepare_assignee_and_followers(self, task_data: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
        # the first YouGile assignee is the assignee, other assignees and chat subscribers are followers
        assigned = task_data.get("assigned") or []
        try:
            subscribers = self.client.get_task_subscribers(task_data["id"])
        except YouGileAPIException as e:
            log.warning(f"Chat subscribers of the task {self._task_name} are not exported: {e}")
            self.failed.append(f"Chat subscribers of the task {self._task_name} are not exported: {e}")
            subscribers = []
        user_ids = list(dict.fromkeys(assigned + subscribers))
        logins = [self.user_cache.get_user_login(user_id) for user_id in user_ids]

        assignee = logins[0] if assigned else None
        followers = [login for login in dict.fromkeys(logins) if login and login != assignee]
        return assignee, followers

    def _prepare_priority(self, task_data: Dict[str, Any]) -> str:
        for sticker_id, state in (task_data.get("stickers") or {}).items():
            if self.sticker_cache.is_priority(sticker_id):
                return self._map_priority(self.sticker_cache.get_state_value(state))
        return self.DEFAULT_PRIORITY

    def _prepare_local_fields(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        # transform YouGile stickers to Tracker local fields
        # warn: text and number fields aren't exported because of YouGile API restrictions
        local_fields = {}
        # the queue has local fields of the stickers of its boards only
        project_stickers = task_data.get("project_stickers")
        for sticker_id, state in (task_data.get("stickers") or {}).items():
            if project_stickers is not None and sticker_id not in project_stickers:
                continue
            sticker = self.sticker_cache.get_sticker(sticker_id)
            if sticker is not None and not self.sticker_cache.is_priority(sticker_id):
                local_fields[f"local_{sticker['key']}"] = self.sticker_cache.get_state_value(state)

        return local_fields

    def _prepare_description_with_attachments(
        self, task_data: Dict[str, Any], attachments_path: str
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        description = task_data.get("description")
        if not description:
            return None, []

        new_description = markdownify(description, heading_style="ATX", bullets="-")
        return self.attachment_dumper.dump_description_attachments(task_data, new_description, attachments_path)

    def _prepare_comments_with_attachments(
        self, task_data: Dict[str, Any], attachments_path: str
    ) -> Tuple[list[Dict], List[Dict[str, Any]]]:
        comments = []
        attachments = []

        for comment in self.client.get_task_chat(task_data["id"]):
            # use html markup if available
            if "properties" in comment and "textHtml" in (comment["properties"] or {}):
                comment["text"] = comment["properties"]["textHtml"]
            if comment.get("editTimestamp") and (comment.get("text") or "").endswith(self.EDITED_MARK):
                comment["text"] = comment["text"][: -len(self.EDITED_MARK)]

            text, cur_attachments = self.attachment_dumper.dump_comment_attachments(comment, attachments_path)
            text = self._process_user_mentions(text, comment)
            # prepare links
            text = re.sub(self.HTML_LINK_PATTERN, r"[\g<text>](\g<url>)", text)
            attachments.extend(cur_attachments)
            if not text.strip():
                # the import requires the comment text
                continue

            comments.append(
                {
                    "text": text,
                    "createdAt": unixtime_to_iso_str(comment["id"]),
                    "createdBy": self.user_cache.get_user_login(comment["fromUserId"]),
                    "updatedAt": unixtime_to_iso_str(comment.get("editTimestamp")),
                    "updatedBy": (
                        self.user_cache.get_user_login(comment["fromUserId"]) if comment.get("editTimestamp") else None
                    ),
                }
            )

        return comments, attachments

    def _process_user_mentions(self, text: str, comment: Optional[Dict[str, Any]] = None) -> str:
        # a mention made in YouGile carries the user ID; chunks follow the order of mentions in the text
        chunks = (((comment or {}).get("properties") or {}).get("params") or {}).get("chunks") or []
        mentions = [chunk for chunk in chunks if chunk.get("type") == "user" and chunk.get("replacement")]
        if mentions:
            result, position = [], 0
            for chunk in mentions:
                start = text.find(chunk["replacement"], position)
                if start < 0:
                    continue
                login = self.user_cache.get_user_login((chunk.get("data") or {}).get("userId"))
                end = start + len(chunk["replacement"])
                result.append(text[position:start] + (f"@{login}" if login else chunk["replacement"]))
                position = end
            return "".join(result) + text[position:]

        user_map = {
            user["realName"]: user["email"]
            for user in self.user_cache.get_all_users()
            if user.get("realName") and user.get("email")
        }
        if not user_map:
            return text
        pattern = r"@\b(" + "|".join(re.escape(name) for name in user_map.keys()) + r")\b"
        return re.sub(pattern, lambda m: f"@{user_map[m.group(1)]}", text)

    def _prepare_checklist_data(self, task_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        checklist_items = []
        # merge all YouGile checklists into one for Tracker
        for checklist in task_data.get("checklists") or []:
            for item in checklist["items"]:
                checklist_items.append(
                    {
                        "checklistItemType": "standard",
                        "text": item["title"],
                        "checked": item["isCompleted"],
                    }
                )

        return checklist_items

    def _prepare_links_data(self, task_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        # the API returns subtasks only, not links between tasks
        links = []
        for subtask_id in task_data.get("subtasks") or []:
            # the parent link of the pair comes from the other task
            if subtask_id == task_data["id"] or self._tracker_parents.get(task_data["id"]) == subtask_id:
                continue
            subtask = self._tasks.get(subtask_id)
            if subtask is None:
                log.warning(f"Subtask {subtask_id} of the task {self._task_name} is not exported")
                continue

            created_at, created_by = self._subtask_link_moment(task_data, subtask)
            relationship = "is parent task for" if self._tracker_parents.get(subtask_id) == task_data["id"] else "relates"
            links.append(
                {
                    "issue": self._keys[subtask_id],
                    "relationship": relationship,
                    "createdAt": created_at,
                    "createdBy": created_by,
                }
            )

        return links

    def _choose_tracker_parents(self, tasks: List[Dict[str, Any]]) -> Dict[str, str]:
        """Return the Tracker parent of each subtask; a parent that closes a cycle of parents is not set.
        Parents of placement are set first."""
        parents = {}
        # the top task of the chain of parents of each task, with paths shortened: a task gets its parent once,
        # until then it tops its own chain, and the parent closes a cycle if the task tops the chain of the parent
        tops = {}

        def top(task_id: str) -> str:
            chain = []
            while tops.get(task_id, task_id) != task_id:
                chain.append(task_id)
                task_id = tops[task_id]
            for linked_id in chain:
                tops[linked_id] = task_id
            return task_id

        for task in sorted(tasks, key=lambda task: task.get("parent_id") not in self._tasks):
            parent_id = self._tracker_parent(task)
            if parent_id is None or top(parent_id) == task["id"]:
                continue
            parents[task["id"]] = parent_id
            tops[task["id"]] = top(parent_id)
        return parents

    def _tracker_parent(self, subtask: Dict[str, Any]) -> Optional[str]:
        """A Tracker issue has one parent: the parent the subtask is placed by, else its first exported parent.
        Other parents get a plain link."""
        if subtask.get("parent_id") in self._tasks:
            return subtask["parent_id"]
        return next(
            (
                parent_id
                for parent_id in self._parents.get(subtask["id"], [])
                if parent_id in self._tasks and parent_id != subtask["id"]
            ),
            None,
        )

    def _subtask_link_moment(self, parent: Dict[str, Any], subtask: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        # the link appears when the later of the two tasks is created
        later = max(parent, subtask, key=lambda task: task["timestamp"])
        return unixtime_to_iso_str(later["timestamp"]), self.user_cache.get_user_login(later.get("createdBy"))

    def _map_priority(self, priority: Any) -> str:
        if isinstance(priority, list):
            priority = priority[0] if priority else None
        mapped_priority = PRIORITIES.get(priority)

        if mapped_priority is None:
            log.warning(f"Unknown priority '{priority}' replaced with default '{self.DEFAULT_PRIORITY}'")
            return self.DEFAULT_PRIORITY

        return mapped_priority


class AttachmentDumper:
    MAX_NAME_BYTES = 255
    # a link in the description, made by markdownify: [name](https://ru.yougile.com/user-data/<id>/<name>?previews...)
    DESCRIPTION_ATTACHMENT_REGEX = re.compile(
        # the caption is the file name: it may have square brackets, plain or escaped
        r"(?P<image>!?)\[(?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*\]\(https://(?:[\w-]+\.)*yougile\.com"
        r"(?P<path>/user-data/[^/\s()]+/(?:[^/?\s()]|\([^/?\s()]*\))+)(?:\?[^\s()]*)?\)"
    )
    # a file in a chat message: /root/#file:/user-data/<id>/<name>, the path is URL-encoded twice
    COMMENT_ATTACHMENT_REGEX = re.compile(r"/root/#file:(?P<path>/user-data/[^/\s]+/[^/\s]+)")
    IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}

    def __init__(self, client: YouGileApi, user_cache: UserCache):
        self.client = client
        self.user_cache = user_cache
        # file path -> attachment of the current task
        self._attachments = {}
        # file paths of the current task that failed to download
        self._failed = set()
        self._task = None
        # messages about files that failed to download
        self.problems = []

    def start_task(self, task: Optional[str] = None) -> None:
        """Forget attachments of the previous task: a file linked in several texts of a task is attached once."""
        self._attachments = {}
        self._failed = set()
        self._task = task

    def _get_unique_name(self, filename: str, url: str, full_id: bool = False) -> str:
        attachment_id = url.split("/")[2]  # id from url like: /user-data/<id>/pic.png
        name, ext = os.path.splitext(filename)
        suffix = f"_{attachment_id if full_id else attachment_id[:8]}"
        # a file name is limited to 255 bytes: the name is shortened first, then the extension
        ext = self._shorten(ext, self.MAX_NAME_BYTES - len(suffix.encode()))
        name = self._shorten(name, self.MAX_NAME_BYTES - len(suffix.encode()) - len(ext.encode()))
        return f"{name}{suffix}{ext}"

    @staticmethod
    def _shorten(text: str, max_bytes: int) -> str:
        return text.encode()[: max(0, max_bytes)].decode(errors="ignore")

    def _dump_attachments(
        self,
        text: str,
        created_at: str,
        created_by: str,
        pattern: re.Pattern,
        dest_path: str,
        path_of,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Download files linked in ``text`` and replace the links with attachment names.

        ``path_of`` returns the URL-encoded file path and whether the file is an image for a link match.
        A file that fails to download keeps its link.
        """
        attachments = []

        def process_link(match: re.Match) -> str:
            path, is_image = path_of(match)
            if path in self._failed:
                return match.group(0)
            if path not in self._attachments:
                attachment_name = unquote(path.rsplit("/", 1)[1]).replace("/", "_").replace("\\", "_")
                attachment_filename = self._get_unique_name(attachment_name, path)
                if any(attachment["filename"] == attachment_filename for attachment in self._attachments.values()):
                    attachment_filename = self._get_unique_name(attachment_name, path, full_id=True)
                try:
                    self._download_attachment(attachment_filename, path, dest_path)
                except Exception as e:
                    log.warning(f"File {path} of the task {self._task} is not downloaded, its link is kept: {e}")
                    self._failed.add(path)
                    # a file deleted in YouGile is not a failure of the export
                    if not (isinstance(e, APIRequestFailedException) and e.status_code == 404):
                        self.problems.append(f"File {path} of the task {self._task} is not downloaded: {e}")
                    return match.group(0)

                self._attachments[path] = {
                    "name": attachment_name,
                    "filename": attachment_filename,
                    "createdAt": created_at,
                    "createdBy": created_by,
                }
                attachments.append(self._attachments[path])

            image = "!" if is_image else ""
            return f"{image}[{self._attachments[path]['filename']}]()"

        text = pattern.sub(process_link, text)
        return text, attachments

    def dump_comment_attachments(
        self, comment_data: Dict[str, Any], dest_path: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        def path_of(match: re.Match) -> Tuple[str, bool]:
            # the encoded "?" of the image preview parameters is decoded with the path
            path = unquote(match.group("path")).split("?", 1)[0]
            return path, self._is_image(path)

        return self._dump_attachments(
            comment_data.get("text") or "",
            unixtime_to_iso_str(comment_data["id"]),
            self.user_cache.get_user_login(comment_data["fromUserId"]),
            self.COMMENT_ATTACHMENT_REGEX,
            dest_path,
            path_of,
        )

    def dump_description_attachments(
        self, task_data: Dict[str, Any], new_description: str, dest_path: str
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        def path_of(match: re.Match) -> Tuple[str, bool]:
            return match.group("path"), bool(match.group("image"))

        return self._dump_attachments(
            new_description,
            unixtime_to_iso_str(task_data["timestamp"]),
            self.user_cache.get_user_login(task_data.get("createdBy")),
            self.DESCRIPTION_ATTACHMENT_REGEX,
            dest_path,
            path_of,
        )

    def _is_image(self, path: str) -> bool:
        return os.path.splitext(path)[1].lower() in self.IMAGE_EXTENSIONS

    def _download_attachment(self, name: str, url: str, download_path: str) -> None:
        os.makedirs(download_path, exist_ok=True)
        attachment_path = os.path.join(download_path, name)
        self.client.download_file(url, attachment_path)
