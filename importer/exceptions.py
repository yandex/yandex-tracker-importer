class BaseTrackerError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self):
        return self.message


class MissingFieldError(BaseTrackerError):
    def __init__(self, data: dict, field_name: str):
        super().__init__(f"Missing required field {field_name} in data: {data}")
        self.data = data
        self.field_name = field_name


class ObjectNotCreatedError(BaseTrackerError):
    object_name = "Object"

    def __init__(self, key: str):
        super().__init__(f"{self.object_name} with key {key} was not created and does not exist")
        self.key = key


class StatusNotCreatedError(ObjectNotCreatedError):
    object_name = "Status"


class IssueTypeNotCreatedError(ObjectNotCreatedError):
    object_name = "Issue Type"


class ResolutionNotCreatedError(ObjectNotCreatedError):
    object_name = "Resolution"


class GlobalFieldNotCreatedError(ObjectNotCreatedError):
    object_name = "Global Field"


class GlobalFieldNotExistsError(BaseTrackerError):
    def __init__(self, key: str):
        super().__init__(f"Global field with key {key} does not exist")
        self.key = key


class LocalFieldNotExistsError(BaseTrackerError):
    def __init__(self, key: str, queue_key: str):
        super().__init__(f"Local field in queue {queue_key} with key {key} does not exist")
        self.key = key


class QueueObjectNotExistsError(BaseTrackerError):
    object_name = "Object"

    def __init__(self, queue_key: str, name: str):
        super().__init__(f"{self.object_name} {name} in queue {queue_key} does not exist")
        self.queue_key = queue_key
        self.name = name


class VersionNotExistsError(QueueObjectNotExistsError):
    object_name = "Version"


class ComponentNotExistsError(QueueObjectNotExistsError):
    object_name = "Component"


class ProjectNotExistsError(BaseTrackerError):
    object_name = "Project"

    def __init__(self, shortId: str):
        super().__init__(f"{self.object_name} with shortId {shortId} does not exist")
        self.shortId = shortId


class GoalNotExistsError(BaseTrackerError):
    object_name = "Goal"

    def __init__(self, reference: str):
        super().__init__(f"{self.object_name} {reference} does not exist")
        self.reference = reference


class GroupNotExistsError(BaseTrackerError):
    object_name = "Group"

    def __init__(self, name: str):
        super().__init__(f"{self.object_name} {name} does not exist")
        self.name = name


class UnsupportedActionError(BaseTrackerError):
    def __init__(self, action_type: str):
        super().__init__(f"Action {action_type} cannot be imported, the automation is skipped")
        self.action_type = action_type


class FieldTemplatesNotSupportedError(BaseTrackerError):
    def __init__(self, queue_key: str, fields: list):
        super().__init__(
            f"Fields {fields} of an issue created in another queue {queue_key} cannot be imported, "
            "the automation is skipped"
        )
        self.queue_key = queue_key
        self.fields = fields


class ObjectWithDataNotCreatedError(BaseTrackerError):
    object_name = "Object"

    def __init__(self, queue_key: str = None, data: dict = None):
        message = f"{self.object_name} with data {data} was not created and does not exist"
        if queue_key:
            message += f" in queue {queue_key}"
        super().__init__(message)
        self.queue_key = queue_key
        self.data = data


class WorkflowNotCreatedError(ObjectWithDataNotCreatedError):
    object_name = "Workflow"


class LocalFieldNotCreatedError(ObjectWithDataNotCreatedError):
    object_name = "Local field"


class ComponentNotCreatedError(ObjectWithDataNotCreatedError):
    object_name = "Component"


class VersionNotCreatedError(ObjectWithDataNotCreatedError):
    object_name = "Version"


class DashboardNotUniqueError(BaseTrackerError):
    def __init__(self, name: str, count: int):
        super().__init__(
            f"Found {count} dashboards named {name}, the one to update is ambiguous"
        )
        self.name = name


class FilterNotImportedError(BaseTrackerError):
    def __init__(self, filter_id: str):
        super().__init__(f"Filter {filter_id} was not imported, the widget is skipped")
        self.filter_id = filter_id


class LocalFieldNotSupportedError(BaseTrackerError):
    def __init__(self, key: str):
        super().__init__(
            f"Local field {key} is bound to a queue and cannot be used outside of it"
        )
        self.key = key


class FilterNameConflictError(BaseTrackerError):
    def __init__(self, name: str):
        super().__init__(
            f"Filter named {name} already exists for its owner. If it was created by a previous "
            "run, restore state/filters.cfg; otherwise rename the filter in the dump"
        )
        self.name = name


class BoardNotImportedError(BaseTrackerError):
    def __init__(self, name: str):
        super().__init__(f"{name} from the dump is not imported yet")
        self.name = name


class BoardNotInDumpError(BaseTrackerError):
    def __init__(self, name: str):
        super().__init__(f"{name} is not in the dump or cannot be created")
        self.name = name


class ConfigValidationError(Exception):
    pass
