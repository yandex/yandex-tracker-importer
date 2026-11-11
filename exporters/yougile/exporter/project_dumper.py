import logging
import os
from typing import Any, Dict, List, Optional

from .api_client import YouGileApi
from .caches import StickerCache, UserCache
from .exceptions import YouGileAPIException
from .tools import CATEGORY_NAME, RESOLUTION, dump_to_yaml, clear_data, title_to_key


log = logging.getLogger(__name__)


# the closing status required by a workflow
CLOSED_STATUS = {"key": "closed", "name": {"en": "Closed", "ru": "Закрыт"}}


# queue permission types of the system roles of a YouGile project
SYSTEM_ROLE_PERMISSIONS = {
    "admin": ["create", "read", "write", "grant"],
    "worker": ["create", "read", "write"],
    "observer": ["read"],
}
# flags of a custom role that change any task of the project
TASK_CHANGES = ("delete", "editTitle", "editDescription", "complete", "close", "connect", "editStickers")


class ProjectDumper:
    DEFAULT_TYPE = "task"
    DEFAULT_PRIORITY = "normal"

    def __init__(self, client: YouGileApi, sticker_cache: StickerCache, user_cache: UserCache) -> None:
        self.client = client
        self.sticker_cache = sticker_cache
        self.user_cache = user_cache
        self._used_statuses = dict()

    def find_by_title(self, title: str) -> List[Dict[str, Any]]:
        """Return projects whose title equals ``title``."""
        # the API searches by a substring of the title
        return [project for project in self.client.search_projects(title) if project.get("title") == title]

    def fetch_project(self, project_id: str) -> Dict[str, Any]:
        """Return the project with its boards, their columns and stickers.

        A board without columns, such as a knowledge base board, has no tasks in the API and is left out."""
        project_data = self.client.get_project(project_id)
        if not project_data:
            raise YouGileAPIException(f"project {project_id} not found")
        # system roles are named in the project users, custom roles by their IDs
        if set((project_data.get("users") or {}).values()) - set(SYSTEM_ROLE_PERMISSIONS):
            project_data["roles"] = {role["id"]: role for role in self.client.get_project_roles(project_id)}
        project_data["boards"] = []
        for board in self.client.get_project_boards(project_id):
            board["columns"] = self.client.get_board_columns(board["id"])
            if not board["columns"]:
                log.info(f"Board '{board['title']}' of the project '{project_data['title']}' has no columns: not exported")
                continue
            self.sticker_cache.add_stickers(self.client.get_board_stickers(board["id"]))
            project_data["boards"].append(board)

        return project_data

    def dump_project(self, project_data: Dict[str, Any], queue_key: str, dump_path: str) -> None:
        queue_data = self._prepare_queue_data(project_data, queue_key)

        dump_dir = os.path.join(dump_path, "queues", queue_key)
        os.makedirs(dump_dir, exist_ok=True)
        dump_to_yaml(clear_data(queue_data), os.path.join(dump_dir, "queue.yaml"))
        log.info(f"Project '{project_data['title']}' with id '{project_data['id']}' dumped as a queue with key '{queue_key}'")

    @staticmethod
    def project_admin(project_data: Dict[str, Any]) -> Optional[str]:
        """Return the ID of the first admin of the project."""
        return next((user_id for user_id, role in (project_data.get("users") or {}).items() if role == "admin"), None)

    def _prepare_queue_data(self, project_data: Dict[str, Any], queue_key: str) -> Dict[str, Any]:
        if queue_key not in self._used_statuses:
            self._used_statuses[queue_key] = dict()

        lead = self.user_cache.get_user_login(self.project_admin(project_data))
        queue_data = {
            "key": queue_key,
            "name": project_data["title"],
            "defaultType": self.DEFAULT_TYPE,
            "defaultPriority": self.DEFAULT_PRIORITY,
            "lead": lead,
            "teamUsers": [
                login
                for login in (self.user_cache.get_user_login(user_id) for user_id in project_data.get("users") or {})
                if login and login != lead
            ],
            "workflows": self._prepare_workflows_data(project_data, queue_key),
            "issueTypesConfig": [
                {
                    "issueType": self.DEFAULT_TYPE,
                    "workflow": f"W{queue_key}",
                    "resolutions": [RESOLUTION["key"]],
                }
            ],
            "local_fields": self._prepare_local_fields_data(project_data),
            "permissions": self._prepare_permissions(project_data),
        }
        return queue_data

    def _prepare_permissions(self, project_data: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
        """Access to the queue by the roles of the project users: a YouGile project is open to its users only, the
        import closes the queue to other employees."""
        users_by_type = {permission_type: [] for permission_type in ("create", "read", "write", "grant")}
        for user_id, role in (project_data.get("users") or {}).items():
            # a user without e-mail is left out: a user missing in the organization fails all permissions of the queue
            login = self.user_cache.get_user_email(user_id)
            if not login:
                log.warning(f"User {user_id} of the project '{project_data['title']}' has no e-mail: no queue permissions")
                continue
            for permission_type in self._role_permissions(project_data, role):
                users_by_type[permission_type].append(login)
        return {
            permission_type: {"users": sorted(set(users))}
            for permission_type, users in users_by_type.items()
            if users
        }

    @staticmethod
    def _role_permissions(project_data: Dict[str, Any], role: str) -> List[str]:
        """Queue permission types of a project role. A custom role gives reading if it shows all tasks, creating if
        it adds tasks, writing if it changes all tasks, settings if it changes the project or its boards; its
        permissions for single boards, columns or tasks are not exported."""
        if role in SYSTEM_ROLE_PERMISSIONS:
            return SYSTEM_ROLE_PERMISSIONS[role]
        role_data = (project_data.get("roles") or {}).get(role)
        if role_data is None:
            log.warning(f"Role {role} of the project '{project_data['title']}' is not found: its users get no access")
            return []
        permissions = role_data.get("permissions") or {}
        boards = permissions.get("boards") or {}
        columns = boards.get("columns") or {}
        all_tasks = columns.get("allTasks") or {}
        if permissions.get("children"):
            log.warning(
                f"Role '{role_data.get('name')}' of the project '{project_data['title']}' has permissions for single "
                "boards, columns or tasks: they are not exported"
            )
        result = []
        if all_tasks.get("show"):
            result.append("read")
        if columns.get("addTask"):
            result.append("create")
        if any(all_tasks.get(flag) for flag in TASK_CHANGES) or any(
            all_tasks.get(flag) not in (None, False, "no") for flag in ("move", "assignUsers", "editSubtasks")
        ):
            result.append("write")
        if any(permissions.get(flag) for flag in ("editTitle", "delete", "addBoard")) or boards.get("settings"):
            result.append("grant")
        if not result:
            log.warning(
                f"Role '{role_data.get('name')}' of the project '{project_data['title']}' gives no access to all tasks: "
                "its users get no queue permissions"
            )
        return result

    def _prepare_workflows_data(self, project_data: Dict[str, Any], queue_key: str) -> List[Dict]:
        # returns any-to-any workflow and saves used statuses

        for board in project_data["boards"]:
            for column in board["columns"]:
                key = title_to_key(column["title"])
                # columns with one status are named after the first of them
                self._used_statuses[queue_key].setdefault(key, {
                    "key": key,
                    "name": {
                        "en": column["title"],
                        "ru": column["title"],
                    },
                })

        # the closing status required by the workflow, if no column gives it
        self._used_statuses[queue_key].setdefault(CLOSED_STATUS["key"], CLOSED_STATUS)

        workflow = {
            "key": f"W{queue_key}",
            "name": f"Рабочий процесс очереди \"{project_data["title"]}\"",
            "initialAction": {
                "name": {
                    "ru": "Не задано",
                    "en": "Undefined",
                },
                "target": "closed",
            },
            "steps": [
                {
                    "status": status["key"],
                    "metaAction": {
                        "name": status["name"],
                        "target": status["key"],
                    },
                }
                for status in self._used_statuses[queue_key].values()
            ],
            "type": "visual",
        }

        return [workflow]

    @staticmethod
    def sticker_ids(project_data: Dict[str, Any]) -> List[str]:
        """IDs of custom stickers used on the boards of the project, in the order of the boards."""
        return list(
            dict.fromkeys(
                sticker_id
                for board in project_data["boards"]
                for sticker_id in (board.get("stickers") or {}).get("custom") or {}
            )
        )

    def _prepare_local_fields_data(self, project_data: Dict[str, Any]) -> List[Dict]:
        local_fields = dict()

        for sticker_id in self.sticker_ids(project_data):
            sticker = self.sticker_cache.get_sticker(sticker_id)
            if sticker is not None:
                if self.sticker_cache.is_priority(sticker_id):
                    continue

                local_fields[sticker["key"]] = {
                    "key": sticker["key"],
                    "name": {
                        "ru": sticker["name"],
                        "en": sticker["name"],
                    },
                    "category": CATEGORY_NAME,
                    "optionsProvider": {
                        "needValidation": True,
                        "type": "FixedListOptionsProvider",
                        "values": sticker["states"],
                    },
                    # a sticker of a task has one state
                    "schema": {"type": "string"},
                }

        return list(local_fields.values())

    @property
    def used_statuses(self) -> List[str]:
        """Statuses of all queues; a column title names a status, CLOSED_STATUS names "closed" when no column gives it."""
        statuses = dict()
        for queue_statuses in self._used_statuses.values():
            for key, status in queue_statuses.items():
                if status is not CLOSED_STATUS:
                    statuses.setdefault(key, status)
        statuses.setdefault(CLOSED_STATUS["key"], CLOSED_STATUS)
        return list(statuses.values())
