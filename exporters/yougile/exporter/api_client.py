import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

from .exceptions import (
    APIRequestFailedException,
    ApiKeyNotFoundException,
    ApiKeyRejectedException,
    CompanyAmbiguousException,
    CompanyNotFoundException,
    MaxRetriesExceededException,
)


log = logging.getLogger(__name__)


class YouGileApi:
    """YouGile REST API v2 client authenticated by a company API key."""

    HOST = "https://ru.yougile.com"
    BASE_URL = f"{HOST}/api-v2"
    PAGE_LIMIT = 1000  # API maximum
    MAX_RETRIES = 7
    DELAY = 10
    TIMEOUT = 60
    CHUNK_SIZE = 1024 * 1024
    RETRY_STATUSES = (408, 429)
    RETRY_EXCEPTIONS = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )

    def __init__(
        self,
        api_key: Optional[str],
        max_retries: int = MAX_RETRIES,
        delay: float = DELAY,
        key_source: str = "the API key from configs/token.cfg",
    ) -> None:
        self.max_retries = max_retries
        self.delay = delay
        self.key_source = key_source
        self.session = requests.Session()
        if api_key is not None:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    @classmethod
    def from_credentials(cls, login: str, password: str, company_name: str) -> "YouGileApi":
        """Authenticate with an existing company API key."""
        client = cls(None)
        credentials = {"login": login, "password": password}

        companies = client._paginate("post", "/auth/companies", json=credentials)
        company_ids = [company["id"] for company in companies if company.get("name") == company_name]
        if not company_ids:
            raise CompanyNotFoundException(company_name)
        if len(company_ids) > 1:
            raise CompanyAmbiguousException(company_name)

        keys = client._request("post", "/auth/keys/get", json={**credentials, "companyId": company_ids[0]})
        active_keys = [key["key"] for key in keys or [] if not key.get("deleted")]
        if not active_keys:
            raise ApiKeyNotFoundException(company_name)

        client.session.headers["Authorization"] = f"Bearer {active_keys[0]}"
        client.key_source = f"the API key of the company {company_name} found by login and password"
        return client

    def check_api_key(self) -> None:
        try:
            self._request("get", "/users/me")
        except APIRequestFailedException as e:
            if e.status_code in (401, 403):
                raise ApiKeyRejectedException(e.status_code, self.key_source) from None
            raise

    @staticmethod
    def _encode_params(params: Dict[str, Any]) -> Dict[str, Any]:
        # the API accepts only lowercase booleans
        return {
            name: str(value).lower() if isinstance(value, bool) else value
            for name, value in params.items()
            if value is not None
        }

    def _wait_before_retry(self, what: str, failure: str, attempt: int) -> None:
        if attempt < self.max_retries:
            log.warning(f"{what}: {failure}, retrying in {self.delay} seconds ({attempt}/{self.max_retries})")
            time.sleep(self.delay)

    def _attempt(self, method: str, url: str, **kwargs: Any) -> Tuple[Optional[requests.Response], Optional[str]]:
        """Send a request once; return ``(response, None)``, or ``(None, reason)`` for a retryable failure."""
        try:
            response = self.session.request(method, url, timeout=self.TIMEOUT, **kwargs)
        except self.RETRY_EXCEPTIONS as e:
            return None, type(e).__name__

        if response.status_code in self.RETRY_STATUSES or response.status_code >= 500:
            response.close()
            return None, f"status {response.status_code}"
        return response, None

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(1, self.max_retries + 1):
            response, failure = self._attempt(method, url, **kwargs)
            if response is not None:
                return response
            self._wait_before_retry(f"{method.upper()} {url}", failure, attempt)

        raise MaxRetriesExceededException(self.max_retries, failure)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        not_found_ok: bool = True,
    ) -> Any:
        """Return the response body; 404 gives ``{}`` if ``not_found_ok``, else raises as other errors."""
        response = self._send(method, self.BASE_URL + path, params=self._encode_params(params or {}), json=json)

        if response.status_code == 404 and not_found_ok:
            return {}
        if not 200 <= response.status_code < 300:
            raise APIRequestFailedException(response.status_code, response.text)
        return response.json()

    def _paginate(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """Return all pages of a list. A list of an existing object comes empty, not 404: 404 means the object is
        missing and raises."""
        results = []
        offset = 0

        while True:
            page_params = {**(params or {}), "limit": self.PAGE_LIMIT, "offset": offset}
            response = self._request(method, path, params=page_params, json=json, not_found_ok=False)
            results.extend(response.get("content", []))

            if not (response.get("paging") or {}).get("next"):
                return results
            offset += self.PAGE_LIMIT

    def search_projects(self, project_name: str) -> List[Dict]:
        return self._paginate("get", "/projects", {"title": project_name})

    def get_project(self, project_id: str) -> Dict:
        return self._request("get", f"/projects/{project_id}")

    def get_project_boards(self, project_id: str) -> List[Dict]:
        return self._paginate("get", "/boards", {"projectId": project_id})

    def get_project_roles(self, project_id: str) -> List[Dict]:
        """Return custom roles of the project with their permissions."""
        return self._paginate("get", f"/projects/{project_id}/roles")

    def get_board(self, board_id: str) -> Dict:
        return self._request("get", f"/boards/{board_id}")

    def get_board_columns(self, board_id: str) -> List[Dict]:
        return self._paginate("get", "/columns", {"boardId": board_id})

    def get_tasks(self, include_deleted: bool = False) -> List[Dict]:
        """Return tasks of the company, subtasks without a column included; completed and archived ones too."""
        return self._paginate("get", "/task-list", {"includeDeleted": include_deleted or None})

    def get_task(self, task_id: str) -> Dict:
        return self._request("get", f"/tasks/{task_id}")

    def get_users(self) -> List[Dict]:
        return self._paginate("get", "/users")

    def get_user(self, user_id: str) -> Dict:
        return self._request("get", f"/users/{user_id}")

    def get_task_subscribers(self, task_id: str) -> List[str]:
        """Return IDs of users subscribed to the task chat."""
        return self._request("get", f"/tasks/{task_id}/chat-subscribers", not_found_ok=False) or []

    def get_task_chat(self, task_id: str) -> List[Dict]:
        return self._paginate("get", f"/chats/{task_id}/messages")

    def get_board_stickers(self, board_id: str) -> List[Dict]:
        # get both string and sprint stickers
        string_stickers = self._paginate("get", "/string-stickers", {"boardId": board_id})
        sprint_stickers = self._paginate("get", "/sprint-stickers", {"boardId": board_id})
        return string_stickers + sprint_stickers

    def download_file(self, url: str, filepath: str) -> None:
        """Download a file by its path on the YouGile host; replace ``filepath`` only on success."""
        part_path = os.path.join(os.path.dirname(filepath), f".{uuid.uuid4().hex}.part")
        try:
            for attempt in range(1, self.max_retries + 1):
                failure = self._download_attempt(self.HOST + url, part_path)
                if failure is None:
                    os.replace(part_path, filepath)
                    return
                self._wait_before_retry(f"Download of {url}", failure, attempt)
        finally:
            self._remove_file(part_path)

        raise MaxRetriesExceededException(self.max_retries, failure)

    def _download_attempt(self, url: str, filepath: str) -> Optional[str]:
        """Download a file once; return the reason of a retryable failure, ``None`` on success."""
        response, failure = self._attempt("get", url, stream=True)
        if response is None:
            return failure

        try:
            if not 200 <= response.status_code < 300:
                raise APIRequestFailedException(response.status_code, f"download of {url} failed")

            expected_size = response.headers.get("Content-Length")
            size = 0
            try:
                with open(filepath, "wb") as f:
                    for chunk in response.iter_content(self.CHUNK_SIZE):
                        f.write(chunk)
                        size += len(chunk)
            except self.RETRY_EXCEPTIONS as e:
                return type(e).__name__

            if expected_size is not None and not response.headers.get("Content-Encoding") and size != int(expected_size):
                return f"got {size} of {expected_size} bytes"
            return None
        finally:
            response.close()

    @staticmethod
    def _remove_file(filepath: str) -> None:
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass
