import functools
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor

from .trello_client import SimpleTrelloClient


logger = logging.getLogger(__name__)


class TrelloDataFetcher:
    _TRELLO_BATCH_LIMIT = 10
    # a page of actions holds 50 by default, 1000 at most
    _ACTIONS_PAGE_LIMIT = 1000

    def __init__(self, client: SimpleTrelloClient, max_workers: Optional[int] = None):
        self._client = client
        self._max_workers = max_workers or os.cpu_count()
        self._user_cache = {}
        self._token_owner: Optional[Dict[str, Any]] = None
        # requests that failed: their data is missing in the export
        self.failed_requests: List[str] = []

    def fetch_board(self, board_id: str) -> Dict[str, Any]:
        try:
            board_data = self._client.get(f"/boards/{board_id}")

            urls = [
                f"/boards/{board_id}/actions?filter=createBoard&limit=1",
                f"/boards/{board_id}/members?filter=all&fields=fullName",
                f"/boards/{board_id}/lists",
                f"/boards/{board_id}/customFields"
            ]

            create_info, members, lists, custom_fields = self._execute_batch(urls)
            creator = create_info[0].get("memberCreator") if create_info else None
            # an old board may have no creation action: its author is the token owner

            return {
                **board_data,
                "creator": creator or self._get_token_owner(),
                "members": members or [],
                "lists": lists or [],
                "custom_fields": custom_fields or [],
                "id": board_id
            }
        except Exception as e:
            logger.exception(f"Failed to fetch board {board_id}")
            raise e

    def fetch_cards_for_board(self, board_id: str) -> List[Dict[str, Any]]:
        try:
            cards = self._client.get(f"/boards/{board_id}/cards") or []
            board_custom_fields = self._get_board_custom_fields(board_id) or []

            process_func = functools.partial(
                self._process_single_card,
                board_id=board_id,
                board_custom_fields=board_custom_fields
            )

            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                processed_cards = list(executor.map(process_func, cards))

            return processed_cards
        except Exception as e:
            logger.exception(f"Failed to fetch cards for board {board_id}")
            raise e

    def _process_single_card(self, card: Dict[str, Any], board_id: str, board_custom_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        card_id = card["id"]
        urls = [
            # a card is created, copied, converted from a checklist item or sent by email: the action names its author.
            # A copy or conversion is also an action of the source card, so these are requested separately from the
            # creation and in a full page; commas inside a batched url are encoded, the batch separates urls by commas
            f"/cards/{card_id}/actions?filter=createCard&limit=1",
            f"/cards/{card_id}/actions?filter=copyCard%2CconvertToCardFromCheckItem%2CemailCard&limit=1000",
            f"/cards/{card_id}/actions?filter=updateCard&limit=1",
            f"/cards/{card_id}/checklists",
            f"/cards/{card_id}/actions?filter=commentCard&limit={self._ACTIONS_PAGE_LIMIT}",
            f"/cards/{card_id}/list",
            f"/cards/{card_id}/customFieldItems",
            f"/cards/{card_id}/attachments"
        ]

        create_info, copy_info, update_info, checklists, comments, card_list, custom_fields, attachments = self._execute_batch(urls)
        comments = self._fetch_older_comments(card_id, comments or [])

        # assignees of checklist items and authors of attachments come as member ids
        member_ids = {
            item["idMember"] for checklist in checklists or [] for item in checklist.get("checkItems", []) if item.get("idMember")
        }
        member_ids |= {
            attachment["idMember"] for attachment in attachments or []
            if attachment.get("idMember") and not attachment.get("memberCreator")
        }
        members_map = self._fetch_members_batch(list(member_ids)) if member_ids else {}
        for checklist in checklists or []:
            for item in checklist.get("checkItems", []):
                item["member"] = members_map.get(item.get("idMember"))
        for attachment in attachments or []:
            if not attachment.get("memberCreator"):
                attachment["memberCreator"] = members_map.get(attachment.get("idMember"))
            attachment["date"] = attachment.get("date") or creation_time_from_id(attachment.get("id"))

        # the creation is the action of this card; a card without such an action has its date in its id, its author
        # is the token owner
        card_creation = next(
            (
                action for action in (create_info or []) + (copy_info or [])
                if ((action.get("data") or {}).get("card") or {}).get("id") == card_id
            ),
            {},
        )
        card_creation = {
            **card_creation,
            "date": card_creation.get("date") or creation_time_from_id(card_id),
            "memberCreator": card_creation.get("memberCreator") or self._get_token_owner(),
        }

        card.update({
            "create_info": card_creation,
            "update_info": update_info[0] if update_info else {},
            "checklists": checklists or [],
            "comments": comments or [],
            "list_info": card_list or {},
            "custom_field_items": custom_fields or [],
            "attachments": attachments or [],
            "board_custom_fields": board_custom_fields or []
        })

        return card

    def _fetch_older_comments(self, card_id: str, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """All comments of a card: the first page and the older ones; actions come newest first."""
        page = comments
        while len(page) >= self._ACTIONS_PAGE_LIMIT:
            endpoint = f"/cards/{card_id}/actions"
            before = page[-1]["id"]
            params = {"filter": "commentCard", "limit": self._ACTIONS_PAGE_LIMIT, "before": before}
            try:
                page = self._client.get(endpoint, params=params) or []
            except Exception as exc:
                logger.exception(f"Failed to fetch comments of card {card_id}")
                self.failed_requests.append(f"{endpoint}?before={before}: {exc}")
                break
            if page and page[-1]["id"] == before:
                self.failed_requests.append(f"{endpoint}?before={before}: the page did not move")
                break
            comments = comments + page
        return comments

    def _fetch_members_batch(self, member_ids: List[str]) -> Dict[str, Dict]:
        members_map = {}

        cached_ids = []
        for member_id in member_ids:
            cached = self._user_cache.get(member_id)
            if cached is not None:
                members_map[member_id] = cached
            else:
                cached_ids.append(member_id)

        if cached_ids:
            urls = [f"/members/{member_id}" for member_id in cached_ids]
            results = self._execute_batch(urls)

            for member_id, result in zip(cached_ids, results):
                if result:
                    self._user_cache[member_id] = result
                    members_map[member_id] = result
                else:
                    logger.warning(f"Failed to fetch member {member_id}")

        return members_map

    def _get_token_owner(self) -> Dict[str, Any]:
        # a failure is raised: without the owner the board and its cards lose authors required by the import
        if self._token_owner is None:
            owner = self._client.get("/members/me", params={"fields": "fullName"})
            if not owner:
                raise ValueError("The token owner is not returned by /members/me")
            self._token_owner = owner
        return self._token_owner

    def _execute_batch(self, urls: List[str]) -> List[Any]:
        results = []
        for idx in range(0, len(urls), self._TRELLO_BATCH_LIMIT):
            batch_urls = urls[idx:idx+self._TRELLO_BATCH_LIMIT]
            batch_param = ','.join(url for url in batch_urls)

            batch_response = self._client.get("/batch", params={"urls": batch_param})
            for url, response in zip(batch_urls, batch_response):
                if "200" in response:
                    response_data = response["200"]
                    if not response_data:
                        logger.warning(
                            f"Empty response received with status 200 for URL: {url}"
                        )
                    results.append(response_data)
                else:
                    # status: the statusCode field, otherwise a numeric key such as "404"
                    error_status = str(response.get("statusCode") or next(
                        (key for key in response if key.isdecimal()), "UNKNOWN"
                    ))
                    logger.warning(
                        f"Request failed for URL: {url}. "
                        f"Server responded with: {error_status}"
                    )
                    # a missing member stays missing on a rerun
                    if error_status != "404" or not url.startswith("/members/"):
                        self.failed_requests.append(f"{url}: {error_status}")
                    results.append(None)

        return results

    def _get_board_custom_fields(self, board_id: str) -> Optional[List[Dict]]:
        try:
            return self._client.get(f"/boards/{board_id}/customFields")
        except Exception as exc:
            logger.exception(f"Failed to get custom fields for board {board_id}")
            self.failed_requests.append(f"/boards/{board_id}/customFields: {exc}")
            return None


def creation_time_from_id(object_id: Optional[str]) -> Optional[str]:
    """Creation time of a Trello object: the first 8 hex digits of its full 24-digit id are a Unix timestamp."""
    if not isinstance(object_id, str) or not re.fullmatch(r"[0-9a-f]{24}", object_id):
        return None
    moment = datetime.fromtimestamp(int(object_id[:8], 16), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
