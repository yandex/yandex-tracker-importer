import logging
import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Set, Tuple


log = logging.getLogger(__name__)

# the import splits a key by its first hyphen
TASK_KEY_PATTERN = re.compile(r"^(?P<prefix>[^-\s]+)-(?P<number>\d+)$")


def split_task_key(key: Optional[str]) -> Optional[Tuple[str, int]]:
    match = TASK_KEY_PATTERN.match(key or "")
    if not match:
        return None
    return match.group("prefix"), int(match.group("number"))


def task_parents(tasks: Iterable[Dict]) -> Dict[str, List[str]]:
    """Return IDs of parent tasks of each subtask; a task can be a subtask of several tasks."""
    parents = {}
    for task in tasks:
        for subtask_id in task.get("subtasks") or []:
            parents.setdefault(subtask_id, []).append(task["id"])
    return parents


class Placement(NamedTuple):
    column_id: str
    # the parent the subtask is placed by, None for a task in a column
    parent_id: Optional[str] = None


def place_tasks(
    tasks: Iterable[Dict], exported_columns: Iterable[str], preferred: Optional[Dict[str, Set[str]]] = None
) -> Dict[str, Placement]:
    """Return the column of each task in ``exported_columns``.

    A task without a column (a subtask) takes the column of its first parent, in the order of the task list, whose
    column or the column of whose own first parent and so on is exported. A subtask with ``preferred`` columns (of the
    queue of its key from an earlier export, else of the project of its YouGile number) takes the first such parent
    placed in them, if any.
    """
    tasks_by_id = {task["id"]: task for task in tasks}
    exported_columns = set(exported_columns)
    parents = task_parents(tasks_by_id.values())
    preferred = preferred or {}
    # placements of tasks found without passing a task of the current path: a task shared by several subtasks is
    # placed once
    found = {}

    def known(task_id: str) -> Optional[Tuple[Optional[Placement], bool]]:
        """Return the placement of a task in a column or placed before, ``None`` if parents are to be walked."""
        if task_id in found:
            return found[task_id], False
        column_id = tasks_by_id[task_id].get("columnId")
        if column_id:
            return (Placement(column_id) if column_id in exported_columns else None), False
        return None

    def placement_of(task_id: str) -> Tuple[Optional[Placement], bool]:
        """Return the placement and whether a task of the current path was skipped to find it.

        Parents are walked with an explicit stack: a chain of subtasks is not limited by the recursion depth."""
        result = known(task_id)
        if result is not None:
            return result
        frames = [_Frame(task_id, parents.get(task_id, []), preferred.get(task_id))]
        on_path = {task_id}
        while True:
            frame = frames[-1]
            if result is not None:
                # the result of the parent walked last
                parent_placement, parent_cut = result
                result = None
                frame.cut = frame.cut or parent_cut
                if parent_placement:
                    placement = Placement(parent_placement.column_id, frame.parents[frame.next - 1])
                    if not frame.wanted or placement.column_id in frame.wanted:
                        frame.first = placement
                        frame.next = len(frame.parents)
                    else:
                        frame.first = frame.first or placement
            if frame.next < len(frame.parents):
                parent_id = frame.parents[frame.next]
                frame.next += 1
                if parent_id not in tasks_by_id:
                    continue
                if parent_id in on_path:
                    frame.cut = True
                    continue
                result = known(parent_id)
                if result is None:
                    frames.append(_Frame(parent_id, parents.get(parent_id, []), preferred.get(parent_id)))
                    on_path.add(parent_id)
                continue
            frames.pop()
            on_path.discard(frame.task_id)
            if not frame.cut:
                found[frame.task_id] = frame.first
            result = (frame.first, frame.cut)
            if not frames:
                return result

    placements = {}
    for task_id, task in tasks_by_id.items():
        placement, _ = placement_of(task_id)
        if placement:
            placements[task_id] = placement
        elif not task.get("columnId") and not parents.get(task_id):
            # the list covers the whole company: such a task may belong to a project that is not exported
            log.info(f"Task {task.get('idTaskProject')}, id {task_id}, has no column and no parent task in the export")

    return placements


class _Frame:
    """A task whose parents are being walked."""

    __slots__ = ("task_id", "parents", "wanted", "next", "first", "cut")

    def __init__(self, task_id: str, parents: List[str], wanted: Optional[Set[str]]) -> None:
        self.task_id = task_id
        self.parents = parents
        self.wanted = wanted or set()
        # index of the next parent to walk
        self.next = 0
        # the placement found: by a preferred parent, else by the first placed parent
        self.first = None
        # a task of the current path was skipped
        self.cut = False


class Renumbered(NamedTuple):
    task: Dict
    key: str
    reason: str


class Allocation(NamedTuple):
    # task ID -> key
    keys: Dict[str, str]
    renumbered: List[Renumbered]
    # tasks with a key taken by an earlier task: they are not exported
    duplicates: List[Dict]


def has_project_key(task: Dict) -> bool:
    """A key equal to the company-wide ``idTaskCommon`` is not numbered within a project."""
    key = task.get("idTaskProject")
    return split_task_key(key) is not None and key != task.get("idTaskCommon")


def allocate_keys(
    tasks: List[Dict], prefix: str, reserved: Iterable[int] = (), history: Optional[Dict[str, List[str]]] = None
) -> Allocation:
    """Return a key ``<prefix>-<number>`` for each task of a queue.

    ``history`` gives the keys of earlier exports by task ID, the last one is the current key. A task keeps its latest
    key of the queue from an earlier export. Otherwise a task keeps its YouGile number if its key has the queue prefix,
    in any letter case, and no earlier export gave it to another task. Other tasks (moved from another project, placed
    by a parent task of another project, without a project key or with a number given to another task) get a number
    after the largest taken, ``reserved`` or given number, in order of creation; ``reserved`` are numbers of the prefix
    used by any task of the company. A task whose YouGile number is taken by an earlier task is not exported.
    """
    history = history or {}
    # number -> IDs of tasks an earlier export gave a key of the prefix with the number
    given = {}
    for task_id, task_keys in history.items():
        for key in task_keys:
            parsed = split_task_key(key)
            if parsed and parsed[0].casefold() == prefix.casefold():
                given.setdefault(parsed[1], set()).add(task_id)

    keys = {}
    # numbers kept by tasks as their YouGile number
    own = set()
    renumbered, duplicates, pending = [], [], []
    ordered = sorted(tasks, key=lambda task: (task.get("timestamp") or 0, task["id"]))
    for task in ordered:
        earlier = [
            parsed[1]
            for parsed in map(split_task_key, history.get(task["id"], []))
            if parsed and parsed[0].casefold() == prefix.casefold()
        ]
        if not earlier:
            continue
        number = earlier[-1]
        keys[task["id"]] = f"{prefix}-{number}"
        if _is_own_number(task, prefix, number):
            own.add(number)
        else:
            renumbered.append(Renumbered(task, keys[task["id"]], _reason(task, prefix)))
    for task in ordered:
        if task["id"] in keys:
            continue
        parsed = split_task_key(task.get("idTaskProject"))
        if not (has_project_key(task) and parsed[0].casefold() == prefix.casefold()):
            pending.append((task, _reason(task, prefix)))
        elif parsed[1] in own:
            duplicates.append(task)
        elif given.get(parsed[1], {task["id"]}) != {task["id"]}:
            pending.append((task, "number given to another task by an earlier export"))
        else:
            own.add(parsed[1])
            keys[task["id"]] = f"{prefix}-{parsed[1]}"

    next_number = max(own | set(given) | set(reserved), default=0)
    for task, reason in pending:
        next_number += 1
        keys[task["id"]] = f"{prefix}-{next_number}"
        renumbered.append(Renumbered(task, keys[task["id"]], reason))

    return Allocation(keys, renumbered, duplicates)


def _is_own_number(task: Dict, prefix: str, number: int) -> bool:
    parsed = split_task_key(task.get("idTaskProject"))
    return has_project_key(task) and parsed[0].casefold() == prefix.casefold() and parsed[1] == number


def _reason(task: Dict, prefix: str) -> str:
    if not has_project_key(task):
        return "no project key"
    if split_task_key(task["idTaskProject"])[0].casefold() == prefix.casefold():
        return "number given to another task by an earlier export"
    return "key of another project"
