import logging
from typing import List, Dict, Any

from yandex_tracker_client.objects import Resource

from .mapper import EntityMapper


logger = logging.getLogger(__name__)


class LinksCollector:
    def __init__(self, key_field: str, key_extractor: callable = None):
        self._links = {}
        self._processed_keys = set()
        self._key_field = key_field
        self._key_extractor = key_extractor or (lambda x: x)

    def add_links(self, source: Any, links: List[Dict[str, Any]]) -> None:
        unique_links = []
        for link_data in links:
            if self._key_field not in link_data:
                logger.warning(
                    f"Link for {source} is missing required '{self._key_field}' field: {link_data}"
                )
                continue

            target_key = self._key_extractor(link_data[self._key_field])
            if target_key not in self._processed_keys:
                unique_links.append(link_data)

        if unique_links:
            source_key = source.key if hasattr(source, 'key') else source
            self._processed_keys.add(source_key)
            self._links[source] = unique_links

    @property
    def collected_links(self) -> Dict[Any, List[Dict[str, Any]]]:
        return self._links


class IssueLinksCollector:
    def __init__(self, mapper: EntityMapper):
        self._collector = LinksCollector(
            key_field="issue",
            key_extractor=lambda issue: mapper.get_issue_key(issue)
        )

    def add_issue_links(self, issue_obj: Resource, links: List[Dict[str, Any]]) -> None:
        self._collector.add_links(issue_obj, links)

    @property
    def collected_links(self) -> Dict[Resource, List[Dict[str, Any]]]:
        return self._collector.collected_links


class EntityLinksCollector:
    def __init__(self):
        self._collector = LinksCollector(key_field="entity", key_extractor=str)

    def add_entity_links(self, source_id: str, links: List[Dict[str, Any]]) -> None:
        self._collector.add_links(source_id, links)

    @property
    def collected_links(self) -> Dict[str, List[Dict[str, Any]]]:
        return self._collector.collected_links
