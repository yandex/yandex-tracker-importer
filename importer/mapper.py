from typing import Optional, Dict
from .config import get_generic_keys_mapping


class EntityMapper:

    def __init__(self, project_path: str) -> None:
        self._queue_map = get_generic_keys_mapping(project_path, "queues")
        self._user_map = get_generic_keys_mapping(project_path, "users")
        self._statuses_map = get_generic_keys_mapping(project_path, "statuses")
        self._resolutions_map = get_generic_keys_mapping(project_path, "resolutions")
        self._issue_types_map = get_generic_keys_mapping(project_path, "issue_types")
        self._priorities_map = get_generic_keys_mapping(project_path, "priorities", validate_file_exist=False)
        self._fields_map = get_generic_keys_mapping(project_path, "fields", validate_file_exist=False)
        self._local_fields_map = get_generic_keys_mapping(project_path, "local_fields", validate_file_exist=False)
        self._groups_map = get_generic_keys_mapping(project_path, "groups", validate_file_exist=False)

    def _get_mapping_value(self, mapping: Dict[str, str], key: Optional[str]) -> Optional[str]:
        if key is None:
            return None
        return mapping.get(key, key)

    def get_queue(self, queue_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._queue_map, queue_key)

    def get_user(self, user_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._user_map, user_key)

    def get_status(self, status_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._statuses_map, status_key)

    def get_resolution(self, resolution_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._resolutions_map, resolution_key)

    def get_issue_type(self, issue_type_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._issue_types_map, issue_type_key)

    def get_priority(self, priority_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._priorities_map, priority_key)

    def get_field(self, field_key: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._fields_map, field_key)

    def get_issue_key(self, issue_key: Optional[str]) -> Optional[str]:
        if issue_key is None:
            return None
        queue_key, issue_num = issue_key.split("-")
        return "{}-{}".format(self.get_queue(queue_key), issue_num)

    def get_group(self, group_name: Optional[str]) -> Optional[str]:
        return self._get_mapping_value(self._groups_map, group_name)
