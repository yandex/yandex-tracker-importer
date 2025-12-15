import argparse
import logging
import os
from datetime import datetime

import yandex_tracker_client as tracker_client

from .config import load_config
from .mapper import EntityMapper
from .tracker_importer import TrackerImporter


def init_logging(dump_path: str, log_level: str = "INFO") -> None:
    logs_path = os.path.join(dump_path, "logs")
    os.makedirs(logs_path, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper()))
    formatter = logging.Formatter('%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(
        os.path.join(logs_path, f'import-{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.log')
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)


def get_tracker_client(api_url: str, token: str, org_id: str = None, cloud_org_id: str = None):
    return tracker_client.TrackerClient(
        token=token,
        org_id=org_id,
        base_url=api_url,
        cloud_org_id=cloud_org_id,
        api_version="v3",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import data to Yandex Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum number of parallel workers (default: number of CPU cores)"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set logging level (default: INFO)"
    )

    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Data directory (default: 'data')"
    )

    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    project_path = os.getcwd()

    init_logging(project_path, args.log_level)

    cfg = load_config(project_path)
    dest_client = get_tracker_client(
        cfg.dest_tracker_api,
        cfg.dest_tracker_token,
        cfg.dest_org_id,
        cfg.dest_cloud_org_id,
    )

    entity_mapper = EntityMapper(project_path)
    data_path = os.path.join(project_path, args.data_dir or "data")

    importer = TrackerImporter(
        client=dest_client,
        mapper=entity_mapper,
        data_path=data_path,
        max_workers=args.max_workers
    )
    progress_tracker = importer.do_import()

    progress_tracker.close_all()
    progress_tracker.statistics.print_statistics()


if __name__ == "__main__":
    main()
