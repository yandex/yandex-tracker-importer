import csv
import logging
import os
import shutil
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tqdm import tqdm

from .api_client import YouGileApi
from .caches import StickerCache, UserCache
from .exceptions import ExportConfigError, ExportIncompleteError, YouGileAPIException
from .key_state import KeyState
from .project_dumper import ProjectDumper
from .projects_config import parse_projects
from .task_dumper import BUILD_PREFIX, PREVIOUS_PREFIX, TaskDumper
from .task_placement import Renumbered, allocate_keys, has_project_key, place_tasks, split_task_key
from .tools import ARCHIVE_TAG, RESOLUTION, YOUGILE_KEY_FIELD, dump_to_yaml, title_to_key


log = logging.getLogger(__name__)


def _column_name(title: str) -> str:
    """Return the title in lower case with single spaces between words."""
    return " ".join(title.split()).casefold()


class YouGileDumper:

    def __init__(
        self,
        client: YouGileApi,
        dump_path: str,
        timezone: str = "UTC",
        report_path: Optional[str] = None,
        state: Optional[KeyState] = None,
    ) -> None:
        self.client = client
        self.dump_path = dump_path
        self.report_path = report_path
        self.state = state or KeyState()

        sticker_cache = StickerCache()
        self.user_cache = UserCache(self.client)
        self.project_dumper = ProjectDumper(self.client, sticker_cache, self.user_cache)
        self.task_dumper = TaskDumper(self.client, sticker_cache, self.user_cache, timezone)
        self._task_order = []
        self._used_numbers = defaultdict(set)

    def dump(self, project_lines: Iterable[str]) -> None:
        """Check the configuration and the projects, then dump them.

        Raises ``ExportConfigError`` with all problems found before anything is written.
        """
        projects, tasks_by_project = self._check(project_lines)
        self._remove_build_directories()

        tasks_to_dump = []
        task_keys = {}
        renumbered = []
        boards = []
        for project, prefix in projects:
            self.project_dumper.dump_project(project, prefix, self.dump_path)

            allocation = allocate_keys(
                tasks_by_project[project["id"]], prefix, self._used_numbers[prefix.casefold()], self.state.history
            )
            for task in allocation.duplicates:
                message = (
                    f"Task {task.get('idTaskProject')}, id {task['id']}, of the YouGile project '{project['title']}' "
                    "is not exported: another task of the queue has the same key. Task keys must be unique: "
                    "change the prefix of one of the projects in YouGile settings, YouGile renumbers its tasks"
                )
                log.error(message)
                tqdm.write(message)
            task_keys.update(allocation.keys)
            renumbered.extend(allocation.renumbered)
            tasks_to_dump.extend(task for task in tasks_by_project[project["id"]] if task["id"] in allocation.keys)
            boards.extend(self._prepare_boards(project, prefix))

        self._report_changed_keys(tasks_to_dump, task_keys)
        self.state.save(task_keys)

        # dump in the order of the task list: the first exported parent of a subtask is taken in this order
        order = {task_id: position for position, task_id in enumerate(self._task_order)}
        tasks_to_dump.sort(key=lambda task: order[task["id"]])
        self.task_dumper.dump_tasks(tasks_to_dump, task_keys, self.dump_path)
        # a task that is not dumped keeps the directories of earlier exports
        kept = set(task_keys.values()) | {
            key for task_id in self.task_dumper.failed_tasks for key in self.state.history.get(task_id, [])
        }
        self._remove_earlier_tasks([prefix for _, prefix in projects], kept)

        dump_to_yaml(boards, os.path.join(self.dump_path, "boards.yaml"))
        dump_to_yaml(self.project_dumper.used_statuses, os.path.join(self.dump_path, "statuses.yaml"))
        dump_to_yaml([RESOLUTION], os.path.join(self.dump_path, "resolutions.yaml"))
        dump_to_yaml([YOUGILE_KEY_FIELD], os.path.join(self.dump_path, "global_fields.yaml"))
        self._write_renumbered_report(renumbered)

        problems = list(self.task_dumper.failed) + [
            f"User {user_id} is not available: objects of the user may be dumped without the author"
            for user_id in self.user_cache.unavailable_users
        ]
        if problems:
            raise ExportIncompleteError(problems)

    def _check(self, project_lines: Iterable[str]) -> Tuple[List[Tuple[Dict[str, Any], str]], Dict[str, List[Dict]]]:
        """Return the projects with their prefixes and their tasks; raise ``ExportConfigError`` with all problems."""
        lines, errors = parse_projects(project_lines)
        errors.extend(self._check_earlier_export([line.prefix for line in lines]))

        found = []
        for line in lines:
            try:
                matches = self.project_dumper.find_by_title(line.title)
            except YouGileAPIException as e:
                errors.append(f"YouGile project '{line.title}' is not searched: {e}")
                continue
            if not matches:
                errors.append(f"YouGile project '{line.title}' not found: the title must match exactly")
            elif len(matches) > 1:
                ids = ", ".join(project["id"] for project in matches)
                errors.append(f"{len(matches)} YouGile projects are titled '{line.title}' ({ids}): rename them in YouGile")
            else:
                found.append((matches[0], line.prefix))
        if errors:
            raise ExportConfigError(errors)

        try:
            self.user_cache.add_users_to_cache(self.client.get_users())
        except YouGileAPIException as e:
            raise ExportConfigError([f"Users of the YouGile company are not available: {e}"]) from None

        projects = []
        for project, prefix in tqdm(found, desc="Loading projects"):
            try:
                project = self.project_dumper.fetch_project(project["id"])
            except YouGileAPIException as e:
                errors.append(f"YouGile project '{project['title']}' is not loaded: {e}")
                continue
            admin = self.project_dumper.project_admin(project)
            if admin is None:
                errors.append(
                    f"YouGile project '{project['title']}' has no admin: make a user its admin, "
                    "the admin becomes the lead of the queue"
                )
            elif not self.user_cache.get_user_login(admin):
                errors.append(f"Admin {admin} of the YouGile project '{project['title']}' is not available")
            errors.extend(self._check_stickers(project))
            projects.append((project, prefix))
        errors.extend(self._check_columns([project for project, _ in projects]))
        if errors:
            raise ExportConfigError(errors)

        try:
            tasks_by_project = self._place_tasks(projects)
        except YouGileAPIException as e:
            raise ExportConfigError([f"Tasks of the YouGile company are not available: {e}"]) from None
        for project, prefix in projects:
            self._report_prefixes(project, prefix, tasks_by_project[project["id"]])
        return projects, tasks_by_project

    @staticmethod
    def _check_columns(projects: List[Dict[str, Any]]) -> List[str]:
        """A column title gives the status key, statuses are shared by all queues. Columns whose titles differ in
        letter case or in the number of spaces only share one status; other columns with one status key are errors."""
        errors = []
        titles_by_key = defaultdict(set)
        for project in projects:
            for board in project["boards"]:
                board_titles = [column["title"] for column in board["columns"]]
                titles_by_name = defaultdict(list)
                for title in board_titles:
                    titles_by_name[_column_name(title)].append(title)
                for titles in titles_by_name.values():
                    if len(titles) > 1:
                        names = ", ".join(f"'{title}'" for title in titles)
                        errors.append(
                            f"Board '{board['title']}' of the YouGile project '{project['title']}' has columns {names} "
                            "with one status: rename them in YouGile"
                        )
                for title in board_titles:
                    key = title_to_key(title)
                    if key:
                        titles_by_key[key].add((title, project["title"]))
                    else:
                        errors.append(
                            f"Column '{title}' of the YouGile project '{project['title']}' has no letters "
                            "to make a status key: rename it in YouGile"
                        )
        for key, titles in sorted(titles_by_key.items()):
            if len({_column_name(title) for title, _ in titles}) > 1:
                names = ", ".join(f"'{title}' ({project})" for title, project in sorted(titles))
                errors.append(f"Columns {names} of the YouGile projects give the same status {key}: rename them in YouGile")
        return errors

    def _check_stickers(self, project: Dict[str, Any]) -> List[str]:
        """A sticker name gives the key of a local field; stickers with one key share one local field."""
        errors = []
        names_by_key = defaultdict(set)
        for sticker_id in ProjectDumper.sticker_ids(project):
            sticker = self.project_dumper.sticker_cache.get_sticker(sticker_id)
            if sticker is None or self.project_dumper.sticker_cache.is_priority(sticker_id):
                continue
            if not sticker["key"]:
                errors.append(
                    f"Sticker '{sticker['name']}' of the YouGile project '{project['title']}' has no letters "
                    "to make a field key: rename it in YouGile"
                )
            else:
                names_by_key[sticker["key"]].add((sticker["name"], sticker_id))
        for key, stickers in sorted(names_by_key.items()):
            if len(stickers) > 1:
                names = ", ".join(f"'{name}'" for name, _ in sorted(stickers))
                errors.append(
                    f"Stickers {names} of the YouGile project '{project['title']}' give the same field {key}: "
                    "rename them in YouGile"
                )
        return errors

    @staticmethod
    def _report_prefixes(project: Dict[str, Any], prefix: str, tasks: List[Dict[str, Any]]) -> None:
        """Print the prefixes of task keys in the columns of the project, to check the prefix in projects.cfg."""
        counts = defaultdict(int)
        for task in tasks:
            if task.get("columnId") and has_project_key(task):
                counts[split_task_key(task["idTaskProject"])[0]] += 1
        found = ", ".join(f"{key} — {count}" for key, count in sorted(counts.items(), key=lambda item: -item[1]))
        message = f"YouGile project '{project['title']}' → queue {prefix}; task keys in its columns: {found or 'none'}"
        log.info(message)
        tqdm.write(message)

    def _place_tasks(self, projects_with_prefixes: List[Tuple[Dict[str, Any], str]]) -> Dict[str, List[Dict[str, Any]]]:
        """Group not deleted tasks by the project of their column, or of their parent task's column."""
        projects = [project for project, _ in projects_with_prefixes]
        columns = {
            column["id"]: (project, board, column)
            for project in projects
            for board in project["boards"]
            for column in board["columns"]
        }
        project_stickers = {
            project["id"]: set(ProjectDumper.sticker_ids(project))
            for project in projects
        }
        # pages of the list shift if tasks are created during the export: a task can come twice
        all_tasks = list({task["id"]: task for task in self.client.get_tasks(include_deleted=True)}.values())
        tasks = [task for task in all_tasks if not task.get("deleted")]
        # numbers of deleted tasks and of tasks of other projects are not given to other tasks:
        # task keys in texts keep their meaning
        self._used_numbers = defaultdict(set)
        for task in all_tasks:
            parsed = split_task_key(task.get("idTaskProject"))
            if parsed:
                self._used_numbers[parsed[0].casefold()].add(parsed[1])
        self._task_order = [task["id"] for task in tasks]
        # a subtask goes with a parent task in the queue of its key from an earlier export, else in the project of
        # its YouGile number: the subtask keeps its key
        columns_by_prefix = {
            prefix.casefold(): {column["id"] for board in project["boards"] for column in board["columns"]}
            for project, prefix in projects_with_prefixes
        }
        preferred = {}
        for task in tasks:
            if has_project_key(task):
                prefix = split_task_key(task["idTaskProject"])[0].casefold()
                if prefix in columns_by_prefix:
                    preferred[task["id"]] = columns_by_prefix[prefix]
        for task_id, key in self.state.keys.items():
            parsed = split_task_key(key)
            if parsed and parsed[0].casefold() in columns_by_prefix:
                preferred[task_id] = columns_by_prefix[parsed[0].casefold()]
        placements = place_tasks(tasks, columns, preferred)

        tasks_by_project = defaultdict(list)
        for task in tasks:
            if task["id"] not in placements:
                continue
            placement = placements[task["id"]]
            project, board, column = columns[placement.column_id]
            task["board_id"] = board["id"]
            task["column_title"] = column["title"]
            task["parent_id"] = placement.parent_id
            task["project_stickers"] = project_stickers[project["id"]]
            tasks_by_project[project["id"]].append(task)
        return tasks_by_project

    def _check_earlier_export(self, prefixes: List[str]) -> List[str]:
        """Return errors of an earlier export in the data directory: a queue not in projects.cfg, no state file."""
        queues_path = os.path.join(self.dump_path, "queues")
        queues = sorted(
            name
            for name in (os.listdir(queues_path) if os.path.isdir(queues_path) else [])
            if os.path.isdir(os.path.join(queues_path, name)) and not name.startswith(".")
        )
        if not queues:
            return []
        errors = []
        if not self.state.exists:
            errors.append(
                f"{self.state.path} with task keys of the earlier export in {self.dump_path} is not found: restore it "
                "or export into an empty directory"
            )
        for queue in queues:
            if queue not in prefixes:
                errors.append(
                    f"Queue {queue} of the earlier export in {self.dump_path} is not in projects.cfg: add its project "
                    f"to projects.cfg or remove {os.path.join(queues_path, queue)}"
                )
        return errors

    def _remove_build_directories(self) -> None:
        """Restore directories of tasks an interrupted export moved aside, remove directories it was building."""
        if not os.path.isdir(self.dump_path):
            return
        for name in os.listdir(self.dump_path):
            path = os.path.join(self.dump_path, name)
            if name.startswith(PREVIOUS_PREFIX):
                key = name[len(PREVIOUS_PREFIX):]
                dump_dir = os.path.join(self.dump_path, "queues", key.rsplit("-", 1)[0], key)
                if os.path.exists(dump_dir):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.makedirs(os.path.dirname(dump_dir), exist_ok=True)
                    os.replace(path, dump_dir)
            elif name.startswith(BUILD_PREFIX):
                shutil.rmtree(path, ignore_errors=True)

    def _report_changed_keys(self, tasks: List[Dict[str, Any]], keys: Dict[str, str]) -> None:
        """A task placed in another queue since the earlier export, moved itself or by its parent, gets a key of the
        new queue."""
        earlier_keys = self.state.keys
        for task in tasks:
            earlier = earlier_keys.get(task["id"])
            if earlier and earlier != keys[task["id"]]:
                message = (
                    f"Task {task.get('idTaskProject')}, id {task['id']}, exported earlier as {earlier}, gets the key "
                    f"{keys[task['id']]}: it is in another queue now. An issue {earlier} imported earlier stays in Tracker"
                )
                log.warning(message)
                tqdm.write(message)

    def _remove_earlier_tasks(self, prefixes: List[str], keys: Iterable[str]) -> None:
        """Remove directories of tasks of an earlier export that are not exported now: deleted, moved to another
        queue or with a taken key."""
        keys = set(keys)
        for prefix in prefixes:
            queue_path = os.path.join(self.dump_path, "queues", prefix)
            if not os.path.isdir(queue_path):
                continue
            for name in sorted(os.listdir(queue_path)):
                path = os.path.join(queue_path, name)
                if os.path.isdir(path) and name not in keys:
                    shutil.rmtree(path)
                    log.info(f"Task {name} of the earlier export is not exported now, its directory is removed")

    @staticmethod
    def _prepare_boards(project: Dict[str, Any], queue_key: str) -> List[Dict[str, Any]]:
        """A YouGile board becomes a Tracker board with its columns.

        The board of a project with one board takes the tasks of the queue; boards of a project with several boards
        have no rule: the import places their tasks, users place new ones.
        """
        boards = []
        for board in project["boards"]:
            board_data = {
                "id": board["id"],
                "name": board["title"],
                "columns": [
                    {"id": number, "name": column["title"], "statuses": [title_to_key(column["title"])]}
                    for number, column in enumerate(board["columns"], start=1)
                ],
            }
            if len(project["boards"]) == 1:
                board_data["autoFilters"] = {
                    "addFilter": {
                        "enabled": True,
                        "fieldValues": {
                            "queue": [{"fixed": queue_key, "invert": False}],
                            "tags": [{"fixed": ARCHIVE_TAG, "invert": True}],
                        },
                    }
                }
            boards.append(board_data)
        return boards

    def _write_renumbered_report(self, renumbered: List[Renumbered]) -> None:
        if not renumbered or not self.report_path:
            return
        with open(self.report_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["YouGile task ID", "YouGile key", "Tracker key", "Reason"])
            for item in renumbered:
                writer.writerow([item.task["id"], item.task.get("idTaskProject"), item.key, item.reason])
        # relative to the working directory: the path in a container differs from the path on the host
        tqdm.write(
            f"{len(renumbered)} tasks have keys other than their YouGile numbers, see {os.path.relpath(self.report_path)}"
        )
