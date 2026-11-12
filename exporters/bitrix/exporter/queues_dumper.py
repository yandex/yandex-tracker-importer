import logging
import os
import re
from typing import Any, Dict, List, Optional

from .base_dumper import BaseDumper
from .bitrix_client import SimpleBitrixClient
from .transliterate import translit
from .workflow import INITIAL_STATUS, RESOLUTIONS, workflow_steps


logger = logging.getLogger(__name__)


class QueuesDumper(BaseDumper):
    DEFAULT_TYPE = "task"
    DEFAULT_PRIORITY = "normal"
    # queue of epics of scrum groups: an epic is not a task in Bitrix24 and has its own numbering
    EPIC_QUEUE_KEY = "EPIC"
    EPIC_TYPE = "epic"

    def __init__(self, client: SimpleBitrixClient, dest_path: str, max_workers: Optional[int] = None):
        super().__init__(client, dest_path, max_workers)
        self._cache = dict()

    @property
    def _filename(self) -> str:
        return "queue.yaml"

    def dump_by_flow_id(self, flow_id: str) -> bool:
        if flow_id in self._cache:
            return True

        try:
            flow_data = self._fetcher.fetch_flow_by_id(flow_id)
            queue_key = self._get_queue_key(flow_data)
            queue_data = {
                "key": queue_key,
                "name": flow_data["name"],
                "lead": self._get_user(str(flow_data["ownerId"])),
                "defaultType": self.DEFAULT_TYPE,
                "defaultPriority": self.DEFAULT_PRIORITY,
                "description": flow_data["description"],
                "teamUsers": self._team_users(flow_data),
                "permissions": self._permissions(flow_data),
                "issueTypesConfig": self._get_issue_types_config(queue_key),
                "workflows": self._get_workflows(queue_key),
            }

            dump_path = os.path.join(self._dest_path, "queues", queue_data["key"])
            self._clean_and_save_data(queue_data, dump_path)
            self._cache[flow_id] = queue_data["key"]
        except Exception:
            logger.exception(f"Failed to dump queue from flow with id '{flow_id}'")
            return False

        return True

    def _team_users(self, flow_data: Dict[str, Any]) -> List[str]:
        """Users of the team and responsible users of a flow; members are ``[type, id]`` pairs."""
        members = list(flow_data.get("team") or []) + list(flow_data.get("responsibleList") or [])
        users = [self._get_user(member_id) for member_type, member_id in members if member_type == "user"]
        return list(dict.fromkeys(user for user in users if user))

    def _permissions(self, flow_data: Dict[str, Any]) -> Dict[str, Any]:
        """Users and departments that may create tasks in a flow create issues in the queue; without task creators the
        queue keeps the default permissions. Read and write are granted to all employees: the import revokes all
        employees from each permission missing in the dump. Raises if a task creator is not exported."""
        creators = flow_data.get("taskCreators") or []
        if not creators or any(creator_type == "meta-user" for creator_type, _ in creators):
            return {}
        users, groups = [], []
        for creator_type, creator_id in creators:
            if creator_type == "user":
                user = self._get_user(creator_id)
                if user:
                    users.append(user)
                    continue
            elif creator_type == "department":
                group = self._get_department(creator_id)
                if group:
                    groups.append(group)
                    continue
            raise Exception(f"Task creator {creator_type} {creator_id} of flow {flow_data['id']} is not exported")
        everyone = {"groups": [self.ALL_EMPLOYEES_GROUP]}
        return {
            "create": {"users": list(dict.fromkeys(users)), "groups": list(dict.fromkeys(groups))},
            "read": everyone,
            "write": everyone,
        }

    def get_queue_key_by_flow_id(self, flow_id: str) -> Optional[str]:
        return self._cache.get(flow_id)

    # longest queue key the Tracker accepts
    _KEY_LENGTH = 15
    # words the Tracker does not accept as a queue key, in any case
    _RESTRICTED_KEYS = {
        "ADMIN", "AGILE", "AJAX", "ATTACHMENT", "CREATETICKET", "DASHBOARD", "FILTER", "FILTERS", "GROUPING",
        "ISSUECARD", "ME", "ORDER", "PING", "QUEUES", "SUBSCRIPTIONS", "EXPORT", "SETTINGS", "R", "ISSUES",
        "FEDERATIONS", "ADDONS", "PAGES", "YANDEXTESTING",
    }

    @classmethod
    def is_valid_queue_key(cls, key: str) -> bool:
        """A key of a queue of a flow: 2 to 15 Latin capital letters, not a restricted word, not a key of the default
        queue or the queue of epics."""
        return (
            isinstance(key, str)
            and re.fullmatch(rf"[A-Z]{{2,{cls._KEY_LENGTH}}}", key) is not None
            and key not in cls._RESTRICTED_KEYS
            and key not in (cls.DEFAULT_QUEUE_KEY, cls.EPIC_QUEUE_KEY)
        )

    def _get_queue_key(self, flow_data: Dict[str, Any]) -> str:
        """Queue key of a flow: the one given to the flow by an earlier run, else the title in Latin letters, for
        example «Тестовая строка 123» → ``TESTOVAYASTROKA``. A key taken by another flow or not valid for a queue
        (see ``is_valid_queue_key``) gets letters of the flow id: digit 0 is A, 1 is B and so on."""
        flow_id = str(flow_data["id"])
        known = self._flow_queues.get(flow_id)
        if known and known.get("key"):
            return known["key"]

        title = "".join(c for c in flow_data["name"] if c.isalpha() or c.isspace())
        base = re.sub(r"[^A-Z]", "", translit(title).upper()) or "FLOW"
        taken = {flow["key"] for flow in self._flow_queues.values()}
        key = base[:self._KEY_LENGTH]
        if key in taken or not self.is_valid_queue_key(key):
            suffix = "".join(chr(ord("A") + int(digit)) for digit in flow_id if digit.isdigit())
            for extra in [""] + [chr(code) for code in range(ord("A"), ord("Z") + 1)]:
                key = base[:self._KEY_LENGTH - len(suffix) - len(extra)] + suffix + extra
                if key not in taken and self.is_valid_queue_key(key):
                    break
            else:
                raise Exception(f"No free queue key for flow {flow_id}")
        self._flow_queues[flow_id] = {"key": key, "id": flow_id}
        return key

    def dump_default_queue(self) -> bool:
        queue_key = self.DEFAULT_QUEUE_KEY

        if queue_key in self._cache:
            return True

        queue_data = {
            "key": queue_key,
            "name": "Очередь по умолчанию",
            "lead": self._get_user(self._fetcher.fetch_current_user()["ID"]),
            "defaultType": self.DEFAULT_TYPE,
            "defaultPriority": self.DEFAULT_PRIORITY,
            "description": "Очередь для задач из Битрикс24, не привязанных к проекту",
            "issueTypesConfig": self._get_issue_types_config(queue_key),
            "workflows": self._get_workflows(queue_key),
        }

        result = self._dump_queue(queue_data)

        if result:
            self._cache[queue_key] = queue_key

        return result

    def dump_epic_queue(self) -> bool:
        queue_key = self.EPIC_QUEUE_KEY

        if queue_key in self._cache:
            return True

        queue_data = {
            "key": queue_key,
            "name": "Эпики",
            "lead": self._get_user(self._fetcher.fetch_current_user()["ID"]),
            "defaultType": self.EPIC_TYPE,
            "defaultPriority": self.DEFAULT_PRIORITY,
            "description": "Эпики скрам-групп Битрикс24",
            "issueTypesConfig": self._get_issue_types_config(queue_key, self.EPIC_TYPE),
            "workflows": self._get_workflows(queue_key),
        }

        result = self._dump_queue(queue_data)

        if result:
            self._cache[queue_key] = queue_key

        return result

    def _dump_queue(self, queue_data: Dict[str, Any]) -> bool:
        try:
            queue_key = queue_data["key"]
            dump_path = os.path.join(self._dest_path, "queues", queue_key)
            self._clean_and_save_data(queue_data, dump_path)
        except Exception:
            logger.exception(f"Failed to dump queue {queue_data}")
            return False

        return True

    def _get_workflow_key(self, queue_key: str) -> str:
        return f"W{queue_key}"

    def _get_issue_types_config(self, queue_key: str, issue_type: str = DEFAULT_TYPE) -> List[Dict[str, Any]]:
        return [
            {
                "issueType": issue_type,
                "workflow": self._get_workflow_key(queue_key),
                "resolutions": [resolution["key"] for resolution in RESOLUTIONS],
            }
        ]

    def _get_workflows(self, queue_key: str) -> List[Dict[str, Any]]:
        return [
            {
                "initialAction": {
                    "name": {
                        "ru": "Не задано",
                        "en": "Undefined",
                    },
                    "target": INITIAL_STATUS,
                },
                "key": self._get_workflow_key(queue_key),
                "name": "Основной рабочий процесс",
                "steps": workflow_steps(),
                "type": "visual",
            }
        ]
