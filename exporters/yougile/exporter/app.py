import logging
import os
import sys
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .api_client import YouGileApi
from .exceptions import (
    ConfigFileEmptyException,
    ConfigFileNotFoundException,
    ExportConfigError,
    ExportIncompleteError,
    YouGileAPIException,
)
from .dumper import YouGileDumper
from .key_state import KeyState


log = logging.getLogger(__name__)


def init_logging(dump_path: str) -> None:
    logs_path = os.path.join(dump_path, "logs")
    os.makedirs(logs_path, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(
        os.path.join(logs_path, f"export-{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log")
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)


def _get_dump_objects_from_config(project_path: str, filename: str) -> List[str]:
    # lines as they are: errors refer to line numbers of the file
    return config.get_config_content(project_path, filename, strip=False).splitlines()


def _create_yougile_client(project_path: str) -> YouGileApi:
    api_key = config.get_config_content(project_path, "token", validate_file_exist=False)
    if api_key:
        return YouGileApi(api_key.splitlines()[0].strip())

    if config.get_config_file(project_path, "login") is None:
        if config.get_config_file(project_path, "token") is not None:
            raise ConfigFileEmptyException("token")
        raise ConfigFileNotFoundException("token")

    login = config.get_config_content(project_path, "login")
    password = config.get_config_content(project_path, "password")
    company = config.get_config_content(project_path, "company")
    return YouGileApi.from_credentials(login, password, company)


def get_timezone(project_path: str) -> str:
    """Return the time zone of task dates from configs/timezone.cfg, UTC by default."""
    time_zone = (config.get_config_content(project_path, "timezone", validate_file_exist=False) or "UTC").strip()
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ExportConfigError([f"Unknown time zone '{time_zone}' in configs/timezone.cfg"]) from None
    return time_zone


def get_yougile_client(project_path: str) -> YouGileApi:
    client = _create_yougile_client(project_path)
    client.check_api_key()
    return client


def main() -> None:
    project_path = os.getcwd()
    init_logging(project_path)
    data_path = os.path.join(project_path, "data")
    os.makedirs(data_path, exist_ok=True)

    try:
        time_zone = get_timezone(project_path)
        client = get_yougile_client(project_path)
        try:
            projects = _get_dump_objects_from_config(project_path, "projects")
        except ConfigFileNotFoundException:
            raise ExportConfigError(["configs/projects.cfg not found: add lines <prefix>:<YouGile project title>"]) from None

        report_path = os.path.join(project_path, "logs", f"renumbered-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv")
        YouGileDumper(client, data_path, time_zone, report_path, KeyState(project_path)).dump(projects)
    except ExportConfigError as e:
        _report_errors(e.errors, "Nothing is exported: fix the problems above and run the export again")
        sys.exit(1)
    except ExportIncompleteError as e:
        _report_errors(
            e.errors,
            "The export is incomplete: see logs/export-<date>.log and run the export again",
        )
        sys.exit(1)
    except YouGileAPIException as e:
        _report_errors([str(e)], "Nothing is exported: fix the problem above and run the export again")
        sys.exit(1)


def _report_errors(errors: List[str], summary: str) -> None:
    for error in errors:
        log.error(error)
        print(error, file=sys.stderr)
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
