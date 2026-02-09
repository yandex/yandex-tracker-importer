import logging
from dataclasses import dataclass
from typing import List, Union
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)


CardDumpResult = Union['CardDumpSuccess', 'CardDumpError']
BoardDumpResult = Union['BoardDumpSuccess', 'BoardDumpError']


class DumpResult(ABC):

    @abstractmethod
    def log(self) -> None:
        pass


@dataclass
class CardDumpSuccess(DumpResult):
    card_id: str
    issue_key: str

    def log(self) -> None:
        logger.debug(f"Card {self.card_id} exported as {self.issue_key}")


@dataclass
class CardDumpError(DumpResult):
    card_id: str
    error: Exception

    def log(self) -> None:
        logger.error(f"Card {self.card_id} export failed: {self.error}")


@dataclass
class BoardDumpSuccess(DumpResult):
    board_id: str
    queue_key: str
    card_results: List[CardDumpResult] = None

    def __post_init__(self) -> None:
        if self.card_results is None:
            self.card_results = []

    def log(self) -> None:
        if not self.has_errors:
            logger.info(
                f"Board {self.board_id} ({self.queue_key}): "
                f"Successfully exported all {self.total_cards} cards"
            )
        else:
            logger.warning(
                f"Board {self.board_id} ({self.queue_key}): "
                f"Exported {self.successful_cards}/{self.total_cards} cards"
            )

        for card_result in self.card_results:
            card_result.log()

    @property
    def card_errors(self) -> List[CardDumpError]:
        return [r for r in self.card_results if isinstance(r, CardDumpError)]

    @property
    def card_successes(self) -> List[CardDumpSuccess]:
        return [r for r in self.card_results if isinstance(r, CardDumpSuccess)]

    @property
    def total_cards(self) -> int:
        return len(self.card_results)

    @property
    def failed_cards(self) -> int:
        return len(self.card_errors)

    @property
    def successful_cards(self) -> int:
        return len(self.card_successes)

    @property
    def has_errors(self) -> bool:
        return bool(self.card_errors)


@dataclass
class BoardDumpError(DumpResult):
    board_id: str
    error: Exception

    def log(self) -> None:
        logger.error(f"Board {self.board_id} dump failed: {self.error}")
