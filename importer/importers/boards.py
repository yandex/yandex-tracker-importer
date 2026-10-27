import logging
import os
from typing import Any, Dict, List, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.exceptions import BadRequest, Forbidden, NotFound, UnprocessableEntity
from yandex_tracker_client.objects import Resource

from .entities import ProjectImporter
from .fields import GlobalFieldImporter
from ..exceptions import BoardNotImportedError
from ..mapper import EntityMapper
from ..state import ImportState
from ..tracker_collections import create_live_board, delete_sprint, update_sprint
from ..utils import load_yaml_file, prepare_field_value


logger = logging.getLogger(__name__)

BOARDS_FILE = "boards.yaml"
BOARDS_STATE_FILE = "boards.cfg"
SPRINTS_STATE_FILE = "sprints.cfg"
PERMISSIONS_FILE = "boards_permissions.md"
# компоненты и версии API принимает названием и ищет в очереди фильтра
QUEUE_BOUND_FIELDS = {"components", "fixVersions", "affectedVersions"}
CLOSED_SPRINT_STATUSES = {"released", "archived"}
# отказ API создать доску или спринт: повторный прогон его не создаст
REJECTED_BY_API = (BadRequest, UnprocessableEntity, Forbidden)


class RejectedError(Exception):
    """Доску или спринт нельзя создать из-за данных: задачи переносятся без них. `by_api` — отказал
    API (например, очереди из правила нет в целевой организации), а не сама выгрузка."""

    def __init__(self, message: str, by_api: bool = False):
        super().__init__(message)
        self.by_api = by_api


class BoardImporter:
    """Переносит доски и их спринты.

    Названия досок и спринтов не уникальны, поэтому созданные находятся по файлам в каталоге
    `state`. Доска создаётся закрытой: права на неё API не отдаёт, а шире исходных они не должны
    стать. Список досок, которым нужно открыть доступ, пишется в файл рабочего каталога.

    Задачи и функции переходов ссылаются на доски и спринты номерами исходной выгрузки:
    `get_board_id` и `get_sprint_id` переводят их в номера перенесённых."""

    def __init__(
        self,
        client: TrackerClient,
        mapper: EntityMapper,
        data_path: str,
        state_path: str,
        global_field_importer: GlobalFieldImporter,
        project_importer: ProjectImporter,
        ui_url: str,
        status_importer: Any = None,
        issue_type_importer: Any = None,
        resolution_importer: Any = None,
    ) -> None:
        self._client = client
        self._mapper = mapper
        self._state_path = state_path
        self._global_field_importer = global_field_importer
        self._project_importer = project_importer
        self._ui_url = ui_url
        # статусы, типы и резолюции досок могут не встречаться в очередях выгрузки
        self._reference_importers = {
            "status": status_importer, "type": issue_type_importer, "resolution": resolution_importer,
        }
        self._boards_state = ImportState(state_path, BOARDS_STATE_FILE)
        self._sprints_state = ImportState(state_path, SPRINTS_STATE_FILE)
        # файл есть, но не читается: ссылки на доски ждут, пока его не исправят, — иначе задачи
        # навсегда перенеслись бы без досок
        self._broken = False
        self._boards_data = self._load_boards(os.path.join(data_path, BOARDS_FILE))
        self._dumped_boards = {str(board["id"]) for board in self._boards_data}
        self._dumped_sprints = {str(sprint["id"]) for board in self._boards_data for sprint in board["sprints"]}
        self._imported = False
        # доски и спринты, которые не создаются из-за данных или ответа API: повторный прогон их не
        # создаст. Номера досок и спринтов в исходной организации пересекаются, поэтому множества разные
        self._unavailable_boards = set()
        self._unavailable_sprints = set()
        # доски и спринты, которые прочитаны или созданы в этом прогоне: на остальные ссылки ждут
        self._available_boards = set()
        self._available_sprints = set()

    def _load_boards(self, path: str) -> list:
        """Доска или спринт без `id` пропускаются: по нему на них ссылаются задачи. Нечитаемый файл
        не останавливает импорт очередей и задач."""
        if not os.path.exists(path):
            return []
        try:
            boards = load_yaml_file(path) or []
        except Exception:
            logger.exception(f"Error reading {path}, boards are not imported and issues on them wait: fix the file")
            self._broken = True
            return []
        if not isinstance(boards, list):
            logger.error(f"{path} is not a list, boards are not imported and issues on them wait: fix the file")
            self._broken = True
            return []
        loaded = []
        for board in boards:
            if not isinstance(board, dict) or board.get("id") is None:
                logger.error(f"Board without id in {path} is skipped: {board}")
                continue
            sprints = [sprint for sprint in board.get("sprints") or [] if isinstance(sprint, dict) and sprint.get("id") is not None]
            if len(sprints) != len(board.get("sprints") or []):
                logger.error(f"Sprints without id of board {board['id']} are skipped")
            loaded.append({**board, "sprints": sprints})
        return loaded

    def in_dump(self, source_id: Any) -> bool:
        """Доска из выгрузки, которую можно перенести: отклонённая API в этом прогоне — нет."""
        if self._broken:
            return True
        return str(source_id) in self._dumped_boards and str(source_id) not in self._unavailable_boards

    def get_board_id(self, source_id: Any) -> Optional[str]:
        """Номер перенесённой доски. None — доски нет в выгрузке (например, старого типа) или её
        нельзя создать из-за данных: ссылка на неё не переносится. Доска, которая не создалась из-за
        временного сбоя, — ошибка: задача или функция перехода дождутся её на повторном прогоне."""
        if self._broken:
            raise BoardNotImportedError(f"Board {source_id}")
        if str(source_id) not in self._dumped_boards or str(source_id) in self._unavailable_boards:
            return None
        if not self._imported or str(source_id) not in self._available_boards:
            raise BoardNotImportedError(f"Board {source_id}")
        return self._boards_state.get(str(source_id))

    def get_sprint_id(self, source_id: Any) -> Optional[str]:
        if self._broken:
            raise BoardNotImportedError(f"Sprint {source_id}")
        if str(source_id) not in self._dumped_sprints or str(source_id) in self._unavailable_sprints:
            return None
        if not self._imported or str(source_id) not in self._available_sprints:
            raise BoardNotImportedError(f"Sprint {source_id}")
        return self._sprints_state.get(str(source_id))

    def import_boards(self, after_failure: bool = False) -> None:
        """После сбоя импорта очередей отказ API не считается окончательным: доска могла не
        создаться из-за очереди, которая не успела импортироваться, — её задачи ждут повторного
        прогона, а не переносятся без неё."""
        for board_data in self._boards_data:
            name = f"board {board_data.get('id')} {board_data.get('name')}"
            try:
                board = self._get_or_create_board(board_data)
            except RejectedError as error:
                if after_failure and error.by_api:
                    logger.exception(f"Error importing {name}, its issues wait for it: run the import again")
                    continue
                logger.exception(f"Error importing {name}, its issues are imported without it")
                self._unavailable_boards.add(str(board_data["id"]))
                self._unavailable_sprints.update(str(sprint["id"]) for sprint in board_data["sprints"])
                continue
            except Exception:
                logger.exception(f"Error importing {name}, its issues wait for it: run the import again")
                continue
            self._available_boards.add(str(board_data["id"]))
            for sprint_data in board_data["sprints"]:
                sprint_name = f"sprint {sprint_data.get('id')} {sprint_data.get('name')} of {name}"
                try:
                    self._get_or_create_sprint(board, sprint_data)
                    self._available_sprints.add(str(sprint_data["id"]))
                except RejectedError:
                    logger.exception(f"Error importing {sprint_name}, its issues are imported without it")
                    self._unavailable_sprints.add(str(sprint_data["id"]))
                except Exception:
                    logger.exception(f"Error importing {sprint_name}, its issues wait for it: run the import again")
        self._imported = True
        try:
            self._write_permissions_file()
        except Exception:
            logger.exception("Error writing the list of boards to set permissions")

    def _get_or_create_board(self, board_data: Dict[str, Any]) -> Resource:
        source_id = str(board_data["id"])
        created_id = self._boards_state.get(source_id)
        if created_id is not None:
            try:
                board = self._client.boards.get(created_id)
            except NotFound:
                # доску прежнего прогона удалили: создаётся заново; другая ошибка чтения передаётся
                # выше — новая доска стала бы дубликатом
                logger.info(f"Board {created_id} created by a previous run is not found, it is created again")
            else:
                self._create_missing_notes(board, board_data)
                return board

        try:
            prepared = self._prepare_board(board_data)
        except (KeyError, ValueError) as error:
            raise RejectedError(f"Board {source_id} cannot be prepared: {error!r}") from error
        try:
            board = create_live_board(self._client, prepared)
        except REJECTED_BY_API as error:
            raise RejectedError(f"Board {source_id} is rejected by the API", by_api=True) from error
        self._boards_state.save(source_id, str(board.id))
        logger.info(f"Board {board_data['name']} is created as {board.id}")
        if board_data.get("sprintsAvailable"):
            self._delete_empty_sprints(board)
        self._create_missing_notes(board, board_data)
        return board

    def _delete_empty_sprints(self, board: Resource) -> None:
        """Доска со спринтами сама заводит пустой спринт с текущими датами: спринты переносятся из
        выгрузки."""
        try:
            for sprint in self._client.boards[board.id].sprints.get_all():
                delete_sprint(self._client, sprint.id, sprint.version)
        except Exception:
            logger.exception(f"Error deleting the empty sprint of board {board.id}, delete it manually")

    def _create_missing_notes(self, board: Resource, board_data: Dict[str, Any]) -> None:
        """Заметки создаются и у доски прежнего прогона: прогон мог прерваться сразу после доски.
        Заметка, которая не создалась, на перенос доски и задач не влияет."""
        notes = board_data.get("notes") or []
        if not notes:
            return
        try:
            existing = {str(note.get("columnId")) for note in self._client.boards[board.id].notes.get_all()}
            # колонка выгрузки без оставшихся статусов на доске не создаётся
            columns = {str(column.id) for column in self._client.boards[board.id].columns.get_all()}
            for note in notes:
                if str(note["column"]) not in columns:
                    logger.info(f"Note of column {note['column']} of board {board.id} is skipped: the board has no such column")
                    continue
                if str(note["column"]) not in existing:
                    self._client.boards[board.id].notes.create_for_column(note["column"], note.get("text"))
        except Exception:
            logger.exception(f"Error creating notes of board {board.id}, set them manually")

    def _prepare_board(self, board_data: Dict[str, Any]) -> Dict[str, Any]:
        auto_filters = board_data.get("autoFilters") or {}
        prepared = {
            "name": board_data["name"],
            "boardPermissionsTemplate": "private",
            "sprintsAvailable": bool(board_data.get("sprintsAvailable")),
            "backlogAvailable": bool(board_data.get("sprintsAvailable")),
            "columns": self._prepare_columns(board_data),
            "autoFilters": {
                **({"addFilter": self._prepare_add_filter(auto_filters["addFilter"])} if auto_filters.get("addFilter") else {}),
                **(
                    {"removeFilter": self._prepare_remove_filter(auto_filters["removeFilter"])}
                    if auto_filters.get("removeFilter") else {}
                ),
            },
        }
        if board_data.get("estimateBy"):
            prepared["estimateBy"] = self._global_field_importer.get_or_create(board_data["estimateBy"]).id
        return prepared

    def _prepare_columns(self, board_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """API не принимает один статус в нескольких колонках и колонку без статусов: статус, с
        которым сопоставлены статусы выгрузки из разных колонок, остаётся в первой из них, а колонка
        без оставшихся статусов не создаётся."""
        columns, used = [], set()
        for column in board_data.get("columns") or []:
            statuses = []
            for status in column.get("statuses") or []:
                status = self._get_status(status)
                if status in statuses:
                    continue
                if status in used:
                    logger.warning(
                        f"Status {status} of board {board_data['name']} is already in an earlier column, "
                        f"it is not added to column {column['name']}"
                    )
                    continue
                statuses.append(status)
            if column.get("statuses") and not statuses:
                logger.warning(f"Column {column['name']} of board {board_data['name']} has no statuses left, it is not created")
                continue
            used.update(statuses)
            columns.append({
                "id": column["id"],
                "name": column["name"],
                "statuses": statuses,
                **({"limit": column["limit"]} if column.get("limit") else {}),
            })
        return columns

    def _prepare_add_filter(self, add_filter: Dict[str, Any]) -> Dict[str, Any]:
        """Условие, которое не удаётся перенести, — ошибка: без него на доску попадали бы лишние
        задачи."""
        field_values = {}
        for field_key, values in (add_filter.get("fieldValues") or {}).items():
            if field_key.startswith("local_"):
                raise ValueError(f"Local field {field_key} in the board filter is not supported")
            field_obj = None if field_key == "queue" else self._global_field_importer.get_or_create(field_key)
            field_values[field_key if field_obj is None else field_obj.id] = [
                self._prepare_filter_value(field_key, field_obj, value) for value in values or []
            ]
        return {"enabled": add_filter.get("enabled", True), "liveFilter": {"fieldValues": field_values}}

    def _prepare_filter_value(self, field_key: str, field_obj: Any, value: Dict[str, Any]) -> Dict[str, Any]:
        if "fixed" not in value:
            # макрос, диапазон: значение не ссылается на объекты организации
            return value
        fixed = value["fixed"]
        if self._reference_importers.get(field_key) is not None and isinstance(fixed, str):
            self._reference_importers[field_key].get_or_create(fixed)
        if field_key == "queue":
            fixed = self._mapper.get_queue(fixed)
        elif field_key not in QUEUE_BOUND_FIELDS:
            fixed = prepare_field_value(field_obj, fixed, self._mapper, project_importer=self._project_importer)
        return {**value, "fixed": fixed}

    def _get_status(self, status: str) -> str:
        if self._reference_importers["status"] is not None:
            self._reference_importers["status"].get_or_create(status)
        return self._mapper.get_status(status)

    def _prepare_remove_filter(self, remove_filter: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "enabled": remove_filter.get("enabled", True),
            "statuses": list(dict.fromkeys(self._get_status(status) for status in remove_filter.get("statuses") or [])),
            "checkResolutionPresence": remove_filter.get("checkResolutionPresence"),
            "maxTimeInToRemoveState": remove_filter.get("maxTimeInToRemoveState"),
        }

    def _get_or_create_sprint(self, board: Resource, sprint_data: Dict[str, Any]) -> None:
        """Спринт создаётся черновиком с исходными датами, затем ему ставится исходный статус:
        сразу завершённым или архивным API его не создаёт, а запуск переписал бы дату начала."""
        source_id = str(sprint_data["id"])
        sprint = None
        created_id = self._sprints_state.get(source_id)
        if created_id is not None:
            try:
                sprint = self._client.sprints.get(created_id)
            except NotFound:
                logger.info(f"Sprint {created_id} created by a previous run is not found, it is created again")
        if sprint is None:
            try:
                sprint = self._client.sprints.create(
                    name=sprint_data["name"],
                    board={"id": board.id},
                    startDate=sprint_data["startDateTime"],
                    endDate=sprint_data["endDateTime"],
                )
            except (KeyError,) + REJECTED_BY_API as error:
                raise RejectedError(f"Sprint {source_id} is rejected: {error!r}") from error
            self._sprints_state.save(source_id, str(sprint.id))

        status = sprint_data.get("status")
        # статус ставится новому спринту и черновику прежнего прогона, у которого он не встал;
        # статус, который поменяли в целевой организации, не перетирается
        if status and status != sprint.status and (created_id is None or sprint.status == "draft"):
            update = {"status": status}
            if status in CLOSED_SPRINT_STATUSES:
                # задачи спринта остаются в нём: их переносит импорт задач
                update["skipUnresolved"] = True
            try:
                update_sprint(self._client, sprint.id, sprint.version, update)
            except Exception:
                # спринт есть, задачи переносятся в него; статус черновика ставится повторным прогоном
                logger.exception(f"Error setting status {status} of sprint {sprint.id}, it is set on the next run")

    def _write_permissions_file(self) -> None:
        lines = []
        for board_data in self._boards_data:
            created_id = self._boards_state.get(str(board_data["id"]))
            if created_id is None:
                continue
            author = f", автор в исходной системе — {board_data['createdBy']}" if board_data.get("createdBy") else ""
            lines.append(f"- [{board_data['name']}]({self._ui_url}/agile/board/{created_id}){author}")
        if not lines:
            return

        path = os.path.join(self._state_path, PERMISSIONS_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# Доступ к перенесённым доскам\n\n"
                "Доски созданы закрытыми: права досок импорт не переносит. Откройте доступ к каждой, как в "
                "исходной системе, — в настройках доски.\n\n"
                + "\n".join(lines) + "\n"
            )
        logger.warning(f"Boards are created private, set their permissions: see {path}")
