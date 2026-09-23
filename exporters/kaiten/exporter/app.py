import argparse
import logging
import os
from datetime import datetime

from tqdm import tqdm

from .config import get_config_content, get_generic_keys_mapping
from .kaiten_client import SimpleKaitenClient
from .kaiten_dumper import KaitenDumper


logger = logging.getLogger(__name__)


def init_logging(dump_path: str) -> None:
    logs_path = os.path.join(dump_path, "logs")
    os.makedirs(logs_path, exist_ok=True)

    root_logger = logging.getLogger()
    # DEBUG so the file handler below actually receives debug records; noisy HTTP
    # libraries are pinned to WARNING to keep the log readable.
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


def build_base_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    if not url.endswith("/api/v1"):
        url = f"{url}/api/v1"
    return url


def get_kaiten_client(base_url: str, token: str) -> SimpleKaitenClient:
    return SimpleKaitenClient(base_url=base_url, token=token)


def parse_args():
    parser = argparse.ArgumentParser(description="Kaiten data exporter")
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

    source_url = get_config_content(project_path, "source_kaiten_url")
    source_token = get_config_content(project_path, "source_kaiten_token")
    source_client = get_kaiten_client(build_base_url(source_url), source_token)

    boards_mapper = get_generic_keys_mapping(project_path, "boards")

    dumper = KaitenDumper(source_client, boards_mapper, data_path, max_workers=args.workers)
    for result in tqdm(dumper.dump(), total=len(boards_mapper), desc="Boards"):
        result.log()


if __name__ == "__main__":
    main()
