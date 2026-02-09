import functools
import logging
import os
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor

from .trello_client import SimpleTrelloClient


logger = logging.getLogger(__name__)


class TrelloDataFetcher:
    _TRELLO_BATCH_LIMIT = 10

    def __init__(self, client: SimpleTrelloClient, max_workers: Optional[int] = None):
        self._client = client
        self._max_workers = max_workers or os.cpu_count()
        self._user_cache = {}

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

            return {
                **board_data,
                "creator": creator or {},
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
            f"/cards/{card_id}/actions?filter=createCard&limit=1",
            f"/cards/{card_id}/actions?filter=updateCard&limit=1",
            f"/cards/{card_id}/checklists",
            f"/cards/{card_id}/actions?filter=commentCard",
            f"/cards/{card_id}/list",
            f"/cards/{card_id}/customFieldItems",
            f"/cards/{card_id}/attachments"
        ]

        create_info, update_info, checklists, comments, card_list, custom_fields, attachments = self._execute_batch(urls)

        if checklists:
            member_ids = set()
            for checklist in checklists:
                for item in checklist.get("checkItems", []):
                    if item.get("idMember"):
                        member_ids.add(item["idMember"])

            members_map = {}
            if member_ids:
                members_map = self._fetch_members_batch(list(member_ids))

            for checklist in checklists:
                for item in checklist.get("checkItems", []):
                    item["member"] = members_map.get(item.get("idMember"))

        card.update({
            "create_info": create_info[0] if create_info else {},
            "update_info": update_info[0] if update_info else {},
            "checklists": checklists or [],
            "comments": comments or [],
            "list_info": card_list or {},
            "custom_field_items": custom_fields or [],
            "attachments": attachments or [],
            "board_custom_fields": board_custom_fields or [],
            "board_id": board_id
        })

        return card

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

    def _execute_batch(self, urls: List[str]) -> List[Any]:
        results = []
        for idx in range(0, len(urls), self._TRELLO_BATCH_LIMIT):
            batch_urls = urls[idx:idx+self._TRELLO_BATCH_LIMIT]
            batch_param = ','.join(url for url in batch_urls)

            batch_response = self._client.get("/batch", params={"urls": batch_param})
            for url, response in zip(urls, batch_response):
                if "200" in response:
                    response_data = response["200"]
                    if not response_data:
                        logger.warning(
                            f"Empty response received with status 200 for URL: {url}"
                        )
                    results.append(response_data)
                else:
                    error_status = response.get("statusCode", "UNKNOWN")
                    logger.warning(
                        f"Request failed for URL: {url}. "
                        f"Server responded with: {error_status}"
                    )
                    results.append(None)

        return results

    def _get_board_custom_fields(self, board_id: str) -> Optional[List[Dict]]:
        try:
            return self._client.get(f"/boards/{board_id}/customFields")
        except Exception:
            logger.exception(f"Failed to get custom fields for board {board_id}")
            return None
