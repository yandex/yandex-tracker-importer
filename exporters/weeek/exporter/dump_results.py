import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple, Union

logger = logging.getLogger(__name__)


TaskDumpResult = Union['TaskDumpSuccess', 'TaskDumpError']
ProjectDumpResult = Union['ProjectDumpSuccess', 'ProjectDumpError']


class DumpResult(ABC):

    @abstractmethod
    def log(self) -> None:
        pass


@dataclass
class TaskDumpSuccess(DumpResult):
    task_id: str
    issue_key: str

    def log(self) -> None:
        logger.debug(f"Task {self.task_id} exported as {self.issue_key}")


@dataclass
class TaskDumpError(DumpResult):
    task_id: str
    error: Exception

    def log(self) -> None:
        logger.error(f"Task {self.task_id} export failed: {self.error}")


@dataclass
class ProjectDumpSuccess(DumpResult):
    project_id: str
    queue_key: str
    task_results: List[TaskDumpResult] = field(default_factory=list)

    def log(self) -> None:
        if not self.task_errors:
            logger.info(f"Project {self.project_id} ({self.queue_key}): Successfully exported all {len(self.task_results)} tasks")
        else:
            logger.warning(
                f"Project {self.project_id} ({self.queue_key}): "
                f"Exported {len(self.task_results) - len(self.task_errors)}/{len(self.task_results)} tasks"
            )
        for task_result in self.task_results:
            task_result.log()

    @property
    def task_errors(self) -> List[TaskDumpError]:
        return [r for r in self.task_results if isinstance(r, TaskDumpError)]


@dataclass
class ProjectDumpError(DumpResult):
    project_id: str
    error: Exception

    def log(self) -> None:
        logger.error(f"Project {self.project_id} dump failed: {self.error}")


def build_summary(
    results: Iterable[ProjectDumpResult],
    failed_items: Iterable[Tuple[str, str, str]],
    unknown_users: Iterable[str],
    failed_reference_data: Iterable[str] = (),
) -> List[str]:
    """One line per export error and per user who is not a member of the workspace."""
    lines = [f"Workspace: {error}" for error in failed_reference_data]
    for result in results:
        if isinstance(result, ProjectDumpError):
            lines.append(f"Project {result.project_id}: {result.error}")
        else:
            for error in result.task_errors:
                lines.append(f"Project {result.project_id}, task {error.task_id}: {error.error}")
    for issue_key, item, error in sorted(failed_items):
        lines.append(f"{issue_key}, {item}: {error}")
    for user_id in sorted(unknown_users):
        lines.append(f"User {user_id} is not a member of the workspace: add {user_id}:<e-mail> to configs/users.cfg")
    return lines
