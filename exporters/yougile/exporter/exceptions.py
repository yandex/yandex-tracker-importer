from typing import List


class YouGileAPIException(Exception):
    pass


class APIRequestFailedException(YouGileAPIException):

    def __init__(self, status_code: int, response_text: str):
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"API request failed with status {status_code}: {response_text}")


class MaxRetriesExceededException(YouGileAPIException):

    def __init__(self, max_retries: int, last_failure: str):
        self.max_retries = max_retries
        self.last_failure = last_failure
        super().__init__(f"Max retries ({max_retries}) exceeded, last failure: {last_failure}")


class CompanyNotFoundException(YouGileAPIException):

    def __init__(self, company_name: str):
        self.company_name = company_name
        super().__init__(f"Company {company_name} not found")


class CompanyAmbiguousException(YouGileAPIException):

    def __init__(self, company_name: str):
        self.company_name = company_name
        super().__init__(f"Several companies are named {company_name}, put the API key of the right one into configs/token.cfg")


class ApiKeyNotFoundException(YouGileAPIException):

    def __init__(self, company_name: str):
        self.company_name = company_name
        super().__init__(f"Company {company_name} has no API keys, create one or put an existing key into configs/token.cfg")


class ApiKeyRejectedException(YouGileAPIException):

    def __init__(self, status_code: int, key_source: str):
        self.status_code = status_code
        super().__init__(f"YouGile rejected {key_source} with status {status_code}")


class ConfigFileNotFoundException(YouGileAPIException):

    def __init__(self, file_name: str):
        self.file_name = file_name
        super().__init__(f"configs/{file_name}.cfg not found")


class ConfigFileEmptyException(YouGileAPIException):

    def __init__(self, file_name: str):
        self.file_name = file_name
        super().__init__(f"configs/{file_name}.cfg is empty")


class ExportConfigError(YouGileAPIException):
    """Problems of the configuration or the projects found before the export writes anything."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class ExportIncompleteError(YouGileAPIException):
    """Problems found during the export: the dumped data is incomplete."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))
