import os
from typing import Dict, Optional

STATE_DIR = "state"


class ImportState:
    """Файл `state/<имя>` рабочего каталога: исходный идентификатор объекта и созданный.

    Нужен там, где созданный объект нельзя найти через API: у сохранённых фильтров нет
    перечисления, у дашбордов на части установок нет поиска. Строка дописывается сразу после
    создания объекта."""

    def __init__(self, state_path: str, file_name: str) -> None:
        self._state_file = os.path.join(state_path, STATE_DIR, file_name)
        self._created_ids = self._load()

    def get(self, source_id: str) -> Optional[str]:
        return self._created_ids.get(source_id)

    def save(self, source_id: str, created_id: str) -> None:
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        with open(self._state_file, "a") as state:
            state.write(f"{source_id}:{created_id}\n")
            state.flush()
            os.fsync(state.fileno())

        self._created_ids[source_id] = created_id

    def _load(self) -> Dict[str, str]:
        if not os.path.exists(self._state_file):
            return {}

        created_ids = {}
        with open(self._state_file) as state:
            for line in state:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # по последнему двоеточию: в исходном идентификаторе они бывают, в созданном нет
                source_id, _, created_id = line.rpartition(":")
                created_ids[source_id.strip()] = created_id.strip()

        return created_ids
