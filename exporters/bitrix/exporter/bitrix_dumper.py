import csv
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .base_dumper import BaseDumper
from .bitrix_client import SimpleBitrixClient
from .projects_dumper import ProjectsDumper
from .queues_dumper import QueuesDumper
from .issues_dumper import TasksToIssuesDumper
from .utils import atomic_write, clean_empty, dump_to_file
from .workflow import resolutions_data, statuses_data


logger = logging.getLogger(__name__)

# lists to fill in the mapping files of the import
INFO_DIR = "info"
USERS_LIST_FILENAME = f"{INFO_DIR}/users.csv"
DEPARTMENTS_LIST_FILENAME = f"{INFO_DIR}/departments.csv"
# category of the fields of the dump: the system category of Tracker, found by its name
FIELDS_CATEGORY = "Системные"
# files that a rerun of the export into the same working directory reads
STATE_DIR = "state"
FLOWS_FILENAME = f"{STATE_DIR}/flows.cfg"
EXPORTED_GROUPS_FILENAME = f"{STATE_DIR}/projects.cfg"


class BitrixDumper:

    def __init__(self, client: SimpleBitrixClient, dest_path: str, max_workers: Optional[int] = None):
        self._client = client
        self._dest_path = dest_path

        # users, departments and flows of an earlier export are not reused
        for cache in (BaseDumper._users_cache, BaseDumper._users_info, BaseDumper._departments,
                      BaseDumper._department_groups, BaseDumper._departments_info, BaseDumper._flow_queues):
            cache.clear()
        # state of earlier runs: queue keys of flows and groups exported with their tasks
        self._state_errors: Dict[str, str] = {}
        self._exported_groups: Dict[str, Optional[str]] = {}
        try:
            BaseDumper._flow_queues.update(self._read_flows())
        except (OSError, UnicodeError, ValueError) as e:
            self._state_errors[FLOWS_FILENAME] = str(e)
        try:
            self._exported_groups = self._read_exported_groups()
        except (OSError, UnicodeError, ValueError) as e:
            self._state_errors[EXPORTED_GROUPS_FILENAME] = str(e)
        self._projects_dumper = ProjectsDumper(client, dest_path, max_workers)
        self._issues_dumper = TasksToIssuesDumper(client, dest_path)

    def dump(
        self,
        projects_filter: Optional[Dict[str, Any]] = None,
        tasks_filter: Optional[Dict[str, Any]] = None,
        group_ids: Optional[List[str]] = None,
    ) -> Dict[str, List[str]]:
        """Dump tasks and projects; return what failed: tasks, projects and files.

        With ``group_ids`` the groups are dumped with their tasks matching ``tasks_filter``, except the groups exported
        by earlier runs with the same ``tasks_filter``; ``projects_filter`` is not used. Nothing is dumped if a state
        file is not readable or not valid.
        """
        if self._state_errors:
            for filename, error in self._state_errors.items():
                logger.error(f"{filename} is not read or not valid, nothing is dumped: {error}")
            return {"tasks": [], "projects": [], "files": list(self._state_errors)}

        task_results: Dict[str, bool] = {}
        project_results: Dict[str, bool] = {}
        failed_files: List[str] = []
        try:
            if group_ids is not None:
                self._dump_groups(group_ids, tasks_filter or {}, task_results, project_results, failed_files)
            else:
                if tasks_filter is not None:
                    task_results.update(self._issues_dumper.dump_by_filter(tasks_filter))
                    dumped_tasks = [task_id for task_id, ok in task_results.items() if ok and task_id.isdigit()]
                    task_results.update(self._issues_dumper.relink_tasks(dumped_tasks))
                if projects_filter is not None:
                    project_results.update(
                        self._projects_dumper.dump_by_filter(projects_filter, "Dumping projects by filter")
                    )
                self._dump_objects(project_results, failed_files)
        finally:
            # keys of flows are kept also when the run is interrupted
            self._dump_flows(failed_files)

        return {
            "tasks": [task_id for task_id, ok in task_results.items() if not ok],
            "projects": [name for name, ok in project_results.items() if not ok],
            "files": list(dict.fromkeys(failed_files)),
        }

    def _dump_flows(self, failed_files: List[str]) -> None:
        """Write ``state/flows.cfg``: ``<flow id>:<queue key>`` per line."""
        if not BaseDumper._flow_queues:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), FLOWS_FILENAME)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with atomic_write(path) as f:
                for flow_id in sorted(BaseDumper._flow_queues, key=int):
                    f.write(f"{flow_id}:{BaseDumper._flow_queues[flow_id]['key']}\n")
        except OSError as e:
            logger.error(f"{path} is not written: {e}")
            failed_files.append(FLOWS_FILENAME)

    def _read_flows(self) -> Dict[str, Dict[str, Any]]:
        """Flows of ``state/flows.cfg`` by id: ``<flow id>:<queue key>`` per line; blank lines and lines starting with
        ``#`` are skipped. Raises if a flow id is not a number or repeats, a key is not valid for a queue or belongs to several
        flows."""
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), FLOWS_FILENAME)
        flows: Dict[str, Dict[str, Any]] = {}
        if not os.path.exists(path):
            return flows
        owners: Dict[str, str] = {}
        with open(path, encoding="utf-8-sig") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                flow_id, _, key = (part.strip() for part in line.partition(":"))
                if not (flow_id.isascii() and flow_id.isdigit()):
                    raise ValueError(f"line {line_num}: flow id {flow_id!r} is not a number")
                flow_id = str(int(flow_id))
                if flow_id in flows:
                    raise ValueError(f"line {line_num} repeats flow {flow_id}")
                if not QueuesDumper.is_valid_queue_key(key):
                    raise ValueError(f"line {line_num}: flow {flow_id} has key {key!r} that is not valid for a queue")
                if key in owners:
                    raise ValueError(f"line {line_num}: flows {owners[key]} and {flow_id} have the same key {key}")
                owners[key] = flow_id
                flows[flow_id] = {"key": key, "id": flow_id}
        return flows

    def _read_exported_groups(self) -> Dict[str, Optional[str]]:
        """Groups of ``state/projects.cfg`` with the tasks filter they are exported with: ``<id>:<filter in JSON>`` per
        line, the last line of a group wins; a line with an id only marks a group exported with any filter. Raises if
        an id is not a number."""
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), EXPORTED_GROUPS_FILENAME)
        groups: Dict[str, Optional[str]] = {}
        if not os.path.exists(path):
            return groups
        with open(path, encoding="utf-8-sig") as f:
            for line_num, line in enumerate(f, start=1):
                group_id, separator, tasks_filter = line.strip().partition(":")
                if not group_id and not separator:
                    continue
                if not (group_id.isascii() and group_id.isdigit()):
                    raise ValueError(f"line {line_num}: group id {group_id!r} is not a number")
                groups[str(int(group_id))] = tasks_filter if separator else None
        return groups

    def _save_exported_groups(self, group_ids: List[str], tasks_filter: str) -> bool:
        """Append groups exported with the tasks filter to ``state/projects.cfg``. The file is replaced whole: a failed
        write keeps the earlier file."""
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), EXPORTED_GROUPS_FILENAME)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            content = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8-sig") as f:
                    content = f.read()
            # a file edited by hand may lack the line break at the end
            if content and not content.endswith("\n"):
                content += "\n"
            with atomic_write(path) as f:
                f.write(content)
                f.writelines(f"{group_id}:{tasks_filter}\n" for group_id in group_ids)
        except (OSError, UnicodeError) as e:
            logger.error(f"{path} is not written: {e}")
            return False
        self._exported_groups.update((group_id, tasks_filter) for group_id in group_ids)
        return True

    def _dump_groups(
        self,
        group_ids: List[str],
        tasks_filter: Dict[str, Any],
        task_results: Dict[str, bool],
        project_results: Dict[str, bool],
        failed_files: List[str],
    ) -> None:
        """Dump groups with their tasks matching ``tasks_filter``, except the groups exported by earlier runs with the
        same filter, and dump again the subtasks dumped without the link to a task of a group. The shared files are
        written after each group; then the groups dumped without failures are appended to ``state/projects.cfg``."""
        # GROUP_ID of the filter is replaced by the group
        filter_text = json.dumps(
            {key: value for key, value in tasks_filter.items() if key != "GROUP_ID"},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        # group id -> project results of a group dumped without failures of tasks and not appended to
        # state/projects.cfg yet: a failed project may be dumped later as a project of tasks
        pending: Dict[str, List[str]] = {}
        failed_groups: List[str] = []
        state_failed = False
        for group_id in group_ids:
            if group_id in self._exported_groups and self._exported_groups[group_id] in (None, filter_text):
                logger.info(f"Group {group_id} is exported by an earlier run, skipped")
                continue
            group_results = self._projects_dumper.dump_by_ids([group_id], f"Dumping group {group_id}")
            group_tasks = self._issues_dumper.dump_by_filter({**tasks_filter, "GROUP_ID": group_id})
            project_results.update(group_results)
            task_results.update(group_tasks)
            dumped_tasks = [task_id for task_id, ok in group_tasks.items() if ok and task_id.isdigit()]
            relinked = self._issues_dumper.relink_tasks(dumped_tasks)
            task_results.update(relinked)

            files: List[str] = []
            self._dump_objects(project_results, files)
            self._dump_flows(files)
            failed_files.extend(files)

            # failed epics of groups dumped before are reported with the tasks of every next group
            epics_of_other_groups = {f"epics of group {other_id}" for other_id in group_ids if other_id != group_id}
            # a subtask not dumped again is dumped again with the group by the next run
            if all(relinked.values()) and all(
                ok for name, ok in group_tasks.items() if name not in epics_of_other_groups
            ):
                pending[group_id] = list(group_results)
            else:
                failed_groups.append(group_id)
            if files:
                continue
            exported = [
                pending_id for pending_id, projects in pending.items() if all(project_results[name] for name in projects)
            ]
            if exported:
                if not self._save_exported_groups(exported, filter_text):
                    failed_files.append(EXPORTED_GROUPS_FILENAME)
                    state_failed = True
                    continue
                for exported_id in exported:
                    del pending[exported_id]

        for group_id in failed_groups:
            logger.warning(
                f"Group {group_id} is not appended to {EXPORTED_GROUPS_FILENAME}: its tasks or their subtasks are not "
                f"dumped"
            )
        for group_id, projects in pending.items():
            reasons = [name for name in projects if not project_results[name]] or [
                f"{EXPORTED_GROUPS_FILENAME} is not written" if state_failed else "shared files are not written"
            ]
            logger.warning(f"Group {group_id} is not appended to {EXPORTED_GROUPS_FILENAME}: {'; '.join(reasons)}")

    def _dump_objects(self, project_results: Dict[str, bool], failed_files: List[str]) -> None:
        project_results.update(
            self._projects_dumper.dump_by_ids(self._issues_dumper.used_projects, "Dumping projects for tasks")
        )

        if not self._dump_global_fields(self._issues_dumper.user_fields):
            failed_files.append("global_fields.yaml")
        if not self._dump_boards(self._issues_dumper.boards):
            failed_files.append("boards.yaml")
        dump_to_file(statuses_data(), self._dest_path, "statuses.yaml")
        dump_to_file(resolutions_data(), self._dest_path, "resolutions.yaml")
        if not self._dump_users_list():
            failed_files.append(USERS_LIST_FILENAME)
        if not self._dump_departments_list():
            failed_files.append(DEPARTMENTS_LIST_FILENAME)

    def _dump_users_list(self) -> bool:
        """Users referenced by the dump, written to ``info/`` to fill in ``mapping/users.cfg``."""
        return self._dump_list(
            USERS_LIST_FILENAME, ["identifier", "id", "name", "email"], "identifier", BaseDumper.exported_users()
        )

    def _dump_departments_list(self) -> bool:
        """Departments referenced by the permissions in the dump, written to ``info/`` to fill in
        ``mapping/groups.cfg``."""
        departments = BaseDumper.exported_departments()
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), DEPARTMENTS_LIST_FILENAME)
        if not departments and not os.path.exists(path):
            return True
        return self._dump_list(DEPARTMENTS_LIST_FILENAME, ["name", "id", "parent"], "id", departments)

    def _read_list(
        self, filename: str, fieldnames: List[str], key: str
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """Columns and rows by ``key`` of a list in the working directory; no file gives no rows. Raises if the file
        is not readable or its columns differ."""
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), filename)
        fieldnames = list(fieldnames)
        rows: Dict[str, Dict[str, Any]] = {}
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return fieldnames, rows
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not set(fieldnames) <= set(reader.fieldnames or []):
                raise ValueError(f"expected columns {fieldnames}, got {reader.fieldnames}")
            fieldnames += [name for name in reader.fieldnames if name not in fieldnames]
            for row in reader:
                if None in row:
                    raise ValueError(f"line {reader.line_num} has more fields than the header")
                rows[row[key]] = row
        return fieldnames, rows

    def _dump_list(self, filename: str, fieldnames: List[str], key: str, rows: List[Dict[str, Any]]) -> bool:
        """Write a list in the working directory. Rows and extra columns of an existing file are kept: a staged
        export adds tasks to the same ``data``. An unreadable file is not overwritten."""
        path = os.path.join(os.path.dirname(os.path.abspath(self._dest_path)), filename)
        try:
            fieldnames, existing = self._read_list(filename, fieldnames, key)
        except (OSError, UnicodeError, csv.Error, ValueError) as e:
            logger.error(f"{path} is not updated: {e}")
            return False
        for row in rows:
            existing[row[key]] = {**existing.get(row[key], {}), **row}

        def order(row: Dict[str, Any]) -> Any:
            row_id = str(row.get("id") or "")
            return (0, int(row_id), "") if row_id.isdigit() else (1, 0, row_id)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # BOM: spreadsheet applications read the file as UTF-8
            with atomic_write(path, newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(sorted(existing.values(), key=order))
        except OSError as e:
            logger.error(f"{path} is not written: {e}")
            return False
        logger.info(f"{filename} is written to {path}")
        return True

    def _dump_boards(self, boards: List[Dict[str, Any]]) -> bool:
        """Write ``boards.yaml``; boards of an existing file are kept for a staged export.

        An unreadable file is not overwritten.
        """
        path = os.path.join(self._dest_path, "boards.yaml")
        if not boards:
            return True
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or []
                if not isinstance(existing, list) or not all(
                    isinstance(board, dict) and "id" in board for board in existing
                ):
                    raise ValueError("expected a list of boards with ids")
            except (OSError, UnicodeError, yaml.YAMLError, ValueError) as e:
                logger.error(f"{path} is not updated: {e}")
                return False
            new_ids = {board["id"] for board in boards}
            boards = [board for board in existing if board["id"] not in new_ids] + boards

        try:
            dump_to_file([clean_empty(board) for board in boards], self._dest_path, "boards.yaml")
        except OSError as e:
            logger.error(f"{path} is not written: {e}")
            return False
        return True

    def _dump_global_fields(self, user_fields: Dict[str, Any]) -> bool:
        """Write ``global_fields.yaml``; fields of an existing file are kept for a staged export.

        An unreadable file is not overwritten.
        """
        global_fields = [
            {
                "key": "kanbanStatus",
                "name": {
                    "ru": "Статус в Канбан-доске",
                    "en": "Kanban status",
                },
                "category": FIELDS_CATEGORY,
                "schema": {"type": "string"},
            },
            {
                "category": FIELDS_CATEGORY,
                "name": {
                    "ru": "Соисполнители",
                    "en": "Accomplices",
                },
                "key": "accomplices",
                "schema": {
                    "type": "array",
                    "items": "user",
                },
            },
        ]

        for field_key, field_data in user_fields.items():
            global_fields.append(
                clean_empty(
                    {
                        "key": field_key,
                        "name": {
                            "ru": field_data["title"],
                            "en": field_data["title"],
                        },
                        "category": FIELDS_CATEGORY,
                        "schema": field_data["schema"],
                        "optionsProvider": field_data.get("optionsProvider"),
                    }
                )
            )

        path = os.path.join(self._dest_path, "global_fields.yaml")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or []
                if not isinstance(existing, list) or not all(
                    isinstance(field, dict) and isinstance(field.get("key"), str) for field in existing
                ):
                    raise ValueError("expected a list of fields with keys")
            except (OSError, UnicodeError, yaml.YAMLError, ValueError) as e:
                logger.error(f"{path} is not updated: {e}")
                return False
            new_keys = {field["key"] for field in global_fields}
            global_fields = [field for field in existing if field.get("key") not in new_keys] + global_fields

        try:
            dump_to_file(global_fields, self._dest_path, "global_fields.yaml")
        except OSError as e:
            logger.error(f"{path} is not written: {e}")
            return False
        return True
