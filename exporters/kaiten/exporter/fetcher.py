import functools
import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from .kaiten_client import SimpleKaitenClient

logger = logging.getLogger(__name__)


class KaitenDataFetcher:
    """Reads boards, cards and their sub-resources from the Kaiten REST API.

    Kaiten has no batch endpoint (unlike Trello), so per-card sub-resources
    (comments, files, checklists, time logs) are fetched with individual requests
    parallelised over a thread pool. A company-wide user map is preloaded once so
    that entities that only carry a ``*_id`` (comment author, checklist responsible,
    time-log user) can be resolved to a display name/email.
    """

    def __init__(self, client: SimpleKaitenClient, max_workers: Optional[int] = None):
        self._client = client
        self._max_workers = max_workers or os.cpu_count()
        self._users_by_id: Dict[Any, Dict[str, Any]] = {}
        self._sprints_by_id: Dict[Any, Dict[str, Any]] = {}
        self._all_sprints: Optional[List[Dict[str, Any]]] = None
        self._space_uids_by_board: Optional[Dict[str, List[str]]] = None
        self._spaces: Optional[List[Dict[str, Any]]] = None
        self._spaces_loaded = False
        self._space_users: Dict[Any, Optional[List[Dict[str, Any]]]] = {}
        self._space_role_names: Optional[Dict[str, str]] = None
        self._iterations_by_space: Dict[str, List[Dict[str, Any]]] = {}
        self._current_user: Optional[Dict[str, Any]] = None
        self._timeline_timezone = "UTC"

    # -- shared reference data ------------------------------------------------

    def preload_reference_data(self) -> None:
        self._users_by_id = {user["id"]: user for user in self.fetch_users()}
        self._current_user = self.fetch_current_user()
        self._timeline_timezone = self.fetch_timeline_timezone()

    def fetch_timeline_timezone(self) -> str:
        """Time zone of the company timeline; card dates are converted to days in it."""
        try:
            timezone = (self._client.get("/companies/current") or {}).get("timeline_timezone")
            if not timezone:
                logger.warning("The company has no timeline time zone, dates are taken in UTC")
                return "UTC"
            ZoneInfo(timezone)
            return timezone
        except Exception:
            logger.exception("Failed to fetch the timeline time zone, dates are taken in UTC")
            return "UTC"

    @property
    def timeline_timezone(self) -> str:
        return self._timeline_timezone

    def fetch_current_user(self) -> Optional[Dict[str, Any]]:
        try:
            return self._client.get("/users/current")
        except Exception:
            logger.exception("Failed to fetch current user")
            return None

    @property
    def current_user(self) -> Optional[Dict[str, Any]]:
        return self._current_user

    def fetch_users(self) -> List[Dict[str, Any]]:
        try:
            return self._client.get_paginated("/users") or []
        except Exception:
            logger.exception("Failed to fetch users")
            return []

    def fetch_card_types(self) -> List[Dict[str, Any]]:
        try:
            return self._client.get("/card-types") or []
        except Exception:
            logger.exception("Failed to fetch card types")
            return []

    def fetch_custom_properties(self) -> List[Dict[str, Any]]:
        """Company custom properties with their select values (for ``select`` types)."""
        try:
            properties = self._client.get("/company/custom-properties") or []
        except Exception:
            logger.exception("Failed to fetch custom properties")
            return []

        for prop in properties:
            if prop.get("type") in ("select", "multi_select"):
                try:
                    prop["select_values"] = self._client.get(
                        f"/company/custom-properties/{prop['id']}/select-values"
                    ) or []
                except Exception:
                    logger.exception(f"Failed to fetch select values for property {prop.get('id')}")
                    prop["select_values"] = []
        return properties

    def resolve_user(self, user_id: Any) -> Optional[Dict[str, Any]]:
        return self._users_by_id.get(user_id)

    def sprints_of_board(self, board_id: str) -> List[Dict[str, Any]]:
        """Sprints of the board. `/sprints` returns sprints of all boards: they are loaded once."""
        if self._all_sprints is None:
            try:
                self._all_sprints = self._client.get_paginated("/sprints") or []
            except Exception:
                logger.exception("Failed to fetch sprints, boards are exported without sprints")
                self._all_sprints = []
            self._sprints_by_id = {sprint["id"]: sprint for sprint in self._all_sprints}
        return [sprint for sprint in self._all_sprints if str(sprint.get("board_id")) == str(board_id)]

    def iterations_of_board(self, board_id: str) -> List[Dict[str, Any]]:
        """Iterations of the board's spaces except removed ones, each with `card_records` — its cards. Iterations
        belong to a space and are loaded once per space."""
        result = {}
        for space_uid in self._space_uids(board_id):
            if space_uid not in self._iterations_by_space:
                self._iterations_by_space[space_uid] = self._load_iterations(space_uid)
            for iteration in self._iterations_by_space[space_uid]:
                result.setdefault(iteration["id"], iteration)
        return list(result.values())

    def spaces(self) -> Optional[List[Dict[str, Any]]]:
        """All spaces, nested ones included; None when they are not loaded."""
        if not self._spaces_loaded:
            self._spaces_loaded = True
            try:
                self._spaces = self._client.get("/spaces") or []
            except Exception:
                logger.exception("Failed to fetch spaces, boards are exported without iterations and access")
        return self._spaces

    def board_spaces(self, board_id: str) -> List[Dict[str, Any]]:
        """Spaces showing the board and their parent spaces: access to a space covers its nested spaces."""
        spaces = self.spaces() or []
        by_uid = {space.get("uid"): space for space in spaces}
        result = {}
        for space in spaces:
            if not any(str(board.get("id")) == str(board_id) for board in space.get("boards") or []):
                continue
            while space is not None and space.get("uid") not in result:
                result[space.get("uid")] = space
                space = by_uid.get(space.get("parent_entity_uid"))
        return list(result.values())

    def space_users(self, space_id: Any) -> Optional[List[Dict[str, Any]]]:
        """Members of a space with their own roles, without members of parent spaces; None when they are not
        loaded."""
        if space_id not in self._space_users:
            try:
                # an empty answer is not an empty list of members
                self._space_users[space_id] = self._client.get(f"/spaces/{space_id}/users")
            except Exception:
                logger.exception(f"Failed to fetch members of space {space_id}")
                self._space_users[space_id] = None
        return self._space_users[space_id]

    def space_role_names(self) -> Dict[str, str]:
        """Names of space roles by id: `reader`, `writer`, `admin` and others."""
        if self._space_role_names is None:
            try:
                roles = self._client.get("/tree-entity-roles") or []
                self._space_role_names = {role.get("id"): role.get("name") for role in roles}
            except Exception:
                logger.exception("Failed to fetch space roles, access to boards is not exported")
                self._space_role_names = {}
        return self._space_role_names

    def _space_uids(self, board_id: str) -> List[str]:
        if self._space_uids_by_board is None:
            spaces = self.spaces() or []
            self._space_uids_by_board = defaultdict(list)
            for space in spaces:
                for board in space.get("boards") or []:
                    self._space_uids_by_board[str(board.get("id"))].append(space.get("uid"))
        return self._space_uids_by_board.get(str(board_id), [])

    def _load_iterations(self, space_uid: str) -> List[Dict[str, Any]]:
        try:
            iterations = self._client.get_paginated(
                f"/spaces/{space_uid}/iterations", params={"status": "planned,active,closed"}
            ) or []
            for iteration in iterations:
                # the cards endpoint returns all cards at once: it ignores limit and offset
                iteration["card_records"] = self._client.get(
                    f"/spaces/{space_uid}/iterations/{iteration['id']}/cards"
                ) or []
        except Exception as exc:
            if _status_code(exc) == 402:
                logger.info(f"Iterations are not available for space {space_uid}")
            else:
                logger.exception(f"Failed to fetch iterations of space {space_uid}, its boards are exported without them")
            return []
        return iterations

    def resolve_sprint(self, sprint_id: Any) -> Optional[Dict[str, Any]]:
        return self._sprints_by_id.get(sprint_id)

    # -- boards ---------------------------------------------------------------

    def fetch_board(self, board_id: str) -> Dict[str, Any]:
        """A board with its columns and lanes. Kaiten embeds both in the board
        response; we fall back to dedicated endpoints if they are absent."""
        try:
            board = self._client.get(f"/boards/{board_id}")
            if board.get("columns") is None:
                board["columns"] = self._client.get(f"/boards/{board_id}/columns") or []
            if board.get("lanes") is None:
                board["lanes"] = self._client.get(f"/boards/{board_id}/lanes") or []
            board["id"] = board_id
            return board
        except Exception:
            logger.exception(f"Failed to fetch board {board_id}")
            raise

    # -- cards ----------------------------------------------------------------

    def list_cards(self, board_id: str) -> List[Dict[str, Any]]:
        """Cards of a board as the list returns them, with `blockers` of blocked cards: enough to
        number cards and resolve their relations before the full cards are fetched."""
        try:
            # the list holds archived cards too; deleted cards are not returned
            cards = self._client.get_paginated("/cards", params={"board_id": board_id}) or []
            blocked = [card for card in cards if card.get("blocked")]
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                blockers = executor.map(lambda card: self._safe_get(f"/cards/{card['id']}/blockers"), blocked)
                for card, card_blockers in zip(blocked, blockers):
                    # without an answer the active blockers embedded in the list are kept
                    card["blockers"] = card_blockers if card_blockers is not None else card.get("blockers") or []
            return cards
        except Exception:
            logger.exception(f"Failed to fetch cards for board {board_id}")
            raise

    def fetch_cards_for_board(self, board_id: str, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Full cards for the cards of `list_cards`."""
        try:
            process_func = functools.partial(self._process_single_card, board_id=board_id)
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                return list(executor.map(process_func, cards))
        except Exception:
            logger.exception(f"Failed to fetch cards for board {board_id}")
            raise

    def _process_single_card(self, card: Dict[str, Any], board_id: str) -> Dict[str, Any]:
        card_id = card["id"]

        # The list endpoint returns a trimmed card (no description/tags/properties);
        # the full card carries those plus embedded ``files`` and ``checklists``
        # (checklists have no standalone GET endpoint, so we request them inline).
        full_card = self._safe_get(f"/cards/{card_id}", params={"additional_card_fields": "checklists"}) or {}
        # relations come from the list: issue numbers and related cards are resolved from it
        card = {**card, **full_card, "blockers": card.get("blockers") or [], "children_ids": card.get("children_ids") or []}

        card["comments"] = self._enrich_comments(self._safe_get(f"/cards/{card_id}/comments") or [])
        card["time_logs"] = self._enrich_time_logs(self._safe_get(f"/cards/{card_id}/time-logs") or [])
        if card.get("files") is None:
            card["files"] = self._safe_get(f"/cards/{card_id}/files") or []
        card["checklists"] = self._enrich_checklists(card.get("checklists") or [])
        card["board_id"] = board_id
        return card

    def _enrich_comments(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for comment in comments:
            if not comment.get("author") and comment.get("author_id") is not None:
                comment["author"] = self.resolve_user(comment["author_id"])
        return comments

    def _enrich_time_logs(self, time_logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for log in time_logs:
            if not log.get("user") and log.get("user_id") is not None:
                log["user"] = self.resolve_user(log["user_id"])
        return time_logs

    def _enrich_checklists(self, checklists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for checklist in checklists:
            for item in checklist.get("items", []):
                # Only ``responsible_id`` is the assignee; ``user_id`` is the item author.
                if not item.get("responsible") and item.get("responsible_id") is not None:
                    item["responsible"] = self.resolve_user(item["responsible_id"])
        return checklists

    def _safe_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self._client.get(endpoint, params=params)
        except Exception:
            logger.exception(f"Failed to fetch {endpoint}")
            return None


def _status_code(exc: BaseException) -> Optional[int]:
    """HTTP status of a failed request: the client wraps `requests.HTTPError` into `Exception`."""
    while exc is not None:
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code
        exc = exc.__cause__
    return None
