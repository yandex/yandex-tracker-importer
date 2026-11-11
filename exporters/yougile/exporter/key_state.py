import os
from typing import Dict, List, Optional

STATE_DIR = "state"
KEYS_FILE = "yougile_keys.cfg"


class KeyState:
    """File `state/yougile_keys.cfg` of the working directory: the YouGile task ID and its key in the export.

    A repeated export gives a task the key of the earlier export: the import finds the issue by the key and does not
    create it again. A line is appended when a key is given or changed: the lines of a task give the history of its
    keys, the last one is its key. Without ``working_path`` the keys are kept in memory only."""

    def __init__(self, working_path: Optional[str] = None) -> None:
        self._state_file = os.path.join(working_path, STATE_DIR, KEYS_FILE) if working_path else None
        # task ID -> keys given to the task, the last one is its key
        self.history: Dict[str, List[str]] = self._load()

    @property
    def keys(self) -> Dict[str, str]:
        return {task_id: keys[-1] for task_id, keys in self.history.items()}

    @property
    def path(self) -> str:
        return os.path.join(STATE_DIR, KEYS_FILE)

    @property
    def exists(self) -> bool:
        return self._state_file is None or os.path.exists(self._state_file)

    def save(self, keys: Dict[str, str]) -> None:
        current = self.keys
        changed = {task_id: key for task_id, key in keys.items() if current.get(task_id) != key}
        if self._state_file:
            # an export without tasks writes the file too: a repeated export requires it
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "a", encoding="utf-8") as state:
                for task_id, key in changed.items():
                    state.write(f"{task_id}:{key}\n")
                state.flush()
                os.fsync(state.fileno())
        for task_id, key in changed.items():
            self.history.setdefault(task_id, []).append(key)

    def _load(self) -> Dict[str, List[str]]:
        if not self._state_file or not os.path.exists(self._state_file):
            return {}

        history = {}
        with open(self._state_file, encoding="utf-8") as state:
            for line in state:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                task_id, _, key = line.rpartition(":")
                history.setdefault(task_id.strip(), []).append(key.strip())
        return history
