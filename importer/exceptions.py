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


class ConfigValidationError(Exception):
    pass
