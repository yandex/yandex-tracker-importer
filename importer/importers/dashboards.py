import logging
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .base import GetOrCreateImporter
from .fields import GlobalFieldImporter
from .filters import FilterImporter
from ..constants import DEFAULT_TRACKER_UI_URL
from ..exceptions import (
    DashboardNotUniqueError,
    FilterNotImportedError,
    LocalFieldNotSupportedError,
)
from ..mapper import EntityMapper
from ..tracker_collections import get_dashboards, get_widgets
from ..utils import get_optional, prepare_field_value


logger = logging.getLogger(__name__)

# Цвет виджета API принимает числом, а в выгрузке он записан названием
WIDGET_COLORS = {
    "none": 0,
    "darkGreen": 1,
    "red": 2,
    "green": 3,
    "blue": 4,
    "yellow": 5,
}

# Положение виджета задаётся не при создании, а отдельным обновлением дашборда
WIDGET_STATE_FIELDS = ("column", "row", "collapsed", "coords", "order")

# Данные, которые проставляет сам Трекер
WIDGET_SERVICE_FIELDS = ("id", "createdAt", "createdBy", "self", "version", "dashboard")


class DashboardImporter(GetOrCreateImporter):
    """Переносит дашборды вместе с виджетами.

    Создание идёт в три шага: дашборд, затем виджеты по одному — при создании виджет положения не
    принимает, — и в конце одно обновление дашборда, которое раскладывает виджеты по местам."""

    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        global_field_importer: GlobalFieldImporter,
        filter_importer: FilterImporter,
        ui_url: str = DEFAULT_TRACKER_UI_URL,
    ):
        super().__init__(client, mapper)
        self._ui_url = ui_url
        self._widget_urls = {}
        self._global_field_importer = global_field_importer
        self._filter_importer = filter_importer
        self._dashboards = get_dashboards(client)
        self._widgets = get_widgets(client)

    @property
    def object_name(self) -> str:
        return "Dashboard"

    @property
    def widget_urls(self) -> Dict[str, str]:
        """Адреса созданных виджетов по их идентификаторам в исходной организации: по ним
        собираются ссылки метрик целей, проектов и портфелей."""
        return self._widget_urls

    def get_or_create(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        try:
            dashboard_obj = self._find_dashboard(input_data)
        except Exception:
            # не зная, создан ли дашборд раньше, создавать его нельзя: получится дубль
            logger.exception(f"Error searching dashboard {input_data.get('name')}, it is skipped")
            return None

        if dashboard_obj is None:
            dashboard_obj = self._try_creating_object(input_data)

        if dashboard_obj is not None:
            self._import_widgets(input_data.get("widgets") or [], dashboard_obj)

        return dashboard_obj

    def _find_dashboard(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        """Своего ключа у дашборда нет и теги он не поддерживает, поэтому ранее созданный ищется
        по названию и владельцу. Если нашлось несколько, дашборд пропускается: создавать копию
        вслепую хуже, чем оставить перенос этого дашборда человеку."""
        name = input_data.get("name")
        owner = self._mapper.get_user(input_data.get("owner"))

        # владелец в поиск не передаётся: если он не сопоставлен, Трекер отвечает ошибкой на весь
        # запрос, поэтому найденное сверяется с владельцем уже на своей стороне
        found = [
            dashboard
            for dashboard in self._dashboards.find(name=name)
            if get_optional(dashboard, "name") == name and self._has_owner(dashboard, owner)
        ]

        if len(found) > 1:
            raise DashboardNotUniqueError(name, len(found))

        if not found:
            return None

        # поиск отдаёт списочное представление без виджетов, а они нужны, чтобы понять, какие из
        # них уже созданы; заодно берётся свежая версия дашборда для последующего обновления
        return self._dashboards.get(found[0]["id"])

    @staticmethod
    def _has_owner(dashboard: Any, owner: Optional[str]) -> bool:
        if owner is None:
            return True

        dashboard_owner = get_optional(dashboard, "owner") or {}

        return owner in (get_optional(dashboard_owner, "id"), get_optional(dashboard_owner, "display"))

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        return self._find_dashboard(input_data)

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        return self._dashboards.create(**prepared_data)

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": input_data.get("name"),
            "layout": input_data.get("layout"),
            "owner": self._mapper.get_user(input_data.get("owner")),
        }

    def _import_widgets(self, widgets_data: List[Dict[str, Any]], dashboard_obj: Resource) -> None:
        existing = self._load_existing_widgets(dashboard_obj)

        states = []
        for widget_data in widgets_data:
            widget_obj = existing.get(self._widget_key(widget_data))
            if widget_obj is None:
                widget_obj = self._create_widget(widget_data, dashboard_obj)

            if widget_obj is not None:
                states.append(self._prepare_widget_state(widget_data, widget_obj))
                self._remember_widget_url(widget_data, widget_obj, dashboard_obj)

        if states:
            self._set_widgets_state(states, dashboard_obj)

    def _remember_widget_url(
        self, widget_data: Dict[str, Any], widget_obj: Resource, dashboard_obj: Resource
    ) -> None:
        source_id = widget_data.get("id")
        if source_id is None:
            return

        self._widget_urls[str(source_id)] = (
            f"{self._ui_url}/dashboard/{dashboard_obj.id}/widget/{widget_obj.id}"
            "?_embedded=1&_no_controls=1"
        )

    def _load_existing_widgets(self, dashboard_obj: Resource) -> Dict[tuple, Resource]:
        """Виджеты дашборда приходят ссылками, поэтому тело каждого запрашивается отдельно.
        Повторный прогон по ним и узнаёт, какие виджеты уже созданы."""
        existing = {}
        for widget_ref in dashboard_obj.widgets or []:
            widget_id = get_optional(widget_ref, "id")
            try:
                widget_obj = self._widgets.get(widget_id)
            except Exception:
                logger.exception(f"Error loading widget {widget_id}")
                continue

            existing[self._widget_key(widget_obj.as_dict())] = widget_obj

        return existing

    @staticmethod
    def _widget_key(widget_data: Dict[str, Any]) -> tuple:
        """Своего ключа у виджета нет: одинаковыми считаются виджеты одного вида с одним
        заголовком на одном месте дашборда. Одного заголовка мало — у заметок он пустой."""
        coords = widget_data.get("coords") or {}

        return (
            widget_data.get("type"),
            widget_data.get("description"),
            coords.get("x"),
            coords.get("y"),
        )

    def _create_widget(self, widget_data: Dict[str, Any], dashboard_obj: Resource) -> Optional[Resource]:
        try:
            prepared_data = self._prepare_widget_data(widget_data)
        except Exception:
            logger.exception(
                f"Error preparing widget for dashboard {dashboard_obj.name} with data: {widget_data}"
            )
            return None

        try:
            return self._widgets.create_in_dashboard(dashboard_obj.id, **prepared_data)
        except Exception:
            logger.exception(
                f"Error creating widget for dashboard {dashboard_obj.name} with data: {prepared_data}"
            )
            return None

    def _prepare_widget_data(self, widget_data: Dict[str, Any]) -> Dict[str, Any]:
        prepared_data = {
            key: value
            for key, value in widget_data.items()
            if key not in WIDGET_SERVICE_FIELDS and key not in WIDGET_STATE_FIELDS
        }

        if "color" in prepared_data:
            prepared_data["color"] = WIDGET_COLORS.get(prepared_data["color"], prepared_data["color"])

        # groupBy у виджета — это единица группировки (например `day`), а не поле задачи:
        # полем он бывает только внутри сохранённого фильтра
        for key in ("fields", "field"):
            if key in prepared_data:
                prepared_data[key] = self._prepare_fields(prepared_data[key])

        if "filterParams" in prepared_data:
            prepared_data["filterParams"] = self._prepare_filter_params(prepared_data["filterParams"])

        return prepared_data

    def _prepare_fields(self, fields: Any) -> Any:
        if isinstance(fields, list):
            return [self._get_field(key).id for key in fields]

        return self._get_field(fields).id if fields else fields

    def _prepare_filter_params(self, filter_params: Dict[str, Any]) -> Dict[str, Any]:
        prepared_data = {}

        if filter_params.get("query"):
            # запрос переносится как есть: имена полей и значения в нём не сопоставляются
            logger.warning(
                "Widget query is imported as is, check it in Tracker after import: "
                f"{filter_params['query']}"
            )
            prepared_data["query"] = filter_params["query"]

        if filter_params.get("filter"):
            prepared_data["filter"] = self._prepare_filter(filter_params["filter"])

        if filter_params.get("namedFilter"):
            prepared_data["filterId"] = self._get_filter_id(filter_params["namedFilter"])

        if filter_params.get("sorts"):
            # у виджета признак направления называется иначе, чем у фильтра
            prepared_data["sorts"] = [
                {
                    "orderBy": self._get_field(sort["field"]).id,
                    "orderAsc": sort.get("ascending", True),
                }
                for sort in filter_params["sorts"]
            ]

        return prepared_data

    def _prepare_filter(self, filter_data: Dict[str, Any]) -> Dict[str, Any]:
        prepared_data = {}
        for field_key, value in filter_data.items():
            field_obj = self._get_field(field_key)
            prepared_data[field_obj.id] = prepare_field_value(field_obj, value, self._mapper)

        return prepared_data

    def _get_filter_id(self, source_filter_id: Any) -> Any:
        filter_obj = self._filter_importer.get_from_cache(str(source_filter_id))
        if filter_obj is None:
            raise FilterNotImportedError(source_filter_id)

        return filter_obj.id

    def _get_field(self, field_key: str) -> Resource:
        if field_key.startswith("local_"):
            # локальное поле принадлежит очереди, а фильтр и виджет к очереди не привязаны
            raise LocalFieldNotSupportedError(field_key)

        return self._global_field_importer.get_or_create(field_key)

    def _prepare_widget_state(self, widget_data: Dict[str, Any], widget_obj: Resource) -> Dict[str, Any]:
        state = {key: widget_data[key] for key in WIDGET_STATE_FIELDS if key in widget_data}
        state["id"] = widget_obj.id

        return state

    def _set_widgets_state(self, states: List[Dict[str, Any]], dashboard_obj: Resource) -> None:
        try:
            self._dashboards.set_widgets_state(dashboard_obj.id, dashboard_obj.version, states)
        except Exception:
            logger.exception(
                f"Error placing widgets on dashboard {dashboard_obj.name} with data: {states}"
            )
