import logging
import os
import stat
import tempfile
import yaml
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, TextIO


logger = logging.getLogger(__name__)


def str_representer(dumper, data):
    """Custom representer for strings to use literal block style for multiline strings."""
    if '\n' in data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


yaml.add_representer(str, str_representer)


def dump_to_file(data: Any, dump_dir: str, filename: str) -> None:
    """Write YAML in UTF-8; the file is replaced only after the write completes."""
    os.makedirs(dump_dir, exist_ok=True)
    with atomic_write(os.path.join(dump_dir, filename)) as f:
        yaml.dump(data, f, allow_unicode=True)


def _file_mode(path: str) -> int:
    """Mode of an existing file, else the default mode of a new file."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


@contextmanager
def atomic_write(path: str, newline: Optional[str] = None, encoding: str = "utf-8") -> Iterator[TextIO]:
    """Text file written to a temporary file and moved to ``path`` on success, with the mode of ``path``."""
    mode = _file_mode(path)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            yield f
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def parse_datetime(value: str) -> datetime:
    """ISO 8601 date and time; a value without an offset is taken as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        logger.warning(f"Date {value} has no time zone offset, UTC is used")
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_tracker_datetime(value: Optional[str]) -> Optional[str]:
    """ISO 8601 date and time from Bitrix24 in the Tracker import format ``YYYY-MM-DDThh:mm:ss.sss±hhmm``."""
    if not value:
        return None
    dt = parse_datetime(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + dt.strftime("%z")


def to_tracker_day_start(value: Optional[str]) -> Optional[str]:
    """Midnight of the date of an ISO 8601 value, in its own offset, in the Tracker import format."""
    if not value:
        return None
    dt = datetime.fromisoformat(value).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_tracker_datetime(dt.isoformat())


def clean_empty(data: Any) -> Any:
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            cleaned_value = clean_empty(value)
            if cleaned_value not in (None, "", [], {}):
                result[key] = cleaned_value
        return result
    elif isinstance(data, list):
        result = []
        for item in data:
            cleaned_item = clean_empty(item)
            if cleaned_item not in (None, "", [], {}):
                result.append(cleaned_item)
        return result
    else:
        return data
