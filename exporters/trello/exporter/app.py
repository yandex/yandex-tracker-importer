import argparse
import logging
import os
from datetime import datetime
from tqdm import tqdm

from .config import get_config_content, get_generic_keys_mapping
from .trello_client import SimpleTrelloClient
from .trello_dumper import TrelloDumper


logger = logging.getLogger(__name__)


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


def get_trello_client(api_key: str, token: str) -> SimpleTrelloClient:
    return SimpleTrelloClient(api_key=api_key, token=token)


def parse_args():
    parser = argparse.ArgumentParser(description='Trello data exporter')
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='Number of worker threads to use (default: number of CPU cores)'
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_path = os.getcwd()

    init_logging(project_path)
    data_path = os.path.join(project_path, "data")
    os.makedirs(data_path, exist_ok=True)
    source_api = get_config_content(project_path, "source_trello_api")
    source_token = get_config_content(project_path, "source_trello_token")
    source_trello_client = get_trello_client(source_api, source_token)
    boards_mapper = get_generic_keys_mapping(project_path, "boards")

    dumper = TrelloDumper(source_trello_client, boards_mapper, data_path, max_workers=args.workers)
    for result in tqdm(dumper.dump(), total=len(boards_mapper), desc="Boards"):
        result.log()


if __name__ == "__main__":
    main()
