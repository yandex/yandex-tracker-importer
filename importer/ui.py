import os
from dataclasses import dataclass
from tqdm import tqdm


@dataclass
class ImportStatistics:
    queues_imported: int = 0
    queues_failed: int = 0

    issues_imported: int = 0
    issues_failed: int = 0

    projects_imported: int = 0
    projects_failed: int = 0

    goals_imported: int = 0
    goals_failed: int = 0

    portfolios_imported: int = 0
    portfolios_failed: int = 0

    def print_statistics(self) -> None:
        print("\n" + "="*60)
        print(" " * 20 + "IMPORT STATISTICS")
        print("="*60)

        print("\n📁 QUEUES:")
        total_queues = self.queues_imported + self.queues_failed
        print(f"   Imported: {self.queues_imported}/{total_queues}")

        print("\n📋 ISSUES:")
        total_issues = self.issues_imported + self.issues_failed
        print(f"   Imported: {self.issues_imported}/{total_issues}")

        total_projects = self.projects_imported + self.projects_failed
        total_goals = self.goals_imported + self.goals_failed
        total_portfolios = self.portfolios_imported + self.portfolios_failed

        if total_projects + total_goals + total_portfolios > 0:
            print("\n🏢 ENTITIES:")
            if total_projects > 0:
                print(f"   Projects: {self.projects_imported}/{total_projects}")

            if total_goals > 0:
                print(f"   Goals: {self.goals_imported}/{total_goals}")

            if total_portfolios > 0:
                print(f"   Portfolios: {self.portfolios_imported}/{total_portfolios}")

        print("\n" + "="*60)
        entities_failed = self.projects_failed + self.goals_failed + self.portfolios_failed
        if self.queues_failed == 0 and self.issues_failed == 0 and entities_failed == 0:
            print(" " * 15 + "✅ IMPORT COMPLETED SUCCESSFULLY")
            print("="*60 + "\n")


class ImportProgressTracker:
    def __init__(self, data_path: str):
        self.statistics = ImportStatistics()

        self.entity_pbar = None
        self.queue_pbar = None
        self.issue_pbar = None
        self._init_progress_bars(data_path)

    def _count_items(self, path: str) -> int:
        if not os.path.exists(path):
            return 0

        total = 0
        with os.scandir(path) as it:
            total += sum(
                1 if entry.is_dir(follow_symlinks=False) else 0
                for entry in it
            )
        return total

    def _init_progress_bars(self, data_path: str):
        entity_paths = [
            os.path.join(data_path, "projects"),
            os.path.join(data_path, "goals"),
            os.path.join(data_path, "portfolios")
        ]
        total_entities = sum(self._count_items(path) for path in entity_paths)

        queues_path = os.path.join(data_path, "queues")

        total_queues, total_issues = 0, 0
        if os.path.exists(queues_path):
            for queue_dir in os.listdir(queues_path):
                queue_dir_path = os.path.join(queues_path, queue_dir)
                if os.path.isdir(queue_dir_path):
                    total_queues += 1
                    total_issues += self._count_items(queue_dir_path)

        if total_entities > 0:
            self.entity_pbar = tqdm(
                total=total_entities,
                ncols=100,
                desc="Importing entities",
                unit="entity",
                position=0,
                leave=False
            )

        self.queue_pbar = tqdm(
            total=total_queues,
            ncols=100,
            desc="Importing queues",
            unit="queue",
            position=1,
            leave=False
        )

        self.issue_pbar = tqdm(
            total=total_issues,
            ncols=100,
            desc="Importing issues",
            unit="issue",
            position=2,
            leave=False
        )

    def _update_entity_progress(self) -> None:
        total_success = self.statistics.projects_imported + self.statistics.goals_imported + self.statistics.portfolios_imported
        total_failed = self.statistics.projects_failed + self.statistics.goals_failed + self.statistics.portfolios_failed
        if self.entity_pbar is not None:
            self.entity_pbar.update(1)

            self.entity_pbar.set_postfix({
                '✅': total_success,
                '❌': total_failed
            })

    def report_project(self, success: bool) -> None:
        self.statistics.projects_imported += success
        self.statistics.projects_failed += not success
        self._update_entity_progress()

    def report_goal(self, success: bool) -> None:
        self.statistics.goals_imported += success
        self.statistics.goals_failed += not success
        self._update_entity_progress()

    def report_portfolio(self, success: bool) -> None:
        self.statistics.portfolios_imported += success
        self.statistics.portfolios_failed += not success
        self._update_entity_progress()

    def report_queue(self, success: bool) -> None:
        self.queue_pbar.update(1)

        self.statistics.queues_imported += success
        self.statistics.queues_failed += not success

        self.queue_pbar.set_postfix({
            '✅': self.statistics.queues_imported,
            '❌': self.statistics.queues_failed
        })

    def report_issue(self, success: bool) -> None:
        self.issue_pbar.update(1)

        self.statistics.issues_imported += success
        self.statistics.issues_failed += not success

        self.issue_pbar.set_postfix({
            '✅': self.statistics.issues_imported,
            '❌': self.statistics.issues_failed
        })

    def close_all(self) -> None:
        if self.entity_pbar is not None:
            self.entity_pbar.close()

        self.queue_pbar.close()
        self.issue_pbar.close()
