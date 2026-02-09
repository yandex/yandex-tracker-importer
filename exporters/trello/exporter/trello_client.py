import requests
import platform
import time
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class SimpleTrelloClient:

    def __init__(
        self,
        api_key: str,
        token: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        backoff_factor: float = 2.0,
        rate_limit_retry_after: float = 10.0
    ):
        self.api_key = api_key
        self.token = token
        self.base_url = 'https://api.trello.com/1'
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.backoff_factor = backoff_factor
        self.rate_limit_retry_after = rate_limit_retry_after

        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'User-Agent': f'SimpleTrelloClient/{platform.system()}'
        })

    def _should_retry(self, status_code: int) -> bool:
        return status_code in [429, 502, 503, 504, 408]

    def _get_retry_after(self, response: requests.Response) -> float:
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass

        if response.status_code == 429:
            return self.rate_limit_retry_after
        return self.retry_delay

    def _make_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> requests.Response:
        retry_count = 0
        current_delay = self.retry_delay

        while retry_count <= self.max_retries:
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    timeout=30,
                    **kwargs
                )

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

            except requests.exceptions.RequestException as e:
                raise Exception(f"Request failed: {str(e)}")

        raise Exception(f"Request failed after {self.max_retries} retries")

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query_params = params or {}
        query_params['key'] = self.api_key
        query_params['token'] = self.token

        if endpoint.startswith('/'):
            endpoint = endpoint[1:]
        url = f'{self.base_url}/{endpoint}'

        try:
            response = self._make_request('GET', url, params=query_params)
            return response.json()

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                raise Exception(f"Unauthorized: Check your API key and token. URL: {url}")
            elif e.response.status_code == 404:
                raise Exception(f"Resource not found. URL: {url}")
            else:
                raise Exception(f"HTTP Error {e.response.status_code}: {e.response.text}. URL: {url}")
        except requests.exceptions.JSONDecodeError as e:
            raise Exception(f"Failed to decode JSON response: {str(e)}")

    def download_attachment(self, url, file_path):
        headers = {
            "Authorization": (
                f'OAuth oauth_consumer_key="{self.api_key}",'
                f'oauth_token="{self.token}"'
            )
        }
        with requests.get(url, headers=headers, stream=True) as response:
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
