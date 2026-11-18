import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple, Union

logger = logging.getLogger(__name__)


IssueDumpResult = Union['IssueDumpSuccess', 'IssueDumpError']
ProjectDumpResult = Union['ProjectDumpSuccess', 'ProjectDumpError']


class DumpResult(ABC):

    @abstractmethod
    def log(self) -> None:
        pass


@dataclass
class IssueDumpSuccess(DumpResult):
    issue_id: str
    issue_key: str

    def log(self) -> None:
        logger.debug(f"Issue {self.issue_id} exported as {self.issue_key}")


@dataclass
class IssueDumpError(DumpResult):
    issue_id: str
    error: Exception

    def log(self) -> None:
        logger.error(f"Issue {self.issue_id} export failed: {self.error}")


@dataclass
class ProjectDumpSuccess(DumpResult):
    project_id: str
    queue_key: str
    issue_results: List[IssueDumpResult] = field(default_factory=list)

    def log(self) -> None:
        if not self.issue_errors:
            logger.info(f"Project {self.project_id} ({self.queue_key}): Successfully exported all {len(self.issue_results)} issues")
        else:
            logger.warning(
                f"Project {self.project_id} ({self.queue_key}): "
                f"Exported {len(self.issue_results) - len(self.issue_errors)}/{len(self.issue_results)} issues"
            )
        for issue_result in self.issue_results:
            issue_result.log()

    @property
    def issue_errors(self) -> List[IssueDumpError]:
        return [r for r in self.issue_results if isinstance(r, IssueDumpError)]


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
    """One line per export error and per user without an e-mail."""
    lines = [f"YouTrack: {error}" for error in failed_reference_data]
    for result in results:
        if isinstance(result, ProjectDumpError):
            lines.append(f"Project {result.project_id}: {result.error}")
        else:
            for error in result.issue_errors:
                lines.append(f"Project {result.project_id}, issue {error.issue_id}: {error.error}")
    for issue_key, item, error in sorted(failed_items):
        lines.append(f"{issue_key}, {item}: {error}")
    for login in sorted(unknown_users):
        lines.append(f"User {login} has no e-mail: add {login}:<e-mail> to configs/users.cfg")
    return lines
