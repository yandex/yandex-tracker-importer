from typing import Any, Dict, Optional

from yandex_tracker_client import TrackerClient

from .mapper import EntityMapper


class GroupResolver:
    """Переводит группу из выгрузки в номер группы целевой организации.

    Выгрузка хранит группу названием: номера групп в организациях разные. Название проходит через
    `mapping/groups.cfg`, как пользователь через `users.cfg`. Справа там можно указать название
    группы целевой организации или её номер — номер однозначен, даже если группы называются
    одинаково или на разных языках по-разному.

    Список групп запрашивается один раз на весь прогон: группы нужны правам очередей, правам
    сущностей и условиям триггеров, а импортёры создаются на каждую очередь и вид сущности.
    Загружать его стоит до создания объектов (`load_groups`): сбой на полпути оставил бы
    созданный объект без прав, а повторный прогон его уже не дозаполнит."""

    _ids_by_name: Optional[Dict[str, Any]] = None
    _ids: Optional[Dict[str, Any]] = None

    def __init__(self, client: TrackerClient, mapper: EntityMapper) -> None:
        self._client = client
        self._mapper = mapper

    def get_group_id(self, group: Any) -> Optional[Any]:
        """Номер группы в целевой организации или None, если такой группы там нет. Группа,
        записанная ссылкой с идентификатором и названием, ищется по названию."""
        name = group.get("display") if isinstance(group, dict) else group
        target = self._mapper.get_group(name)
        if target is None:
            return None

        self.load_groups()
        # номер справа в groups.cfg — это номер, даже если какая-то группа так и называется. Без
        # сопоставления номером ничего не считается: группа исходной организации, названная
        # цифрами, не должна совпасть с чужой группой по номеру
        if target != name and str(target) in GroupResolver._ids:
            return GroupResolver._ids[str(target)]
        return GroupResolver._ids_by_name.get(target)

    def load_groups(self) -> None:
        if GroupResolver._ids_by_name is None:
            groups = list(self._client.groups)
            GroupResolver._ids = {str(group.id): group.id for group in groups}
            GroupResolver._ids_by_name = {group.name: group.id for group in groups}
