import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from yandex_tracker_client import TrackerClient
from yandex_tracker_client.objects import Resource

from .constants import DEFAULT_TRACKER_UI_URL
from .mapper import EntityMapper
from .utils import get_optional, get_required_field, load_yaml_file
from .importers.global_objects import (
    StatusImporter,
    IssueTypeImporter,
    ResolutionImporter
)
from .importers.queue_objects import (
    WorkflowImporter,
    QueueImporter,
    ComponentImporter,
    VersionImporter
)
from .importers.automations import (
    MacrosImporter,
    TriggerImporter,
    AutoactionImporter
)
from .importers.fields import (
    CategoryImporter,
    LocalFieldImporter,
    GlobalFieldImporter
)
from .importers.dashboards import DashboardImporter
from .importers.filters import FilterImporter
from .importers.entities import (
    EntityImporter,
    ProjectImporter,
    GoalImporter,
    PortfolioImporter
)
from .importers.issues import IssueImporter
from .exceptions import (
    WorkflowNotCreatedError,
    LocalFieldNotCreatedError,
    ComponentNotCreatedError,
    VersionNotCreatedError
)
from .link_collectors import EntityLinksCollector, IssueLinksCollector
from .ui import ImportProgressTracker


logger = logging.getLogger(__name__)


class TrackerImporter:
    def __init__(
            self,
            client: TrackerClient,
            mapper: EntityMapper,
            data_path: str,
            state_path: Optional[str] = None,
            max_workers: Optional[int] = None,
            ui_url: str = DEFAULT_TRACKER_UI_URL,
    ):
        self._client = client
        self._mapper = mapper
        self._data_path = data_path
        # состояние импорта живёт рядом с рабочим каталогом, а не внутри выгрузки:
        # выгрузку копируют между людьми, и чужое состояние уехало бы вместе с ней
        self._state_path = state_path or data_path
        self._max_workers = max_workers or os.cpu_count()
        self._ui_url = ui_url

        self._progress_tracker = ImportProgressTracker(data_path)

        self._issue_links_collector = IssueLinksCollector(mapper)
        self._entity_links_collector = EntityLinksCollector()
        self._status_importer = StatusImporter(client, mapper, data_path)
        self._issue_type_importer = IssueTypeImporter(client, mapper, data_path)
        self._resolution_importer = ResolutionImporter(client, mapper, data_path)
        self._category_importer = CategoryImporter(client, mapper, data_path)
        self._global_field_importer = GlobalFieldImporter(client, mapper, data_path, self._category_importer)
        self._workflow_importer = WorkflowImporter(client, mapper)
        self._queue_importer = QueueImporter(client, mapper)
        self._project_importer = ProjectImporter(client, mapper, self._entity_links_collector, ui_url)
        self._goal_importer = GoalImporter(client, mapper, self._entity_links_collector, ui_url)
        self._portfolio_importer = PortfolioImporter(client, mapper, self._entity_links_collector, ui_url)

        self._filter_importer = FilterImporter(
            client, mapper, self._global_field_importer, self._state_path
        )
        self._dashboard_importer = DashboardImporter(
            client, mapper, self._global_field_importer, self._filter_importer, self._state_path, ui_url
        )

        self._trigger_importers = {}
        self._triggers_collector = {}

    def do_import(self) -> ImportProgressTracker:
        self._import_entities()
        self._import_entity_links_with_threads()
        try:
            self._import_queues()
        finally:
            # условия переходов могут ссылаться на очереди, которые импортировались позже; при сбое
            # дополняются воркфлоу уже импортированных очередей
            self._workflow_importer.complete_workflows()
        self._import_issue_links_with_threads()
        self._import_triggers()
        # дашборды последними: их виджеты ссылаются на очереди, поля и фильтры
        self._import_filters()
        self._import_dashboards()
        self._update_entities_metrics()

        return self._progress_tracker

    def _update_entities_metrics(self) -> None:
        """Метрики проставляются после дашбордов: до их импорта адрес виджета ещё неизвестен."""
        widget_urls = self._dashboard_importer.widget_urls

        for importer in [self._project_importer, self._goal_importer, self._portfolio_importer]:
            for entity_id, metric_items in importer.metric_items.items():
                entity_obj = importer.get_from_cache(entity_id)
                if entity_obj is None:
                    continue

                prepared_items = [
                    prepared_item
                    for prepared_item in (
                        self._prepare_metric_item(item, widget_urls, entity_id)
                        for item in metric_items
                    )
                    if prepared_item is not None
                ]
                if not prepared_items:
                    continue

                try:
                    entity_obj.update(fields={"metricItems": prepared_items})
                except Exception:
                    logger.exception(
                        f"Error updating metrics of entity {entity_id} with data: {prepared_items}"
                    )

    @staticmethod
    def _prepare_metric_item(
        metric_item: Dict[str, Any], widget_urls: Dict[str, str], entity_id: str
    ) -> Optional[Dict[str, Any]]:
        if metric_item.get("widget"):
            widget_url = widget_urls.get(str(metric_item["widget"]))
            if widget_url is None:
                logger.warning(
                    f"Widget {metric_item['widget']} of entity {entity_id} was not imported, "
                    "its metric is skipped"
                )
                return None

            return {"text": metric_item.get("text"), "url": widget_url}

        return {"text": metric_item.get("text"), "url": metric_item.get("url")}

    def _import_filters(self) -> None:
        filters_path = os.path.join(self._data_path, "filters.yaml")
        if not os.path.exists(filters_path):
            return

        for filter_data in load_yaml_file(filters_path) or []:
            self._filter_importer.get_or_create(filter_data)

    def _import_dashboards(self) -> None:
        dashboards_path = os.path.join(self._data_path, "dashboards")
        if not os.path.exists(dashboards_path):
            return

        for dirname in sorted(os.listdir(dashboards_path)):
            dashboard_path = os.path.join(dashboards_path, dirname, "dashboard.yaml")
            if not os.path.exists(dashboard_path):
                continue

            self._dashboard_importer.get_or_create(load_yaml_file(dashboard_path))

    def _import_queues(self) -> None:
        queues_path = os.path.join(self._data_path, "queues")
        if not os.path.exists(queues_path):
            return

        queue_dirs = [
            os.path.join(queues_path, dirname)
            for dirname in os.listdir(queues_path)
            if os.path.exists(os.path.join(queues_path, dirname, "queue.yaml"))
        ]

        for queue_dir in queue_dirs:
            queue_file_path = os.path.join(queue_dir, "queue.yaml")
            queue_data = load_yaml_file(queue_file_path)

            self._import_statuses(queue_data)
            self._import_issue_types(queue_data)
            self._import_resolutions(queue_data)

            queue_obj = self._queue_importer.get_or_create(queue_data)
            self._progress_tracker.report_queue(bool(queue_obj))

            if queue_obj is not None:
                # права — сразу: сбой дальше не должен оставить очередь с доступом по умолчанию
                self._queue_importer.update_permissions(queue_obj, queue_data)

                # локальные поля, компоненты и версии — до воркфлоу: на них ссылаются условия, экраны
                # и функции переходов
                local_field_importer = LocalFieldImporter(
                    client=self._client,
                    mapper=self._mapper,
                    queue_obj=queue_obj,
                    category_importer=self._category_importer,
                )
                self._import_local_fields(queue_data, local_field_importer)

                component_importer = ComponentImporter(
                    client=self._client,
                    mapper=self._mapper,
                    queue_obj=queue_obj
                )
                self._import_components(queue_data, component_importer)

                version_importer = VersionImporter(
                    client=self._client,
                    mapper=self._mapper,
                    queue_obj=queue_obj
                )
                self._import_versions(queue_data, version_importer)

                self._workflow_importer.set_queue_context(
                    local_field_importer,
                    self._global_field_importer,
                    component_importer,
                    version_importer,
                    self._project_importer,
                )
                self._import_workflows(queue_data)
                self._queue_importer.import_issue_types_config(queue_obj, queue_data)

                macros_importer = MacrosImporter(
                    client=self._client,
                    mapper=self._mapper,
                    queue_obj=queue_obj,
                    local_field_importer=local_field_importer,
                    global_field_importer=self._global_field_importer,
                    component_importer=component_importer,
                    version_importer=version_importer,
                    project_importer=self._project_importer
                )
                for macro_data in queue_data.get("macros", []):
                    macros_importer.get_or_create(macro_data)

                autoaction_importer = AutoactionImporter(
                    client=self._client,
                    mapper=self._mapper,
                    queue_obj=queue_obj,
                    local_field_importer=local_field_importer,
                    global_field_importer=self._global_field_importer,
                    component_importer=component_importer,
                    version_importer=version_importer,
                    project_importer=self._project_importer
                )
                for autoaction_data in queue_data.get("autoactions", []):
                    autoaction_importer.get_or_create(autoaction_data)

                if queue_data.get("triggers"):
                    self._trigger_importers[queue_obj.key] = TriggerImporter(
                        client=self._client,
                        mapper=self._mapper,
                        queue_obj=queue_obj,
                        local_field_importer=local_field_importer,
                        global_field_importer=self._global_field_importer,
                        component_importer=component_importer,
                        version_importer=version_importer,
                        project_importer=self._project_importer
                    )
                    self._triggers_collector[queue_obj.key] = queue_data["triggers"]

                issues_importer = IssueImporter(
                    client=self._client,
                    mapper=self._mapper,
                    global_field_importer=self._global_field_importer,
                    local_field_importer=local_field_importer,
                    component_importer=component_importer,
                    version_importer=version_importer,
                    project_importer=self._project_importer,
                    goal_importer=self._goal_importer,
                    issue_links_collector=self._issue_links_collector,
                    ui_url=self._ui_url
                )
                self._import_issues_with_threads(queue_dir, issues_importer)

    def _import_statuses(self, queue_data: Dict[str, Any]) -> None:
        for workflow in get_required_field(queue_data, "workflows"):
            for step in get_required_field(workflow, "steps"):
                status_key = get_required_field(step, "status")
                self._status_importer.get_or_create(status_key)

    def _import_issue_types(self, queue_data: Dict[str, Any]) -> None:
        for config in get_required_field(queue_data, "issueTypesConfig"):
            issue_type_key = get_required_field(config, "issueType")
            self._issue_type_importer.get_or_create(issue_type_key)

    def _import_resolutions(self, queue_data: Dict[str, Any]) -> None:
        for config in get_required_field(queue_data, "issueTypesConfig"):
            for resolution_key in config.get("resolutions", []):
                self._resolution_importer.get_or_create(resolution_key)

    def _import_workflows(self, queue_data: Dict[str, Any]) -> None:
        for workflow_data in get_required_field(queue_data, "workflows"):
            workflow_data["queue"] = queue_data["key"]
            workflow = self._workflow_importer.get_or_create(workflow_data)
            if not workflow:
                raise WorkflowNotCreatedError(
                    self._mapper.get_queue(queue_data["key"]),
                    workflow_data
                )

    def _import_local_fields(self, queue_data: Dict[str, Any], local_field_importer: LocalFieldImporter) -> None:
        for local_field_data in queue_data.get("local_fields", []):
            local_field = local_field_importer.get_or_create(local_field_data)
            if not local_field:
                raise LocalFieldNotCreatedError(
                    self._mapper.get_queue(queue_data["key"]),
                    local_field_data
                )

    def _import_components(self, queue_data: Dict[str, Any], component_importer: ComponentImporter) -> None:
        for component_data in queue_data.get("components", []):
            component = component_importer.get_or_create(component_data)
            if not component:
                raise ComponentNotCreatedError(
                    self._mapper.get_queue(queue_data["key"]),
                    component_data
                )

    def _import_versions(self, queue_data: Dict[str, Any], version_importer: VersionImporter) -> None:
        for version_data in queue_data.get("versions", []):
            version = version_importer.get_or_create(version_data)
            if not version:
                raise VersionNotCreatedError(
                    self._mapper.get_queue(queue_data["key"]),
                    version_data
                )
        # следующая версия, выпуск и архив — когда созданы все версии очереди, но до задач:
        # архивирование убирает версию у задач без резолюции, а в архивную версию задача
        # импортируется
        for version_data in queue_data.get("versions", []):
            try:
                version_importer.update_settings(version_data)
            except Exception:
                logger.exception(f"Error updating version {version_data} of queue {queue_data.get('key')}")

    def _import_issues_with_threads(self, queue_data_path: str, issue_importer: IssueImporter) -> None:
        issue_dirs = [
            os.path.join(queue_data_path, dirname)
            for dirname in os.listdir(queue_data_path)
            if os.path.isdir(os.path.join(queue_data_path, dirname))
        ]
        futures = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for issue_dir in issue_dirs:
                future = executor.submit(self._import_issue, issue_dir, issue_importer)
                futures.append(future)

            for future in futures:
                self._progress_tracker.report_issue(bool(future.result()))

    def _import_issue(self, issue_data_path: str, issue_importer: IssueImporter) -> None:
        issue_file_path = os.path.join(issue_data_path, "issue.yaml")
        issue_data = load_yaml_file(issue_file_path)

        attachments_path = os.path.join(issue_data_path, "attachments")
        issue_obj = issue_importer.get_or_create(issue_data, attachments_path)
        return issue_obj

    def _import_issue_links_with_threads(self) -> None:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for issue_obj, links in self._issue_links_collector.collected_links.items():
                for link_data in links:
                    executor.submit(self._import_issue_link, issue_obj, link_data)

    def _import_entities(self) -> None:
        projects_path = os.path.join(self._data_path, "projects")
        self._import_entity(projects_path, self._project_importer, self._progress_tracker.report_project)

        goals_path = os.path.join(self._data_path, "goals")
        self._import_entity(goals_path, self._goal_importer, self._progress_tracker.report_goal)

        portfolios_path = os.path.join(self._data_path, "portfolios")
        self._import_entity(portfolios_path, self._portfolio_importer, self._progress_tracker.report_portfolio)

        self._update_parents_entities()
        self._update_permission_sources()

    def _import_entity(self, entity_path: str, importer: EntityImporter, entity_progress_callback: Callable) -> None:
        if os.path.exists(entity_path):
            dirs = [d for d in os.listdir(entity_path) if os.path.isdir(os.path.join(entity_path, d))]
            for dirname in dirs:
                data_path = os.path.join(entity_path, dirname)
                file_path = os.path.join(data_path, importer.filename)

                if os.path.exists(file_path):
                    entity_data = load_yaml_file(file_path)
                    attachment_path = os.path.join(data_path, "attachments")
                    entity_obj = importer.get_or_create(entity_data, attachment_path)
                    entity_progress_callback(bool(entity_obj))

    def _get_entity_by_id(self, entity_id: str) -> Optional[Resource]:
        return (
            self._project_importer.get_from_cache(entity_id)
            or self._goal_importer.get_from_cache(entity_id)
            or self._portfolio_importer.get_from_cache(entity_id)
        )

    def _update_parents_entities(self) -> None:
        for importer in [self._project_importer, self._goal_importer, self._portfolio_importer]:
            for child_id, parents in importer.parent_entities.items():
                child_obj = self._get_entity_by_id(child_id)

                primary_parent_obj = self._get_entity_by_id(parents["primary"])
                secondary_parent_objs = [self._get_entity_by_id(parent_id) for parent_id in parents["secondary"]]

                for parent_obj, parent_id in zip(
                    [primary_parent_obj] + secondary_parent_objs,
                    [parents["primary"]] + parents["secondary"],
                ):
                    if parent_id and parent_obj is None:
                        logger.error(f"Parent entity with id {parent_id} does not exists")
                        continue

                update_data = {"parentEntity": {}}
                if primary_parent_obj:
                    update_data["parentEntity"]["primary"] = primary_parent_obj.id
                if secondary_parent_objs:
                    update_data["parentEntity"]["secondary"] = [
                        parent_obj.id for parent_obj in secondary_parent_objs if parent_obj is not None
                    ]

                try:
                    child_obj.update(fields=update_data)
                except Exception:
                    logger.exception(f"Error updating parent for entity {child_id}")

    def _update_permission_sources(self) -> None:
        """Наследование доступа приводится к выгрузке: недостающие источники добавляются, лишние
        снимаются. Если какой-то источник в целевой организации не найден, лишние не снимаются —
        среди них может оказаться его же копия."""
        importers_by_type = {
            "project": self._project_importer,
            "goal": self._goal_importer,
            "portfolio": self._portfolio_importer,
        }
        for importer in importers_by_type.values():
            for entity_id, sources in importer.permission_sources.items():
                entity_obj = importer.get_from_cache(entity_id)
                if entity_obj is None:
                    continue

                wanted, all_found = [], isinstance(sources, list)
                for source in sources if isinstance(sources, list) else []:
                    source_importer = importers_by_type.get(source.get("type")) if isinstance(source, dict) else None
                    source_obj = source_importer.get_from_cache(str(source.get("id"))) if source_importer else None
                    if source_obj is None:
                        logger.error(
                            f"Permission source {source} of {importer.object_name} {entity_id} does not exist"
                        )
                        all_found = False
                    elif source_obj.id not in wanted:
                        wanted.append(source_obj.id)

                try:
                    entity = importer.objects[entity_obj.id]
                    current = [
                        get_optional(source, "id")
                        for source in entity.extended_permissions.get("permissionSources") or []
                    ]
                    to_add = [source_id for source_id in wanted if source_id not in current]
                    to_remove = [source_id for source_id in current if source_id not in wanted] if all_found else []
                    # Источником API принимает только прямого родителя сущности. Если добавить не
                    # удалось, текущие источники не снимаются: иначе сущность осталась бы совсем без
                    # наследования
                    if to_add:
                        entity.update_extended_permissions({"permissionSources": {"add": to_add}})
                    if to_remove:
                        entity.update_extended_permissions({"permissionSources": {"remove": to_remove}})
                except Exception:
                    logger.exception(f"Error updating permission sources of {importer.object_name} {entity_id}")

    def _import_entity_links_with_threads(self) -> None:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for entity_id, links in self._entity_links_collector.collected_links.items():
                for link_data in links:
                    executor.submit(self._import_entity_link, entity_id, link_data)

    def _import_issue_link(self, issue_obj: Resource, link_data: Dict[str, Any]) -> Optional[Resource]:
        try:
            prepared_data = {
                "issue": self._mapper.get_issue_key(link_data["issue"]),
                "relationship": link_data["relationship"],
                "createdAt": link_data.get("createdAt"),
                "createdBy": self._mapper.get_user(link_data.get("createdBy")),
                "updatedAt": link_data.get("updatedAt"),
                "updatedBy": self._mapper.get_user(link_data.get("updatedBy"))
            }
        except Exception:
            logger.exception(
                f"Error preparing issue link data {issue_obj.key} -> {self._mapper.get_issue_key(link_data.get('issue'))} "
                f"with current data: {link_data}"
            )
            return None

        try:
            return issue_obj.links.import_object(**prepared_data)
        except Exception:
            logger.warning(f"Error importing issue link: {issue_obj.key} -> {prepared_data.get('issue')}")
        try:
            logger.info(f"Attempt to create issue link {prepared_data}")
            return issue_obj.links.create(
                **{
                    "issue": prepared_data["issue"],
                    "relationship": prepared_data["relationship"],
                }
            )
        except Exception:
            logger.exception(
                f"Error creating issue link: {issue_obj.key} -> {prepared_data.get('issue')} "
                f"with current data: {prepared_data}"
            )
        return None

    def _import_entity_link(self, entity_id: str, link_data: Dict[str, Any]) -> Optional[Resource]:
        try:
            source_entity = self._get_entity_by_id(entity_id)
            target_entity = self._get_entity_by_id(str(link_data["entity"]))
            if not target_entity:
                logger.error(
                    f"Error importing entity link: {entity_id} -> {link_data.get('entity')}: "
                    f"entity with id {link_data.get('entity')} does not exists"
                )
                return None

            prepared_data = {
                "entity": target_entity.id,
                "relationship": link_data["relationship"],
            }
        except Exception:
            logger.exception(
                f"Error preparing entity link data {entity_id} -> {link_data.get('entity')} "
                f"with current data: {link_data}"
            )
            return None

        try:
            return source_entity.links.create(**prepared_data)
        except Exception:
            logger.exception(
                f"Error creating entity link: {entity_id} -> {prepared_data.get('entity')} "
                f"with current data: {prepared_data}"
            )
        return None

    def _import_triggers(self) -> None:
        for queue_key, triggers in self._triggers_collector.items():
            for trigger_data in triggers:
                self._trigger_importers[queue_key].get_or_create(trigger_data)
