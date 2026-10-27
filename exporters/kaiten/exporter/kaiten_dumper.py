import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .dumpers import (
    BoardDumper,
    BoardToProjectDumper,
    BoardToQueueDumper,
    CardDumper,
    CATEGORY_NAME,
    LANE_FIELD_KEY,
    LANE_FIELD_NAME,
    RESOLUTION_KEY,
    RESOLUTION_NAME,
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
from .utils import dump_to_yaml, issue_types_by_card_type_id, sort_by_created

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
        self._board_dumper = BoardDumper()
        self._boards: List[Dict[str, Any]] = []
        self._lane_values: set = set()
        self._key_by_card_id: Dict[Any, str] = {}
        self._created_at_by_card_id: Dict[Any, Any] = {}
        self._related_card_ids: Dict[Any, set] = defaultdict(set)

    @property
    def failed_files(self) -> List[Tuple[str, str, str]]:
        return self._card_dumper.failed_files

    def dump(self) -> Iterator[BoardDumpResult]:
        self._boards = []
        self._card_dumper.failed_files.clear()
        # boards and their cards are loaded and numbered first: a relation may lead to a card of
        # another board; a board that fails here is left out of the numbering and its relations
        loaded: Dict[str, Any] = {}
        listed_card_ids: set = set()
        for board_id, queue_key in self._boards_mapper.items():
            try:
                board = self._fetcher.fetch_board(board_id)
                cards = self._fetcher.list_cards(board_id)
                # a card listed twice (moved between boards during the listing) is exported once,
                # with the board listed first
                unique_cards = {}
                for card in cards:
                    if card["id"] in listed_card_ids or card["id"] in unique_cards:
                        logger.warning(f"Board {board_id}: card {card['id']} is listed twice, exported once")
                    else:
                        unique_cards[card["id"]] = card
                cards = list(unique_cards.values())
                loaded[board_id] = (board, cards, self._number_cards(queue_key, cards))
                listed_card_ids.update(unique_cards)
            except Exception as exc:
                loaded[board_id] = exc
        self._index_cards([value for value in loaded.values() if not isinstance(value, Exception)])
        for board_id, queue_key in self._boards_mapper.items():
            value = loaded.pop(board_id)
            if isinstance(value, Exception):
                yield BoardDumpError(board_id, value)
            else:
                board, cards, _ = value
                yield self._dump_board(board_id, queue_key, board, cards)
        self._dump_boards()
        self._dump_statuses()
        self._dump_resolutions()
        self._dump_issue_types()
        self._dump_global_fields()
        self._dump_category()

    @staticmethod
    def _number_cards(queue_key: str, cards: List[Dict[str, Any]]) -> Dict[Any, str]:
        return {card["id"]: f"{queue_key}-{num}" for num, card in enumerate(sort_by_created(cards), start=1)}

    def _index_cards(self, boards: List[Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[Any, str]]]) -> None:
        """Issue keys, creation dates and related cards of the cards of all loaded boards."""
        self._key_by_card_id = {}
        self._created_at_by_card_id = {}
        for _, cards, key_by_card_id in boards:
            self._key_by_card_id.update(key_by_card_id)
            self._created_at_by_card_id.update((card["id"], card.get("created")) for card in cards)
        self._related_card_ids = defaultdict(set)
        for _, cards, _ in boards:
            for card in cards:
                for target_id, _ in CardDumper.link_targets(card):
                    if target_id in self._key_by_card_id:
                        self._related_card_ids[card["id"]].add(target_id)
                        self._related_card_ids[target_id].add(card["id"])

    def _dump_board(
        self, board_id: str, queue_key: str, board: Dict[str, Any], cards: List[Dict[str, Any]]
    ) -> BoardDumpResult:
        try:
            sprints = self._fetcher.sprints_of_board(board_id)
            # Boards carry no owner; the queue lead falls back to the token owner.
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

            cards = self._fetcher.fetch_cards_for_board(board_id, cards)
            board_sprints, sprint_by_card, tag_by_card = self._board_dumper.plan_sprints(
                board_id, sprints, self._fetcher.iterations_of_board(board_id), cards, self._fetcher.resolve_sprint
            )
            card_results = self._dump_board_cards(
                cards, queue_dump_path, lane_field_active, status_by_column_id, sprint_by_card, tag_by_card
            )
            self._boards.append(self._board_dumper.prepare(
                board,
                queue_key,
                self._board_to_queue_dumper.board_columns(board, status_by_column_id),
                board_sprints,
                # the token owner is the queue lead, not the board author
                created_by=self._board_to_queue_dumper._prepare_user(board.get("owner")),
                estimate_by_story_points=any(card.get("size") is not None for card in cards),
            ))
            return BoardDumpSuccess(board_id, queue_key, card_results)
        except Exception as exc:
            return BoardDumpError(board_id, exc)

    def _dump_board_cards(
        self,
        cards: List[Dict[str, Any]],
        queue_path: str,
        lane_field_active: bool,
        status_by_column_id: Dict[Any, str],
        sprint_by_card: Dict[Any, str],
        tag_by_card: Dict[Any, str],
    ) -> List[CardDumpResult]:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [
                executor.submit(
                    self._dump_single_card,
                    card=card,
                    queue_path=queue_path,
                    issue_key=self._key_by_card_id[card["id"]],
                    key_by_card_id=self._key_by_card_id,
                    created_at_by_card_id=self._created_at_by_card_id,
                    related_card_ids=self._related_card_ids[card["id"]],
                    status_by_column_id=status_by_column_id,
                    lane_field_active=lane_field_active,
                    sprint=sprint_by_card.get(card["id"]),
                    sprint_tag=tag_by_card.get(card["id"]),
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
        sprint: Optional[str],
        sprint_tag: Optional[str],
    ) -> CardDumpResult:
        try:
            dump_path = os.path.join(queue_path, issue_key)
            self._card_dumper.dump(
                card, dump_path, issue_key=issue_key,
                key_by_card_id=key_by_card_id, created_at_by_card_id=created_at_by_card_id,
                related_card_ids=related_card_ids,
                status_by_column_id=status_by_column_id, lane_field_active=lane_field_active,
                sprint=sprint, sprint_tag=sprint_tag,
            )
            return CardDumpSuccess(card["id"], issue_key)
        except Exception as exc:
            return CardDumpError(card["id"], exc)

    def _dump_boards(self) -> None:
        if self._boards:
            boards = self._board_to_queue_dumper._clean_empty(self._boards)
            dump_to_yaml(boards, os.path.join(self._data_path, "boards.yaml"))

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

    def _dump_resolutions(self) -> None:
        dump_to_yaml([{"key": RESOLUTION_KEY, "name": RESOLUTION_NAME}], os.path.join(self._data_path, "resolutions.yaml"))

    def _dump_category(self) -> None:
        STANDARD_ORDER_CATEGORY = 1
        dump_path = os.path.join(self._data_path, "categories.yaml")
        dump_to_yaml(
            [{"name": CATEGORY_NAME, "order": STANDARD_ORDER_CATEGORY}],
            dump_path,
        )
