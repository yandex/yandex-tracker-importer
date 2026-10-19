"""Коллекции Трекера, которых нет в клиенте: дашборды и виджеты.

Клиент регистрирует любой класс-наследник `Collection` по его пути, поэтому объявленные здесь
коллекции работают как встроенные: ответы разбираются в ресурсы с объявленными полями, а запросы
идут через то же соединение — с повторами, бюджетом ошибок и общей авторизацией.
"""

from typing import Any, Dict, List

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.collections import Collection


class Dashboards(Collection):
    path = "/{api_version}/dashboards/{id}"
    fields = {
        "id": None,
        "self": None,
        "version": None,
        "name": None,
        "layout": None,
        "widgets": [],
        "owner": None,
        "createdBy": None,
        "createdAt": None,
    }

    def find(self, name: str = None, owner: str = None, per_page: int = None, page: int = None):
        """Поиск дашбордов доступен только методом POST с телом запроса и принимает название и
        владельца: своего ключа у дашборда нет, а названия не уникальны."""
        return self._execute_request(
            self._connection.post,
            path="/{api_version}/dashboards/_search",
            params={"perPage": per_page, "page": page},
            data={"name": name, "owner": owner},
        )

    def set_widgets_state(self, dashboard_id: Any, version: Any, widgets: List[Dict[str, Any]]):
        """Раскладывает виджеты по дашборду: при создании виджет положения не принимает, его
        задаёт отдельное обновление дашборда — и только вместе с его версией.

        Список заменяет набор виджетов целиком: виджет, которого в нём нет, удаляется."""
        return self._execute_request(
            self._connection.patch,
            path=f"/{{api_version}}/dashboards/{dashboard_id}",
            params={"version": version},
            data={"widgets": widgets},
        )


class Widgets(Collection):
    # виджет создаётся в дашборде, а удаляется обновлением набора виджетов (см. set_widgets_state)
    path = "/{api_version}/widgets/{id}"
    fields = {
        "id": None,
        "self": None,
        "version": None,
        "type": None,
        "dashboard": None,
        "description": None,
        "refresh": None,
        "color": None,
        "column": None,
        "row": None,
        "collapsed": None,
        "coords": None,
        "order": None,
        "createdBy": None,
        "createdAt": None,
    }

    def create_in_dashboard(self, dashboard_id: Any, **kwargs):
        return self._execute_request(
            self._connection.post,
            path=f"/{{api_version}}/dashboards/{dashboard_id}/widgets",
            data=kwargs,
        )


def get_dashboards(client: TrackerClient) -> Dashboards:
    return client._get_collection(Dashboards)


def get_widgets(client: TrackerClient) -> Widgets:
    return client._get_collection(Widgets)
