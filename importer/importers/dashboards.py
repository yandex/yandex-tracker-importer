import logging
import unicodedata
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import NotFound, TrackerServerError
from yandex_tracker_client.objects import Resource

from .base import GetOrCreateImporter
from .fields import GlobalFieldImporter
from .filters import FilterImporter
from ..acl import AclUpdater
from ..constants import DEFAULT_TRACKER_UI_URL
from ..exceptions import (
    BoardNotInDumpError,
    DashboardNotUniqueError,
    FilterNotImportedError,
    LocalFieldNotSupportedError,
)
from ..mapper import EntityMapper
from ..state import ImportState
from ..tracker_collections import get_dashboards, get_widgets, update_template_permissions
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

# Данные, которые проставляет сам Трекер; `updatedAt`, `updatedBy`, `datasetInfo` — из выгрузок прежних версий
WIDGET_SERVICE_FIELDS = (
    "id", "createdAt", "createdBy", "self", "version", "dashboard", "updatedAt", "updatedBy", "datasetInfo",
)

# Виды виджетов, которые API создаёт с автором и датой создания (`_import`)
IMPORTABLE_WIDGET_TYPES = (
    "info", "filter", "created-vs-resolved", "issues", "table", "table-sum", "time-since",
)

# Поля виджетов этих видов — поля целей, проектов и портфелей, а не задач
ENTITY_WIDGET_TYPES = ("goalFilter", "projectPortfolioFilter")

# Условия виджетов целей и портфелей со значениями-пользователями
ENTITY_USER_FILTER_KEYS = ("lead", "author", "createdBy", "teamUsers", "clients", "followers")

# Поля задач в параметрах виджета: оси таблиц и поле `issues`, `time-since`
WIDGET_FIELD_KEYS = ("fields", "field", "fieldX", "fieldY", "rowField", "columnField", "sumField")

# Статусы графиков `cycle-time` и `flow`
WIDGET_STATUS_LIST_KEYS = ("fromStatuses", "toStatuses", "excludedStatuses", "includedStatuses")

DASHBOARDS_STATE_FILE = "dashboards.cfg"

# Ответы на поиск дашбордов на установках, где его нет
SEARCH_UNAVAILABLE_STATUSES = (404, 405)

# Длина начала слова в запросе поиска дашбордов; иероглиф и хирагана берутся по одному символу
SEARCH_PREFIX_LENGTH = 15
SINGLE_CHARACTER_WORDS = ("CJK UNIFIED IDEOGRAPH", "HIRAGANA")


def _is_word_char(char: str) -> bool:
    # буквы, цифры, диакритика (например, огласовки деванагари)
    return unicodedata.category(char)[0] in "LNM"


class DashboardImporter(GetOrCreateImporter):
    """Переносит дашборды вместе с виджетами.

    Создание идёт в три шага: дашборд, затем виджеты по одному — при создании виджет положения не
    принимает, — и в конце одно обновление дашборда, которое раскладывает виджеты по местам.

    Созданные дашборды записываются в `state/dashboards.cfg`: по нему они находятся при
    повторном прогоне, в том числе на установках без поиска дашбордов."""

    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        global_field_importer: GlobalFieldImporter,
        filter_importer: FilterImporter,
        state_path: str,
        ui_url: str = DEFAULT_TRACKER_UI_URL,
        status_importer: Any = None,
        board_importer: Any = None,
    ):
        super().__init__(client, mapper)
        self._ui_url = ui_url
        self._status_importer = status_importer
        self._board_importer = board_importer
        self._acl = AclUpdater(client, mapper)
        self._owners = {}
        self._widget_urls = {}
        self._global_field_importer = global_field_importer
        self._filter_importer = filter_importer
        self._dashboards = get_dashboards(client)
        self._widgets = get_widgets(client)
        self._state = ImportState(state_path, DASHBOARDS_STATE_FILE)
        self._search_available = True

    @property
    def object_name(self) -> str:
        return "Dashboard"

    @property
    def widget_urls(self) -> Dict[str, str]:
        """Адреса созданных виджетов по их идентификаторам в исходной организации: по ним
        собираются ссылки метрик целей, проектов и портфелей."""
        return self._widget_urls

    def get_or_create(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        source_id = input_data.get("id")
        try:
            dashboard_obj = self._find_imported(input_data) or self._find_dashboard(input_data)
        except Exception:
            # не зная, создан ли дашборд раньше, создавать его нельзя: получится дубль
            logger.exception(f"Error searching dashboard {input_data.get('name')}, it is skipped")
            return None

        if dashboard_obj is None and source_id is None and not self._search_available:
            # без id дашборд не записать в файл, без поиска его не найти при повторном прогоне
            logger.error(
                f"Dashboard {input_data.get('name')} has no id in the dump and dashboard search is not "
                "available, it is skipped"
            )
            return None

        if dashboard_obj is None:
            dashboard_obj = self._try_creating_object(input_data)

        if dashboard_obj is not None:
            # запись до создания виджетов: если прогон прервётся на виджетах, следующий найдёт
            # дашборд по файлу
            if source_id is not None and self._state.get(str(source_id)) != str(dashboard_obj.id):
                self._state.save(str(source_id), str(dashboard_obj.id))
            self._import_widgets(input_data.get("widgets") or [], dashboard_obj)
            if input_data.get("permissions") is not None:
                self._update_permissions(dashboard_obj, input_data)

        return dashboard_obj

    def _update_permissions(self, dashboard_obj: Resource, input_data: Dict[str, Any]) -> None:
        """Права приводятся к выгрузке на каждом прогоне, как у шаблонов."""
        self._acl.update(
            f"dashboard {input_data.get('name')}",
            input_data["permissions"],
            lambda: get_optional(self._dashboards.get_permissions(dashboard_obj.id), "acl"),
            lambda data: update_template_permissions(
                self._dashboards, "dashboards", dashboard_obj.id, self._dashboards.get(dashboard_obj.id).version, data
            ),
        )

    def _find_imported(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        """Дашборд из `state/dashboards.cfg`. Удалённый (404) создаётся заново.

        Название и владелец сверяются с выгрузкой: каталог `state` общий для рабочего каталога, и
        запись могла остаться от другой выгрузки."""
        source_id = input_data.get("id")
        created_id = self._state.get(str(source_id)) if source_id is not None else None
        if created_id is None:
            return None

        try:
            dashboard_obj = self._dashboards.get(created_id)
        except NotFound:
            return None
        except Exception:
            logger.warning(
                f"Dashboard {created_id} recorded in state/dashboards.cfg for dashboard {source_id} "
                "of the dump can not be read"
            )
            raise

        name = input_data.get("name")
        owner = self._mapper.get_user(input_data.get("owner"))
        if get_optional(dashboard_obj, "name") != name or not self._has_owner(dashboard_obj, owner):
            logger.warning(
                f"Dashboard {created_id} recorded in state/dashboards.cfg for dashboard {source_id} "
                f"of the dump is not {name} of {owner}, it is not reused"
            )
            return None

        return dashboard_obj

    def _find_dashboard(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        """Поиск дашборда по названию и владельцу: своего ключа и тегов у дашборда нет. Если
        нашлось несколько, дашборд пропускается: какой из них обновлять, определить нельзя."""
        if not self._search_available:
            return None

        name = input_data.get("name")
        owner = self._mapper.get_user(input_data.get("owner"))

        # отсутствие поиска определяется по первому запросу; ошибка на следующих страницах — сбой,
        # дашборд пропускается
        try:
            pages = iter(self._dashboards.find(name=self._search_query(name)))
            first = next(pages, None)
        except TrackerServerError as error:
            if error.status_code not in SEARCH_UNAVAILABLE_STATUSES:
                raise
            self._search_available = False
            logger.warning(
                "Dashboard search is not available in this Tracker installation: dashboards created "
                "by earlier runs are found only by state/dashboards.cfg, keep it between runs"
            )
            return None

        candidates = ([first] if first is not None else []) + list(pages)
        found = self._select_dashboards(candidates, name, owner)
        if not found and self._search_query(name) != name:
            # повтор по полному названию, как искалось раньше
            found = self._select_dashboards(self._dashboards.find(name=name), name, owner)

        if len(found) > 1:
            raise DashboardNotUniqueError(name, len(found))

        if not found:
            return None

        # поиск отдаёт списочное представление без виджетов, а они нужны, чтобы понять, какие из
        # них уже созданы; заодно берётся свежая версия дашборда для последующего обновления
        return self._dashboards.get(found[0]["id"])

    def _select_dashboards(self, candidates: Any, name: Optional[str], owner: Optional[str]) -> List[Any]:
        # владелец в поиск не передаётся: если он не сопоставлен, Трекер отвечает ошибкой на весь
        # запрос, поэтому найденное сверяется с владельцем уже на своей стороне
        return [
            dashboard
            for dashboard in candidates
            if get_optional(dashboard, "name") == name and self._has_owner(dashboard, owner)
        ]

    @staticmethod
    def _search_query(name: Optional[str]) -> Optional[str]:
        """Запрос для поиска дашбордов: начала слов названия. По полному названию со знаками
        препинания или длинными словами поиск дашборд не находит.

        Из каждой части между пробелами берётся первая последовательность букв, цифр и
        диакритики, до 15 символов; из иероглифа и хираганы — один символ. Точное название
        проверяется после поиска."""
        if not name:
            return name

        words = []
        for part in name.split():
            start = next((i for i, char in enumerate(part) if _is_word_char(char)), None)
            if start is None:
                continue

            end = start
            while end < len(part) and _is_word_char(part[end]):
                end += 1

            if unicodedata.name(part[start], "").startswith(SINGLE_CHARACTER_WORDS):
                words.append(part[start])
            else:
                words.append(part[start:end][:SEARCH_PREFIX_LENGTH])

        return " ".join(words) or name

    def _has_owner(self, dashboard: Any, owner: Optional[str]) -> bool:
        """Найденный пользователь сравнивается по идентификаторам, ненайденный — по записи в выгрузке:
        имена не уникальны. Владелец дашборда приходит с `id`, равным `uid` пользователя, а `passportUid`
        и `cloudUid` есть не у всех пользователей."""
        if owner is None:
            return True

        dashboard_owner = get_optional(dashboard, "owner") or {}
        owner_ids = self._owner_ids(owner)
        if owner_ids:
            keys, refs = ("id", "passportUid", "cloudUid"), owner_ids
        else:
            keys, refs = ("id", "display"), {owner}

        return any(str(get_optional(dashboard_owner, key)) in refs for key in keys)

    def _owner_ids(self, owner: str) -> set:
        if owner not in self._owners:
            try:
                user = self._client.users.get(owner)
                ids = {
                    str(value)
                    for value in (get_optional(user, key) for key in ("uid", "trackerUid", "passportUid", "cloudUid"))
                    if value is not None
                }
            except NotFound:
                logger.warning(f"Owner {owner} of dashboards is not found: check mapping/users.cfg")
                ids = set()
            # прочий сбой пробрасывается: не зная владельца, дашборд прошлого прогона не узнать, и он
            # создался бы заново
            self._owners[owner] = ids
        return self._owners[owner]

    def _get_object(self, input_data: Dict[str, Any]) -> Optional[Resource]:
        return self._find_dashboard(input_data)

    def _create_object(self, prepared_data: Dict[str, Any]) -> Resource:
        if prepared_data.get("createdBy") and prepared_data.get("start"):
            return self._dashboards.import_object(**prepared_data)
        prepared_data.pop("createdBy", None)
        prepared_data.pop("start", None)
        return self._dashboards.create(**prepared_data)

    def _prepare_data(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Дашборд с автором и датой создания из выгрузки создаётся импортом; без них — обычным
        созданием."""
        return {
            "name": input_data.get("name"),
            "layout": input_data.get("layout"),
            "owner": self._mapper.get_user(input_data.get("owner")),
            "createdBy": self._mapper.get_user(input_data.get("createdBy")),
            "start": input_data.get("createdAt"),
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

        created_by = self._mapper.get_user(widget_data.get("createdBy"))
        created_at = widget_data.get("createdAt")
        try:
            if widget_data.get("type") in IMPORTABLE_WIDGET_TYPES and created_by and created_at:
                # автор и дата создания сохраняются у видов, которые принимает импорт виджетов
                return self._widgets.import_in_dashboard(
                    dashboard_obj.id, **prepared_data, createdBy=created_by, start=created_at
                )
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

        widget_type = prepared_data.get("type")

        # groupBy у виджета — это единица группировки (например `day`), а не поле задачи:
        # полем он бывает только внутри сохранённого фильтра
        if widget_type not in ENTITY_WIDGET_TYPES:
            for key in WIDGET_FIELD_KEYS:
                if key in prepared_data:
                    prepared_data[key] = self._prepare_fields(prepared_data[key])
            # фильтр графиков (`cycle-time`, `flow`, `velocity`) задаётся на верхнем уровне
            if prepared_data.get("filter"):
                prepared_data["filter"] = self._prepare_filter(prepared_data["filter"])
        elif isinstance(prepared_data.get("filter"), dict):
            prepared_data["filter"] = self._prepare_entity_filter(prepared_data["filter"])

        if prepared_data.get("filterParams"):
            prepared_data["filterParams"] = self._prepare_filter_params(prepared_data["filterParams"])
        else:
            prepared_data.pop("filterParams", None)

        named_filter = prepared_data.pop("namedFilter", None)
        if named_filter is not None:
            prepared_data["filterId"] = self._get_filter_id(ref_id(named_filter))

        self._prepare_statuses(prepared_data)
        self._prepare_board_refs(prepared_data)

        return prepared_data

    def _prepare_entity_filter(self, filter_data: Dict[str, Any]) -> Dict[str, Any]:
        """Пользователи в условиях виджетов целей и портфелей сопоставляются по `users.cfg`. Остальные
        условия — статус сущности (`according_to_plan`, `at_risk` и т. п.) и даты — переносятся без
        изменений: это постоянные значения, а не объекты организации."""
        prepared_data = dict(filter_data)
        for key in ENTITY_USER_FILTER_KEYS:
            value = prepared_data.get(key)
            if isinstance(value, list):
                prepared_data[key] = [self._mapper.get_user(user) for user in value]
            elif isinstance(value, str):
                prepared_data[key] = self._mapper.get_user(value)
        return prepared_data

    def _prepare_statuses(self, prepared_data: Dict[str, Any]) -> None:
        """Статусы графиков — ключами статусов целевой организации. В выгрузках прежних версий
        они записаны ссылками."""
        for key in WIDGET_STATUS_LIST_KEYS:
            if prepared_data.get(key):
                prepared_data[key] = [self._get_status_key(status) for status in prepared_data[key]]
        if prepared_data.get("forStatus"):
            prepared_data["forStatus"] = self._get_status_key(prepared_data["forStatus"])
        if prepared_data.get("statusColors"):
            # цвет удалённого статуса приходит без статуса
            prepared_data["statusColors"] = [
                {"status": self._get_status_key(item["status"]), "color": item.get("color")}
                for item in prepared_data["statusColors"]
                if item.get("status")
            ]

        settings = prepared_data.get("calculationSettings")
        if isinstance(settings, dict):
            settings = dict(settings)
            for key in ("toStatuses", "statuses"):
                if settings.get(key):
                    settings[key] = [self._get_status_key(status) for status in settings[key]]
            if settings.get("valueField"):
                settings["valueField"] = self._prepare_fields(ref_id(settings["valueField"]))
            prepared_data["calculationSettings"] = settings

    def _get_status_key(self, status: Any) -> str:
        key = ref_key(status)
        if self._status_importer is None:
            return self._mapper.get_status(key)
        return self._status_importer.get_or_create(key).key

    def _prepare_board_refs(self, prepared_data: Dict[str, Any]) -> None:
        """Доска группировки по спринтам и спринт диаграммы сгорания — номерами перенесённых.
        Без них график не имеет смысла: виджет пропускается."""
        bucket = prepared_data.get("bucket")
        if isinstance(bucket, dict) and bucket.get("board") is not None:
            source_id = ref_id(bucket["board"])
            board_id = self._board_importer.get_board_id(source_id) if self._board_importer else None
            if board_id is None:
                raise BoardNotInDumpError(f"Board {source_id}")
            prepared_data["bucket"] = {**bucket, "board": int(board_id)}

        if prepared_data.get("sprintId") is not None:
            source_id = prepared_data["sprintId"]
            sprint_id = self._board_importer.get_sprint_id(source_id) if self._board_importer else None
            if sprint_id is None:
                raise BoardNotInDumpError(f"Sprint {source_id}")
            prepared_data["sprintId"] = int(sprint_id)

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


def ref_id(value: Any) -> Any:
    """Номер объекта: в выгрузках прежних версий ссылки записаны объектами."""
    return get_optional(value, "id") if isinstance(value, dict) else value


def ref_key(value: Any) -> Any:
    """Ключ статуса: в выгрузках прежних версий ссылки записаны объектами."""
    return get_optional(value, "key") if isinstance(value, dict) else value
