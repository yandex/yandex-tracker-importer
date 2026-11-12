import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


class BitrixApiError(Exception):
    """``rejected`` is set when the API answered the request with an error that a retry does not change; ``code`` is
    the API error code of a rejected request with one error. A rejected ``batch`` carries ``results`` of all commands,
    ``None`` for rejected ones, and ``failures``: the error of each rejected command by its index."""

    def __init__(
        self,
        message: str,
        rejected: bool = False,
        code: Optional[str] = None,
        results: Optional[List[Any]] = None,
        failures: Optional[Dict[int, Dict[str, Any]]] = None,
    ):
        super().__init__(message)
        self.rejected = rejected
        self.code = code
        self.results = results
        self.failures = failures or {}


def build_query(params: Dict[str, Any], prefix: Optional[str] = None) -> str:
    """Encode parameters as a PHP-style query string: ``filter[ID][0]=1``."""
    parts = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            nested = build_query(value, name)
        elif isinstance(value, (list, tuple, set)):
            nested = build_query({str(i): item for i, item in enumerate(value)}, name)
        else:
            nested = f"{quote(name)}={quote(str(value))}"
        if nested:
            parts.append(nested)
    return "&".join(parts)


def _page_start(value: Any) -> Optional[int]:
    """Return a page offset as ``int``, or ``None`` if ``value`` is not a non-negative integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None
    return None


class SimpleBitrixClient:
    """Bitrix24 REST API client authenticated by an inbound webhook.

    Limits: about two requests per second (``QUERY_LIMIT_EXCEEDED``) and an execution
    time budget per method within a 10-minute window (``OPERATION_TIME_LIMIT``).
    Webhook credentials are excluded from errors and logs.
    """

    BATCH_SIZE = 50
    OPERATION_LIMIT_WINDOW = 600.0
    _RETRY_STATUSES = (408, 429, 500, 502, 503, 504)
    _RETRY_ERRORS = ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "INTERNAL_SERVER_ERROR")
    _RETRY_EXCEPTIONS = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    _URL_EXCEPTIONS = (
        requests.exceptions.MissingSchema,
        requests.exceptions.InvalidSchema,
        requests.exceptions.InvalidURL,
    )

    def __init__(
        self,
        webhook_url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        min_request_interval: float = 0.5,
        timeout: float = 120.0,
    ):
        self.webhook_url = webhook_url.strip().rstrip("/") + "/"
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.min_request_interval = min_request_interval
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    @property
    def portal_url(self) -> str:
        """Portal address without the webhook path, for relative download links."""
        return self.webhook_url.split("/rest/")[0]

    def mask(self, text: Any) -> str:
        """Replace the webhook path and code in ``text`` with ``***``."""
        text = str(text)
        webhook_path = self.webhook_url.split("/rest/", 1)[-1].rstrip("/")
        secrets = [self.webhook_url.rstrip("/")]
        if "/" in webhook_path:
            secrets += [webhook_path, quote(webhook_path, safe=""), webhook_path.split("/")[1]]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait_time = self._last_request_at + self.min_request_interval - time.monotonic()
            if wait_time > 0:
                time.sleep(wait_time)
            self._last_request_at = time.monotonic()

    def _operation_limit_wait(self, time_info: Any) -> float:
        reset_at = time_info.get("operating_reset_at") if isinstance(time_info, dict) else None
        try:
            reset_at = float(reset_at)
        except (TypeError, ValueError):
            return self.OPERATION_LIMIT_WINDOW
        if not math.isfinite(reset_at):
            return self.OPERATION_LIMIT_WINDOW
        return min(max(reset_at - time.time(), 0.0), self.OPERATION_LIMIT_WINDOW) + 1.0

    def _wait_before_retry(self, method: str, failure: str, wait_time: float, attempt: int) -> None:
        logger.warning(
            f"{method}: {failure}. Retrying in {wait_time:.0f} seconds... (Attempt {attempt + 1}/{self.max_retries})"
        )
        time.sleep(wait_time)

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call a method and return the whole response: ``result``, ``total``, ``next``, ``time``."""
        url = f"{self.webhook_url}{method}.json"
        retry_count = 0
        current_delay = self.retry_delay

        while True:
            self._throttle()
            wait_time = current_delay
            fatal = None
            response = None
            try:
                response = self.session.post(url, json=params or {}, timeout=self.timeout, allow_redirects=False)
            except self._RETRY_EXCEPTIONS as e:
                failure = f"{type(e).__name__}: {self.mask(e)}"
            except self._URL_EXCEPTIONS as e:
                fatal = f"{type(e).__name__}: webhook URL is not recognized, check configs/webhook_url"
            except requests.exceptions.RequestException as e:
                fatal = f"{type(e).__name__}: {self.mask(e)}"

            if response is not None and 300 <= response.status_code < 400:
                fatal = f"HTTP {response.status_code} redirect, check configs/webhook_url"

            # Raise outside except: the original exception may contain the webhook path.
            if fatal:
                raise BitrixApiError(f"{method}: {fatal}")

            if response is not None:
                try:
                    data = response.json()
                except ValueError:
                    data = None

                if not isinstance(data, dict):
                    failure = f"HTTP {response.status_code}, response is not a JSON object"
                    if response.status_code not in self._RETRY_STATUSES:
                        raise BitrixApiError(f"{method}: {failure}")
                else:
                    error = data.get("error")
                    if response.status_code < 400 and not error:
                        return data

                    failure = self.mask(f"HTTP {response.status_code} {error}: {data.get('error_description')}")
                    if response.status_code not in self._RETRY_STATUSES and error not in self._RETRY_ERRORS:
                        raise BitrixApiError(f"{method}: {failure}", rejected=True, code=error)
                    if error == "OPERATION_TIME_LIMIT":
                        wait_time = self._operation_limit_wait(data.get("time"))

            if retry_count >= self.max_retries:
                raise BitrixApiError(f"{method}: {failure} after {self.max_retries} retries")

            self._wait_before_retry(method, failure, wait_time, retry_count)
            current_delay *= self.backoff_factor
            retry_count += 1

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Call a method and return its ``result``."""
        return self._request(method, params).get("result")

    def get_list(self, method: str, params: Optional[Dict[str, Any]] = None, key: Optional[str] = None) -> List[Any]:
        """Fetch all pages of a list method; ``key`` names the list inside ``result``."""
        page_params = dict(params or {})
        start = _page_start(page_params.get("start", 0))
        if start is None:
            raise BitrixApiError(f"{method}: invalid page start")
        items: List[Any] = []

        while True:
            data = self._request(method, page_params)
            page = data.get("result")
            if page in (None, []):
                page = []
            elif key is not None:
                if not isinstance(page, dict) or key not in page:
                    raise BitrixApiError(f"{method}: no '{key}' list in result")
                page = page[key]
                if page is None:
                    page = []
            if not isinstance(page, list):
                raise BitrixApiError(f"{method}: expected a list, got {type(page).__name__}")
            items.extend(page)

            if data.get("next") is None:
                return items
            next_start = _page_start(data["next"])
            if next_start is None:
                raise BitrixApiError(f"{method}: invalid 'next' in response")
            if next_start <= start:
                raise BitrixApiError(f"{method}: pagination does not advance")
            start = next_start
            page_params = dict(page_params, start=start)

    def batch(self, method: str, items: List[Dict[str, Any]]) -> List[Any]:
        """Call a method with each parameter set via ``batch``; results keep the input order.

        A list result longer than one page is fetched to the end. A command with a temporary error is resent alone.
        Commands the API rejects raise ``BitrixApiError`` after all chunks, with the results of the other commands.
        """
        results: List[Any] = []
        failures: Dict[int, Dict[str, Any]] = {}

        for chunk_start in range(0, len(items), self.BATCH_SIZE):
            chunk = items[chunk_start:chunk_start + self.BATCH_SIZE]
            commands = {f"cmd{i}": f"{method}?{build_query(params)}" for i, params in enumerate(chunk)}
            command_results, next_starts, rejected = self._batch_chunk(method, commands)

            for offset, (label, params) in enumerate(zip(commands, chunk)):
                if label in rejected:
                    error = rejected[label]
                    failures[chunk_start + offset] = error if isinstance(error, dict) else {"error": error}
                    results.append(None)
                    continue
                result = command_results.get(label)
                if label in next_starts:
                    if not isinstance(result, list):
                        raise BitrixApiError(f"{method}: paged result is not a list")
                    next_start = _page_start(next_starts[label])
                    if next_start is None or next_start <= (_page_start(params.get("start", 0)) or 0):
                        raise BitrixApiError(f"{method}: invalid 'next' in batch response")
                    try:
                        result = result + self.get_list(method, dict(params, start=next_start))
                    except BitrixApiError as e:
                        if not e.rejected:
                            raise
                        failures[chunk_start + offset] = {"error": e.code, "error_description": str(e)}
                        results.append(None)
                        continue
                results.append(result)

        if failures:
            codes = {failure.get("error") for failure in failures.values()}
            raise BitrixApiError(
                self.mask(f"{method}: batch errors in {len(failures)} of {len(items)} commands: {list(failures.values())[:3]}"),
                rejected=True,
                code=next(iter(codes)) if len(codes) == 1 else None,
                results=results,
                failures={index: {key: self.mask(value) for key, value in failure.items()} for index, failure in failures.items()},
            )
        return results

    def _batch_chunk(
        self, method: str, commands: Dict[str, str]
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """Send one ``batch`` and resend the commands with temporary errors. Return the results, the next page starts
        and the errors of the rejected commands by label."""
        results: Dict[str, Any] = {}
        next_starts: Dict[str, Any] = {}
        rejected: Dict[str, Any] = {}
        pending = dict(commands)
        retry_count = 0
        current_delay = self.retry_delay

        while True:
            response = self.call("batch", {"halt": 0, "cmd": pending}) or {}
            # Bitrix24 returns an empty mapping as an empty list
            page = response.get("result") or {}
            errors = response.get("result_error") or {}
            nexts = response.get("result_next") or {}
            page, errors, nexts = (value if isinstance(value, dict) else {} for value in (page, errors, nexts))
            for label in pending:
                if label not in errors:
                    results[label] = page.get(label)
                    if label in nexts:
                        next_starts[label] = nexts[label]

            temporary = {}
            for label, error in errors.items():
                code = error.get("error") if isinstance(error, dict) else error
                if code in self._RETRY_ERRORS:
                    temporary[label] = error
                else:
                    rejected[label] = error
            if not temporary:
                return results, next_starts, rejected

            failure = self.mask(f"batch errors: {temporary}")
            if retry_count >= self.max_retries:
                raise BitrixApiError(f"{method}: {failure} after {self.max_retries} retries")

            wait_time = current_delay
            if any(isinstance(error, dict) and error.get("error") == "OPERATION_TIME_LIMIT" for error in temporary.values()):
                times = response.get("result_time") or {}
                wait_time = max(
                    (
                        self._operation_limit_wait(times.get(label))
                        for label, error in temporary.items()
                        if isinstance(error, dict) and error.get("error") == "OPERATION_TIME_LIMIT"
                    ),
                    default=self.OPERATION_LIMIT_WINDOW,
                )

            self._wait_before_retry(method, failure, wait_time, retry_count)
            current_delay *= self.backoff_factor
            retry_count += 1
            pending = {label: commands[label] for label in temporary}
