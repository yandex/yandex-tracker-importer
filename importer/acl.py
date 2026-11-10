import logging
from typing import Any, Callable, Dict, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .groups import GroupResolver
from .mapper import EntityMapper
from .utils import clean_empty, get_optional


logger = logging.getLogger(__name__)


class AclUpdater:
    """Приводит права объекта с видами `READ`, `WRITE`, `GRANT` (шаблон, дашборд) к выгрузке:
    недостающие выдаются, лишние снимаются.

    Снятие и выдача — отдельными запросами: отказ по одному субъекту отклоняет весь запрос."""

    def __init__(self, client: TrackerClient, mapper: EntityMapper) -> None:
        self._client = client
        self._mapper = mapper
        self._group_resolver = GroupResolver(client, mapper)
        self._myself = None
        self._user_uids = {}

    def update(
        self, name: str, permissions: Dict[str, Any], load_current: Callable[[], Dict[str, Any]],
        apply: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """`load_current` отдаёт текущие права объекта, `apply` отправляет `grant` или `revoke`."""
        try:
            update = self._get_permissions_update(permissions, load_current() or {}, name)
        except Exception:
            logger.exception(f"Error updating permissions of {name}")
            return

        for operation in ("revoke", "grant"):
            if not update.get(operation):
                continue
            try:
                apply({operation: update[operation]})
            except Exception:
                logger.exception(f"Error updating permissions of {name} with data: {{{operation!r}: {update[operation]}}}")

    def _get_permissions_update(self, permissions: Dict[str, Any], current: Dict[str, Any], name: str) -> Dict[str, Any]:
        """Недостающие права выдаются, лишние снимаются. Пользователи и группы сравниваются по
        номерам; если кого-то из них не удалось найти, этот вид прав не снимается."""
        myself = self._get_myself()
        myself_uid = str(myself.uid)
        grant, revoke = {}, {}
        for permission in set(permissions) | set(current):
            subjects = permissions.get(permission) or {}
            current_subjects = current.get(permission) or {}
            resolvers = {
                "users": lambda user: self._get_user_uid(user, name),
                "groups": lambda group: self._get_group_id(group, name),
                "roles": lambda role: role,
            }
            wanted, complete = {}, {}
            for kind, resolve in resolvers.items():
                resolved = [resolve(item) for item in subjects.get(kind) or []]
                wanted[kind] = set(resolved) - {None}
                complete[kind] = None not in resolved
            existing = {kind: {str(get_optional(item, "id")) for item in get_optional(current_subjects, kind) or []} for kind in resolvers}
            grant[permission] = {kind: sorted(wanted[kind] - existing[kind]) for kind in resolvers}
            revoke[permission] = {
                kind: sorted(existing[kind] - wanted[kind]) for kind in resolvers if complete[kind]
            }
            if myself_uid in revoke[permission].get("users", []) and not get_optional(myself, "admin"):
                # без своих прав учётная запись не администратора не прочитает объект на следующем прогоне
                revoke[permission]["users"].remove(myself_uid)
                logger.warning(f"{permission} permission of the import account is kept in {name}: it is not an administrator")
        return clean_empty({"grant": grant, "revoke": revoke})

    def _get_user_uid(self, user: str, name: str) -> Optional[str]:
        target = self._mapper.get_user(user)
        if target not in self._user_uids:
            try:
                self._user_uids[target] = str(self._client.users.get(target).uid)
            except Exception:
                logger.warning(f"User {user} is not found, it is skipped in permissions of {name}: check mapping/users.cfg")
                self._user_uids[target] = None
        return self._user_uids[target]

    def _get_group_id(self, group: str, name: str) -> Optional[str]:
        group_id = self._group_resolver.get_group_id(group)
        if group_id is None:
            # пропущенная группа сужает доступ: предупреждение в журнал
            logger.warning(f"Group {group} is not found, it is skipped in permissions of {name}: check mapping/groups.cfg")
            return None
        return str(group_id)

    def _get_myself(self) -> Resource:
        if self._myself is None:
            self._myself = self._client.myself
        return self._myself
