import logging
import os
import sys
from datetime import datetime
from typing import Any, List, Optional

from .bitrix_client import SimpleBitrixClient
from .bitrix_dumper import BitrixDumper
from .config import get_config_content, get_config_file, get_yaml_config


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

    # urllib3 warnings include request URLs, which contain the webhook path
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def get_bitrix_client(webhook_url: str) -> SimpleBitrixClient:
    return SimpleBitrixClient(webhook_url)


def get_group_ids(project_path: str) -> Optional[List[str]]:
    """Group ids of ``configs/.projects``, one per line; blank lines and lines starting with ``#`` are skipped. No file
    gives ``None``. Raises if an id is not a number."""
    if get_config_file(project_path, "projects") is None:
        return None
    lines = [line.strip() for line in get_config_content(project_path, "projects").splitlines()]
    group_ids = [line for line in lines if line and not line.startswith("#")]
    not_numbers = [group_id for group_id in group_ids if not (group_id.isascii() and group_id.isdigit())]
    if not_numbers:
        raise ValueError(f"Group ids in configs/.projects are not numbers: {', '.join(not_numbers)}")
    return list(dict.fromkeys(str(int(group_id)) for group_id in group_ids))


def check_group_config(project_path: str, tasks_filter: Any) -> None:
    """Raise if ``configs/.projects`` is used with ``projects_filter`` or with a tasks filter that is not a mapping or
    has its own condition on the group."""
    if get_config_file(project_path, "projects_filter") is not None:
        raise ValueError("configs/.projects and projects_filter are both set: keep one of them")
    if tasks_filter is None:
        return
    if not isinstance(tasks_filter, dict):
        raise ValueError("tasks_filter is not a mapping")
    group_conditions = [key for key in tasks_filter if str(key).endswith("GROUP_ID") and key != "GROUP_ID"]
    if group_conditions:
        raise ValueError(
            f"tasks_filter has conditions on the group with configs/.projects: {', '.join(map(str, group_conditions))}"
        )


def main() -> None:
    project_path = os.getcwd()
    init_logging(project_path)

    data_path = os.path.join(project_path, "data")
    os.makedirs(data_path, exist_ok=True)

    webhook_url = get_config_content(project_path, "webhook_url")
    projects_filter = get_yaml_config(project_path, "projects_filter")
    tasks_filter = get_yaml_config(project_path, "tasks_filter")
    group_ids = get_group_ids(project_path)
    if group_ids is not None:
        check_group_config(project_path, tasks_filter)

    source_client = get_bitrix_client(webhook_url)
    failures = BitrixDumper(source_client, data_path).dump(
        projects_filter=projects_filter, tasks_filter=tasks_filter, group_ids=group_ids
    )

    if any(failures.values()):
        summary = "; ".join(f"{kind}: {', '.join(items)}" for kind, items in failures.items() if items)
        logging.getLogger(__name__).error(f"Export finished with failures: {summary}")
        print(f"Export finished with failures, see logs: {summary}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
