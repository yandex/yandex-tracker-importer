import logging
import os
import platform
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)


class SimpleYouTrackClient:
    """Thin YouTrack REST API client.

    `base_url` is the address of the instance: ``https://example.youtrack.cloud`` or
    ``https://example.com/youtrack``; requests go to ``<base_url>/api``. Authentication is a permanent token. A request
    is retried on 408, 429 and 5xx; 429 waits for ``Retry-After`` when it is given.
    """

    PAGE_SIZE = 100
    # an error page of a proxy is HTML: its text is cut in messages
    _ERROR_TEXT_LIMIT = 300

    def __init__(
        self,
        base_url: str,
        token: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        rate_limit_retry_after: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/api"):
            self.base_url = self.base_url[:-len("/api")]
        parts = urlsplit(self.base_url)
        # a file url of the API is a path from the root of the host
        self._host_url = f"{parts.scheme}://{parts.netloc}"
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.rate_limit_retry_after = rate_limit_retry_after

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"SimpleYouTrackClient/{platform.system()}",
        })

    @staticmethod
    def _should_retry(status_code: int) -> bool:
        return status_code in (408, 429) or status_code >= 500

    def _get_retry_after(self, response: requests.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        if response.status_code == 429:
            return self.rate_limit_retry_after
        return self.retry_delay

    def _make_request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        retry_count = 0
        current_delay = self.retry_delay

        while retry_count <= self.max_retries:
            try:
                response = self.session.request(method=method, url=url, params=params, timeout=60)

                if response.status_code < 400:
                    return response

                if not self._should_retry(response.status_code) or retry_count >= self.max_retries:
                    response.raise_for_status()

                wait_time = self._get_retry_after(response) if response.status_code == 429 else current_delay
                logger.warning(
                    f"Request failed with status {response.status_code}. "
                    f"Retrying in {wait_time} seconds... (Attempt {retry_count + 1}/{self.max_retries})"
                )
                time.sleep(wait_time)
                if response.status_code != 429:
                    current_delay *= self.backoff_factor
                retry_count += 1

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                is_connection_error = type(e) is requests.exceptions.ConnectionError
                error_type = "Connection" if is_connection_error else "Timeout"
                error_msg = "Connection error" if is_connection_error else "Request timeout"

                if retry_count >= self.max_retries:
                    raise Exception(f"{error_type} failed after {self.max_retries} retries")

                logger.exception(
                    f"{error_msg}. "
                    f"Retrying in {current_delay} seconds... (Attempt {retry_count + 1}/{self.max_retries})"
                )
                time.sleep(current_delay)
                current_delay *= self.backoff_factor
                retry_count += 1

            except requests.exceptions.HTTPError:
                raise

            except requests.exceptions.RequestException as e:
                raise Exception(f"Request failed: {str(e)}") from e

        raise Exception(f"Request failed after {self.max_retries} retries")

    def _build_url(self, endpoint: str) -> str:
        return f"{self.base_url}/api/{endpoint.lstrip('/')}"

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET of the API. The API returns only the attributes listed in ``fields``; an unknown attribute is skipped
        without an error."""
        url = self._build_url(endpoint)
        try:
            response = self._make_request("GET", url, params=params)
            if not response.content:
                return None
            return response.json()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code == 401:
                raise YouTrackApiError(status_code, f"Unauthorized: check your YouTrack token. URL: {url}") from e
            if status_code == 404:
                raise YouTrackApiError(status_code, f"Resource not found. URL: {url}") from e
            raise YouTrackApiError(status_code, f"HTTP Error {status_code}: {_error_text(e.response)}. URL: {url}") from e
        except requests.exceptions.JSONDecodeError as e:
            raise Exception(f"Failed to decode JSON response: {str(e)}. URL: {url}") from e

    def get_all(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
        """A collection page by page via ``$skip``/``$top`` until a short page."""
        params = params or {}
        skip = 0
        items: List[Any] = []
        while True:
            page = self.get(endpoint, params={**params, "$skip": skip, "$top": self.PAGE_SIZE}) or []
            items.extend(page)
            if len(page) < self.PAGE_SIZE:
                break
            skip += len(page)
        return items

    def file_url(self, url: str) -> str:
        """Absolute url of a file: the API gives a path from the root of the host with a signature."""
        if url.startswith(("http://", "https://")):
            return url
        return f"{self._host_url}/{url.lstrip('/')}"

    def download_file(self, url: str, file_path: str, expected_size: Optional[int] = None) -> None:
        """Downloads a file through a temporary file; the size is checked against `Content-Length` and
        `expected_size`. Network errors, 408, 429, 5xx and an incomplete body are retried. A file url is signed and
        needs no token."""
        headers = {"Accept-Encoding": "identity"}
        part_path = file_path + ".part"
        delay = self.retry_delay
        for attempt in range(self.max_retries + 1):
            rate_limited = False
            try:
                with requests.get(self.file_url(url), headers=headers, stream=True, timeout=60) as response:
                    if self._should_retry(response.status_code):
                        rate_limited = response.status_code == 429
                        raise RetryableDownloadError(f"status {response.status_code}", response)
                    if response.status_code >= 400:
                        # the url is signed: it is kept out of the message
                        raise DownloadError(f"status {response.status_code}")
                    written = 0
                    with open(part_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                written += len(chunk)
                    # Content-Length of an encoded body differs from the number of decoded bytes
                    content_length = None if response.headers.get("Content-Encoding") else response.headers.get("Content-Length")
                    for expected in (content_length, expected_size):
                        if expected is not None and written != int(expected):
                            raise RetryableDownloadError(f"{written} of {expected} bytes received")
                os.replace(part_path, file_path)
                return
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
                requests.exceptions.Timeout,
                RetryableDownloadError,
            ) as e:
                if attempt >= self.max_retries:
                    raise DownloadError(f"Download failed after {self.max_retries + 1} attempts: {_describe(e)}") from None
                # 429 waits for Retry-After and does not grow the backoff, as in _make_request
                wait_time = self._get_retry_after(e.response) if rate_limited else delay
                logger.warning(f"Download failed: {_describe(e)}. Retrying in {wait_time} seconds")
                time.sleep(wait_time)
                if not rate_limited:
                    delay *= self.backoff_factor
            except requests.exceptions.RequestException as e:
                raise DownloadError(f"Download failed: {_describe(e)}") from None
            finally:
                if os.path.exists(part_path):
                    os.remove(part_path)


def _error_text(response: requests.Response) -> str:
    """``error_description`` of a JSON error of the API, else the beginning of the body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:SimpleYouTrackClient._ERROR_TEXT_LIMIT]
    if isinstance(body, dict) and (body.get("error_description") or body.get("error")):
        return str(body.get("error_description") or body.get("error"))
    return response.text[:SimpleYouTrackClient._ERROR_TEXT_LIMIT]


def _describe(error: Exception) -> str:
    """Error of a download without its url: the url carries its signature."""
    return str(error) if isinstance(error, RetryableDownloadError) else type(error).__name__


class YouTrackApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class RetryableDownloadError(Exception):
    def __init__(self, message: str, response: Optional[requests.Response] = None):
        super().__init__(message)
        self.response = response


class DownloadError(Exception):
    pass
