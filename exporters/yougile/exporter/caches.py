import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from .api_client import YouGileApi
from .exceptions import APIRequestFailedException, MaxRetriesExceededException
from .tools import PRIORITIES, title_to_key


log = logging.getLogger(__name__)


class StickerCache:
    PRIORITY_ICON = "prio"

    def __init__(self) -> None:
        self._sticker_cache = dict()
        self._state_cache = dict()

    def add_stickers(self, stickers: List[Dict[str, Any]]) -> None:
        for sticker in stickers:
            self._sticker_cache[sticker["id"]] = {
                "key": title_to_key(sticker["name"]),
                "name": sticker["name"],
                "icon": sticker.get("icon"),
                "states": [self._state_label(state) for state in sticker.get("states", [])],
            }

            for state in sticker.get("states", []):
                self._state_cache[state["id"]] = self._state_label(state)

    @staticmethod
    def _state_label(state: Dict[str, Any]) -> str:
        """A sprint state shows its dates, as in YouGile: «Sprint 1 (24.06–08.07)»."""
        if state.get("begin") and state.get("end"):
            begin, end = (datetime.fromtimestamp(state[edge] / 1000, tz=timezone.utc) for edge in ("begin", "end"))
            return f"{state['name']} ({begin:%d.%m}–{end:%d.%m})"
        return state["name"]

    def get_sticker(self, sticker_id: str) -> Optional[Dict]:
        return self._sticker_cache.get(sticker_id)

    def is_priority(self, sticker_id: str) -> bool:
        """The standard priority sticker: the priority icon and priority states only."""
        sticker = self.get_sticker(sticker_id) or {}
        return sticker.get("icon") == self.PRIORITY_ICON and set(sticker.get("states", [])) <= set(PRIORITIES)

    def get_state_value(self, state: Union[str, List[str]]) -> str:
        """A sticker of a task has one state; of a list of states the first one is taken."""
        if isinstance(state, list):
            log.warning(f"Sticker value {state} has several states: the first one is exported")
            state = state[0] if state else ""
        return self._state_cache.get(state, state)


class UserCache:

    def __init__(self, client: YouGileApi):
        self._client = client
        self._user_cache = dict()
        self._unavailable = set()

    UNKNOWN_USER = "yougile-user-{}"

    def get_user_email(self, user_id: Optional[str]) -> Optional[str]:
        if not user_id:
            return None
        if user_id in self._unavailable:
            return None
        if user_id not in self._user_cache:
            try:
                user = self._client.get_user(user_id)
            except (APIRequestFailedException, MaxRetriesExceededException) as e:
                # only a not found user is cached as missing; a failed one is not requested again in this run
                log.error(f"User {user_id} is not available: {e}")
                self._unavailable.add(user_id)
                return None
            else:
                if not user:
                    log.warning(f"User {user_id} not found")
            self._user_cache[user_id] = user

        return self._user_cache[user_id].get("email")

    def get_user_login(self, user_id: Optional[str]) -> Optional[str]:
        """Return the user e-mail, or ``yougile-user-<id>`` for a user without e-mail or not found, to map at import;
        ``None`` if the user is not available."""
        if not user_id:
            return None
        email = self.get_user_email(user_id)
        if email or user_id not in self._user_cache:
            return email
        return self.UNKNOWN_USER.format(user_id)

    @property
    def unavailable_users(self) -> List[str]:
        """IDs of users whose requests failed: their objects are dumped without them."""
        return sorted(self._unavailable)

    def add_users_to_cache(self, users: List[Dict[str, str]]) -> None:
        for user in users:
            self._user_cache[user["id"]] = user

    def get_all_users(self) -> List[Dict[str, str]]:
        return [user for user in self._user_cache.values() if user]
