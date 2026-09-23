import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

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
        self._card_types_by_id: Dict[Any, Dict[str, Any]] = {}
        self._sprints_by_id: Dict[Any, Dict[str, Any]] = {}
        self._current_user: Optional[Dict[str, Any]] = None

    # -- shared reference data ------------------------------------------------

    def preload_reference_data(self) -> None:
        self._users_by_id = {user["id"]: user for user in self.fetch_users()}
        self._card_types_by_id = {ct["id"]: ct for ct in self.fetch_card_types()}
        self._current_user = self.fetch_current_user()

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

    def resolve_card_type(self, type_id: Any) -> Optional[Dict[str, Any]]:
        return self._card_types_by_id.get(type_id)

    def preload_sprints(self, board_id: str) -> None:
        try:
            sprints = self._client.get_paginated("/sprints", params={"board_id": board_id}) or []
        except Exception:
            logger.exception(f"Failed to fetch sprints for board {board_id}")
            return
        for sprint in sprints:
            self._sprints_by_id[sprint["id"]] = sprint

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

    def fetch_cards_for_board(self, board_id: str) -> List[Dict[str, Any]]:
        try:
            cards = self._client.get_paginated("/cards", params={"board_id": board_id}) or []

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
        card = {**card, **full_card}

        card["comments"] = self._enrich_comments(self._safe_get(f"/cards/{card_id}/comments") or [])
        card["time_logs"] = self._enrich_time_logs(self._safe_get(f"/cards/{card_id}/time-logs") or [])
        if card.get("files") is None:
            card["files"] = self._safe_get(f"/cards/{card_id}/files") or []
        card["checklists"] = self._enrich_checklists(card.get("checklists") or [])
        if card.get("blocked"):
            card["blockers"] = self._safe_get(f"/cards/{card_id}/blockers") or []
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
