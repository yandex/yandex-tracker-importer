import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Iterator, List, Dict, Optional

from .dumpers import BoardToProjectDumper, BoardToQueueDumper, CardDumper
from .dump_results import (
    BoardDumpResult,
    BoardDumpError,
    BoardDumpSuccess,
    CardDumpResult,
    CardDumpError,
    CardDumpSuccess
)
from .fetcher import TrelloDataFetcher
from .trello_client import SimpleTrelloClient
from .utils import dump_to_yaml


class TrelloDumper:
    def __init__(self, client: SimpleTrelloClient, boards_mapper: Dict, data_path: str, max_workers: Optional[int] = None):
        self._client = client
        self._data_path = data_path
        self._boards_mapper = boards_mapper
        self._max_workers = max_workers or os.cpu_count()

        self._fetcher = TrelloDataFetcher(client, max_workers)
        self._board_to_queue_dumper = BoardToQueueDumper()
        self._board_to_project_dumper = BoardToProjectDumper()
        self._card_dumper = CardDumper(client)

    def dump(self) -> Iterator[BoardDumpResult]:
        for board_id, queue_key in self._boards_mapper.items():
            yield self._dump_board(board_id, queue_key)
        self._dump_lists()
        self._dump_category()

    def _dump_board(self, board_id: str, queue_key: str) -> BoardDumpResult:
        try:
            board_data = self._fetcher.fetch_board(board_id)

            queue_dump_path = os.path.join(self._data_path, "queues", queue_key)
            self._board_to_queue_dumper.dump(board_data, queue_dump_path, queue_key=queue_key)

            project_dump_path = os.path.join(self._data_path, "projects", board_data.get("name", "UNKNOWN"))
            self._board_to_project_dumper.dump(board_data, project_dump_path)

            card_results = self._dump_board_cards(board_id, queue_dump_path, queue_key)

            return BoardDumpSuccess(board_id, queue_key, card_results)
        except Exception as exc:
            return BoardDumpError(board_id, exc)

    def _dump_board_cards(self, board_id: str, queue_path: str, queue_key: str) -> List[CardDumpResult]:
        cards = sorted(
            self._fetcher.fetch_cards_for_board(board_id),
            key=lambda card_data: datetime.fromisoformat(
                card_data.get("creation_info", {}).get("date")
                or datetime.now().isoformat()
            )
        )

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            card_dump_results = []
            for num, card in enumerate(cards, start=1):
                issue_key = f"{queue_key}-{num}"
                future = executor.submit(
                    self._dump_single_card,
                    card_data=card,
                    queue_path=queue_path,
                    issue_key=issue_key
                )
                card_dump_results.append(future.result())

        return card_dump_results

    def _dump_single_card(self, card_data: dict, issue_key: str, queue_path: str) -> CardDumpResult:
        try:
            dump_path = os.path.join(queue_path, issue_key)
            self._card_dumper.dump(card_data, dump_path, issue_key=issue_key)
            return CardDumpSuccess(card_data["id"], issue_key)
        except Exception as exc:
            return CardDumpError(card_data["id"], exc)

    def _dump_lists(self) -> None:
        dump_path = os.path.join(self._data_path, "statuses.yaml")
        dump_to_yaml(
            [
                {
                    "key": key,
                    "name": {"en": name, "ru": name}
                }
                for key, name in self._board_to_queue_dumper.collected_statuses.items()
            ],
            dump_path
        )

    def _dump_category(self) -> None:
        STANDARD_ORDER_CATEGORY = 1
        dump_path = os.path.join(self._data_path, "categories.yaml")
        dump_to_yaml(
            [
                {
                    "name": {"en": "From Trello", "ru": "Из Trello"},
                    "order": STANDARD_ORDER_CATEGORY
                }
            ],
            dump_path
        )
