import argparse
import logging
import os
import sys
from datetime import datetime

from tqdm import tqdm

from .config import get_config_content, get_generic_keys_mapping
from .dump_results import build_summary
from .weeek_client import SimpleWeeekClient
from .weeek_dumper import WeeekDumper


logger = logging.getLogger(__name__)


def init_logging(dump_path: str) -> None:
    logs_path = os.path.join(dump_path, "logs")
    os.makedirs(logs_path, exist_ok=True)

    root_logger = logging.getLogger()
    # the log file gets debug records; HTTP libraries log warnings only
    root_logger.setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    formatter = logging.Formatter("%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(
        os.path.join(logs_path, f"export-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)


def get_weeek_client(token: str) -> SimpleWeeekClient:
    return SimpleWeeekClient(token=token)


def parse_args():
    parser = argparse.ArgumentParser(description="WEEEK data exporter")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of worker threads to use (default: number of CPU cores)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_path = os.getcwd()

    init_logging(project_path)
    data_path = os.path.join(project_path, "data")
    os.makedirs(data_path, exist_ok=True)

    source_token = get_config_content(project_path, "source_weeek_token")
    source_client = get_weeek_client(source_token)

    projects_mapper = get_generic_keys_mapping(project_path, "projects")
    users_mapping = get_generic_keys_mapping(project_path, "users", validate_file_exist=False)

    dumper = WeeekDumper(source_client, projects_mapper, users_mapping, data_path, max_workers=args.workers)
    results = []
    for result in tqdm(dumper.dump(), total=len(projects_mapper), desc="Projects"):
        result.log()
        results.append(result)
    summary = build_summary(results, dumper.failed_items, dumper.unknown_users, dumper.failed_reference_data)
    if summary:
        message = "Export finished with errors:\n" + "\n".join(summary)
        logger.error(message)
    else:
        message = "Export finished without errors"
        logger.info(message)
    print(message)
    if summary:
        sys.exit(1)


if __name__ == "__main__":
    main()
