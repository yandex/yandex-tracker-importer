import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from .dumpers import (
    BoardToProjectDumper,
    BoardToQueueDumper,
    CardDumper,
    CATEGORY_NAME,
    LANE_FIELD_KEY,
    LANE_FIELD_NAME,
)
from .dump_results import (
    BoardDumpError,
    BoardDumpResult,
    BoardDumpSuccess,
    CardDumpError,
    CardDumpResult,
    CardDumpSuccess,
)
from .fetcher import KaitenDataFetcher
from .kaiten_client import SimpleKaitenClient
from .utils import dump_to_yaml, issue_types_by_card_type_id

logger = logging.getLogger(__name__)


class KaitenDumper:
    def __init__(self, client: SimpleKaitenClient, boards_mapper: Dict, data_path: str, max_workers: Optional[int] = None):
        self._client = client
        self._data_path = data_path
        self._boards_mapper = boards_mapper
        self._max_workers = max_workers or os.cpu_count()

        self._fetcher = KaitenDataFetcher(client, max_workers)
        self._fetcher.preload_reference_data()
        self._card_types = self._fetcher.fetch_card_types()
        self._custom_properties = self._fetcher.fetch_custom_properties()

        self._board_to_queue_dumper = BoardToQueueDumper(self._card_types, self._custom_properties)
        self._board_to_project_dumper = BoardToProjectDumper()
        self._card_dumper = CardDumper(client, self._fetcher, self._custom_properties, self._card_types)
        self._lane_values: set = set()

    def dump(self) -> Iterator[BoardDumpResult]:
        for board_id, queue_key in self._boards_mapper.items():
            yield self._dump_board(board_id, queue_key)
        self._dump_statuses()
        self._dump_issue_types()
        self._dump_global_fields()
        self._dump_category()

    def _dump_board(self, board_id: str, queue_key: str) -> BoardDumpResult:
        try:
            board = self._fetcher.fetch_board(board_id)
            self._fetcher.preload_sprints(board_id)
            # Boards carry no owner; fall back to the authenticated user so the
            # queue always has a (required) lead.
            lead = self._board_to_queue_dumper._prepare_user(
                board.get("owner") or self._fetcher.current_user
            )

            queue_dump_path = os.path.join(self._data_path, "queues", queue_key)
            status_by_column_id = self._board_to_queue_dumper.dump(
                board, queue_dump_path, queue_key=queue_key, lead=lead
            )

            # a title is not unique across spaces and may contain a path separator
            project_dir = re.sub(r"[\\/]", "_", f"{board.get('title') or 'board'}_{board_id}")
            project_dump_path = os.path.join(self._data_path, "projects", project_dir)
            self._board_to_project_dumper.dump(board, project_dump_path, created_by=lead)

            # Swimlanes only carry meaning when a board has more than one lane.
            lane_titles = [lane.get("title") for lane in board.get("lanes", []) if lane.get("title")]
            lane_field_active = len(lane_titles) > 1
            if lane_field_active:
                self._lane_values.update(lane_titles)

            card_results = self._dump_board_cards(
                board_id, queue_dump_path, queue_key, lane_field_active, status_by_column_id
            )
            return BoardDumpSuccess(board_id, queue_key, card_results)
        except Exception as exc:
            return BoardDumpError(board_id, exc)

    def _dump_board_cards(
        self, board_id: str, queue_path: str, queue_key: str, lane_field_active: bool, status_by_column_id: Dict[Any, str]
    ) -> List[CardDumpResult]:
        cards = sorted(
            self._fetcher.fetch_cards_for_board(board_id),
            key=lambda card: card.get("created") or datetime.now().isoformat(),
        )

        # Assign issue keys up front so card relations (children/parents) can be
        # resolved to keys while dumping.
        key_by_card_id = {card["id"]: f"{queue_key}-{num}" for num, card in enumerate(cards, start=1)}
        created_at_by_card_id = {card["id"]: card.get("created") for card in cards}
        related_card_ids = defaultdict(set)
        for card in cards:
            for target_id, _ in CardDumper.link_targets(card):
                if target_id in key_by_card_id:
                    related_card_ids[card["id"]].add(target_id)
                    related_card_ids[target_id].add(card["id"])

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [
                executor.submit(
                    self._dump_single_card,
                    card=card,
                    queue_path=queue_path,
                    issue_key=key_by_card_id[card["id"]],
                    key_by_card_id=key_by_card_id,
                    created_at_by_card_id=created_at_by_card_id,
                    related_card_ids=related_card_ids[card["id"]],
                    status_by_column_id=status_by_column_id,
                    lane_field_active=lane_field_active,
                )
                for card in cards
            ]
            return [future.result() for future in futures]

    def _dump_single_card(
        self,
        card: dict,
        issue_key: str,
        queue_path: str,
        key_by_card_id: dict,
        created_at_by_card_id: dict,
        related_card_ids: set,
        status_by_column_id: dict,
        lane_field_active: bool,
    ) -> CardDumpResult:
        try:
            dump_path = os.path.join(queue_path, issue_key)
            self._card_dumper.dump(
                card, dump_path, issue_key=issue_key,
                key_by_card_id=key_by_card_id, created_at_by_card_id=created_at_by_card_id,
                related_card_ids=related_card_ids,
                status_by_column_id=status_by_column_id, lane_field_active=lane_field_active,
            )
            return CardDumpSuccess(card["id"], issue_key)
        except Exception as exc:
            return CardDumpError(card["id"], exc)

    def _dump_statuses(self) -> None:
        dump_path = os.path.join(self._data_path, "statuses.yaml")
        dump_to_yaml(
            [
                {"key": key, "name": {"en": status["name"], "ru": status["name"]}, "type": status["type"]}
                for key, status in self._board_to_queue_dumper.collected_statuses.items()
            ],
            dump_path,
        )

    def _dump_issue_types(self) -> None:
        types = []
        seen = set()
        for key, name in issue_types_by_card_type_id(self._card_types).values():
            if key in seen:
                continue
            seen.add(key)
            types.append({"key": key, "name": {"en": name, "ru": name}})
        if types:
            dump_to_yaml(types, os.path.join(self._data_path, "issue_types.yaml"))

    def _dump_global_fields(self) -> None:
        if not self._lane_values:
            return
        fields = [
            {
                "key": LANE_FIELD_KEY,
                "name": LANE_FIELD_NAME,
                "category": CATEGORY_NAME["ru"],
                "schema": {"type": "string"},
                "optionsProvider": {
                    "type": "FixedListOptionsProvider",
                    "values": sorted(self._lane_values),
                },
            }
        ]
        dump_to_yaml(fields, os.path.join(self._data_path, "global_fields.yaml"))

    def _dump_category(self) -> None:
        STANDARD_ORDER_CATEGORY = 1
        dump_path = os.path.join(self._data_path, "categories.yaml")
        dump_to_yaml(
            [{"name": CATEGORY_NAME, "order": STANDARD_ORDER_CATEGORY}],
            dump_path,
        )
