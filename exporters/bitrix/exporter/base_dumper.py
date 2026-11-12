import logging
import requests
import os
from abc import ABC, abstractmethod
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from .bitrix_client import SimpleBitrixClient
from .fetcher import BitrixDataFetcher
from .utils import clean_empty, dump_to_file


logger = logging.getLogger(__name__)


class AttachmentDownloadError(Exception):
    pass


class UsersFetchError(Exception):
    pass


class DepartmentsFetchError(Exception):
    pass


class BaseDumper(ABC):
    DEFAULT_QUEUE_KEY = "TASK"
    # identifier of a user without email or absent from user.get: bitrix-user-<id>
    USER_ID_PREFIX = "bitrix-user-"
    # project id: bitrix24-<group id>, distinct from ids of other sources in the import tag imported_<id>
    PROJECT_ID_PREFIX = "bitrix24-"
    # Bitrix24 user id -> user identifier in the dump
    _users_cache: Dict[str, str] = {}
    # user identifier -> user details for info/users.csv
    _users_info: Dict[str, Dict[str, Any]] = {}
    # Bitrix24 department id -> department, loaded on the first request
    _departments: Dict[str, Dict[str, Any]] = {}
    # department id -> department details for info/departments.csv
    _departments_info: Dict[str, Dict[str, Any]] = {}
    # flow id -> queue key and id of the flow for state/flows.cfg, with the flows of earlier runs
    _flow_queues: Dict[str, Dict[str, Any]] = {}

    def __init__(self, client: SimpleBitrixClient, dest_path: str, max_workers: Optional[int] = None):
        self._fetcher = BitrixDataFetcher(client)
        self._dest_path = dest_path
        self._max_workers = max_workers or os.cpu_count()

    @property
    @abstractmethod
    def _filename(self) -> str:
        pass

    def _clean_and_save_data(self, prepared_data: Any, dump_path: str) -> None:
        prepared_data = clean_empty(prepared_data)
        dump_to_file(prepared_data, dump_path, f"{self._filename}")

    @classmethod
    def _project_dump_id(cls, group_id: Any) -> str:
        return f"{cls.PROJECT_ID_PREFIX}{group_id}"

    def _get_user(self, user_id: Any) -> Optional[str]:
        """Email of the user, or ``bitrix-user-<id>`` for a user without email or absent from ``user.get``."""
        if not user_id or str(user_id) == "0":
            return None
        user_id = str(user_id)
        self._load_users([user_id])
        if user_id not in self._users_cache:
            logger.warning(f"User {user_id} is not returned by user.get, exported as {self.USER_ID_PREFIX}{user_id}")
            self._remember_user(user_id, {})
        return self._users_cache[user_id]

    def _load_users(self, user_ids: Iterable[str]) -> None:
        uncached_user_ids = [str(user_id) for user_id in user_ids if user_id and str(user_id) not in self._users_cache]
        if not uncached_user_ids:
            return

        users = self._fetcher.fetch_users(uncached_user_ids)
        if users is None:
            raise UsersFetchError(f"Failed to fetch users {uncached_user_ids}")
        for user in users:
            self._remember_user(str(user["ID"]), user)

    def _remember_user(self, user_id: str, user: Dict[str, Any]) -> None:
        email = (user.get("EMAIL") or "").strip()
        identifier = email or f"{self.USER_ID_PREFIX}{user_id}"
        self._users_cache[user_id] = identifier
        self._users_info[identifier] = {
            "identifier": identifier,
            "id": user_id,
            "name": " ".join(part for part in (user.get("NAME"), user.get("LAST_NAME")) if part),
            "email": email,
        }

    # name of the group of all employees of a Tracker organization; no department group gets it
    ALL_EMPLOYEES_GROUP = "Все сотрудники"
    # department id -> group of the department in the Tracker permissions
    _department_groups: Dict[str, str] = {}

    def _get_department(self, department_id: Any) -> Optional[str]:
        """Group of a department in the Tracker permissions. A reference may carry a suffix such as ``3:F``; it is
        dropped."""
        department_id = str(department_id).split(":", 1)[0]
        if not self._departments:
            departments = self._fetcher.fetch_departments()
            if departments is None:
                raise DepartmentsFetchError("Failed to fetch departments")
            self._departments.update({str(department["ID"]): department for department in departments})
            self._department_groups.update(self._name_department_groups())
        department = self._departments.get(department_id)
        if department is None:
            logger.warning(f"Department {department_id} is not returned by department.get")
            return None
        group = self._department_groups[department_id]
        self._departments_info[department_id] = {
            "name": group,
            "id": department_id,
            "parent": self._parent_department_name(department) or "",
        }
        return group

    def _name_department_groups(self) -> Dict[str, str]:
        """Groups named after departments, unique among all departments of the portal: a name shared with another
        department or with the group of all employees gets the parent name, then the department id."""
        names = Counter(department["NAME"] for department in self._departments.values())
        taken = set(names) | {self.ALL_EMPLOYEES_GROUP}
        groups = {}
        labels = {}
        for department_id, department in self._departments.items():
            name = department["NAME"]
            if names[name] == 1 and name != self.ALL_EMPLOYEES_GROUP:
                groups[department_id] = name
            else:
                parent_name = self._parent_department_name(department)
                labels[department_id] = f"{name} ({parent_name})" if parent_name else None
        label_counts = Counter(labels.values())
        for department_id, label in labels.items():
            if label is None or label in taken or label_counts[label] > 1:
                name = self._departments[department_id]["NAME"]
                label = f"{name} ({department_id})"
                if label in taken:
                    label = f"{name} (id {department_id})"
            groups[department_id] = label
        return groups

    def _parent_department_name(self, department: Dict[str, Any]) -> Optional[str]:
        parent = self._departments.get(str(department.get("PARENT") or ""))
        return parent["NAME"] if parent else None

    @classmethod
    def exported_departments(cls) -> List[Dict[str, Any]]:
        return sorted(cls._departments_info.values(), key=lambda department: int(department["id"]))

    @classmethod
    def exported_users(cls) -> List[Dict[str, Any]]:
        return sorted(cls._users_info.values(), key=lambda user: int(user["id"]))

    def _download_all_attachments_threaded(self, attachments: List[Dict[str, Any]], attachments_path: str) -> None:
        failed_downloads = []

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:  # type: ignore
            futures = []
            for attachment in attachments:
                future = executor.submit(
                    self._download_attachment,
                    attachment,
                    attachments_path,
                )
                futures.append((attachment["NAME"], future))

            for name, future in futures:
                try:
                    future.result()
                except Exception as e:
                    failed_downloads.append((name, str(e)))
                    logger.exception(f"Failed to download {name}")

        if failed_downloads:
            error_msg = "\n".join(f"{name}: {error}" for name, error in failed_downloads)
            raise Exception(f"Some files failed to download:\n{error_msg}")

    def _download_attachment(self, attachment: Dict[str, Any], attachments_path: str) -> None:
        attachment_name = self._get_attachment_filename(attachment)
        filepath = os.path.join(attachments_path, attachment_name)

        try:
            os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
            with requests.get(attachment["DOWNLOAD_URL"], stream=True, timeout=30) as response:
                response.raise_for_status()
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return
        except requests.exceptions.HTTPError as e:
            failure = f"HTTP {e.response.status_code}"
        except requests.exceptions.RequestException as e:
            failure = type(e).__name__
        except OSError as e:
            failure = f"{type(e).__name__}: {e}"

        # Raise outside except: DOWNLOAD_URL may contain the webhook path.
        if os.path.exists(filepath):
            os.remove(filepath)
        raise AttachmentDownloadError(f"{attachment_name}: {failure}")

    def _get_attachment_filename(self, attachment: Dict[str, Any]) -> str:
        name, ext = os.path.splitext(attachment['NAME'])
        return f"{name}_{attachment['ID']}{ext}"
