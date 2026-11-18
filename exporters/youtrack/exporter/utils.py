import logging
import re
import yaml
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, unquote

from .transliterate import translit

logger = logging.getLogger(__name__)

TRACKER_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"

# characters kept as is in a link url; "(", ")" and spaces are percent-encoded
URL_SAFE_CHARS = ":/?#@!$&'*+,;=%-._~"


def encode_url(url: str) -> str:
    """Url of a markdown link: spaces and parentheses are percent-encoded."""
    return quote(url, safe=URL_SAFE_CHARS)


def str_representer(dumper, data):
    """Multiline strings are YAML literal blocks."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_representer)


def dump_to_yaml(prepared_data: Any, file_path: str) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(prepared_data, f, allow_unicode=True)


# bytes of a name in a directory or a file name: a file name is at most 255 bytes with the id and the extension
NAME_PART_LIMIT = 150
EXTENSION_LIMIT = 20


def truncate_bytes(text: str, limit: int) -> str:
    """The longest prefix of `text` within `limit` bytes of UTF-8."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


# Tracker keys of statuses, issue types and fields are at most 30 characters long, names are at most 50
KEY_MAX_LENGTH = 30
NAME_MAX_LENGTH = 50


def title_to_key(title: Optional[str]) -> Optional[str]:
    if title is None:
        return None

    # punctuation separates words
    letters_and_digits = "".join(c if c.isalnum() else " " for c in title)
    words = translit(letters_and_digits).split()
    if not words:
        return None

    camel_case_key = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    # translit only covers Cyrillic; drop any other non-ASCII letters (accented
    # Latin, Greek, …) so the key stays valid for Tracker.
    ascii_key = camel_case_key.encode("ascii", "ignore").decode("ascii")
    if not ascii_key:
        return None
    # a Tracker key starts with a letter
    if ascii_key[0].isdigit():
        ascii_key = "k" + ascii_key
    return ascii_key[:KEY_MAX_LENGTH]


def normalize_name(name: str) -> str:
    """Name form for clash checks: case and extra spaces are ignored."""
    return " ".join(name.split()).lower()


class KeyAllocator:
    """Unique Tracker keys, compared ignoring case: a clash of keys gets a numeric suffix."""

    def __init__(self, reserved: Iterable[str] = ()):
        self._key_by_name: Dict[str, str] = {}
        self._taken = {key.lower() for key in reserved}

    def key(self, name: str, fallback: str) -> str:
        """One key per name."""
        normalized = normalize_name(name)
        if normalized not in self._key_by_name:
            self._key_by_name[normalized] = self.new_key(name, fallback)
        return self._key_by_name[normalized]

    def new_key(self, name: str, fallback: str) -> str:
        base = title_to_key(name) or fallback
        key, suffix = base, 2
        while key.lower() in self._taken:
            key = f"{base[:KEY_MAX_LENGTH - len(str(suffix))]}{suffix}"
            suffix += 1
        self._taken.add(key.lower())
        return key


class NameAllocator:
    """Unique Tracker names within the length limit: a taken name gets a « (N)» suffix."""

    def __init__(self, max_length: int = NAME_MAX_LENGTH, reserved: Iterable[str] = ()):
        self._max_length = max_length
        self._name_by_source: Dict[str, str] = {}
        self._taken = {normalize_name(name) for name in reserved}

    def name(self, source: str) -> str:
        """One name per source name."""
        normalized = normalize_name(source)
        if normalized not in self._name_by_source:
            self._name_by_source[normalized] = self.new_name(source)
        return self._name_by_source[normalized]

    def new_name(self, source: str) -> str:
        name, number = source[:self._max_length].rstrip(), 2
        while normalize_name(name) in self._taken:
            suffix = f" ({number})"
            name = source[:self._max_length - len(suffix)].rstrip() + suffix
            number += 1
        self._taken.add(normalize_name(name))
        return name


def ms_to_tracker_datetime(value: Any) -> Optional[str]:
    """YouTrack timestamp in milliseconds -> ``YYYY-MM-DDThh:mm:ss.sss+0000``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        logger.warning(f"Unrecognized timestamp {value!r} is skipped")
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}+0000"


def ms_to_date(value: Any) -> Optional[str]:
    """YouTrack timestamp of a date -> ``YYYY-MM-DD`` of its day in UTC: YouTrack stores a date as a moment of its
    day in UTC."""
    date = ms_to_tracker_datetime(value)
    return date[:10] if date else None


def latest_tracker_datetime(values: Iterable[Optional[str]]) -> Optional[str]:
    """The latest of dates in the importer format; empty values are skipped."""
    dates = [value for value in values if value]
    return max(dates, key=_moment) if dates else None


def _moment(value: str) -> datetime:
    return datetime.strptime(value, TRACKER_DATETIME_FORMAT)


def prepare_attachment_filename(original_name: str, attachment_id: str) -> str:
    """File name in the `attachments` directory: path separators and square brackets of the name are replaced,
    the file id is appended."""
    ENCODED_PATTERN = r"%[0-9A-Fa-f]{2}"
    if re.search(ENCODED_PATTERN, original_name):
        original_name = unquote(original_name)
    # a link of the importer `[name]()` ends at the first closing bracket
    original_name = re.sub(r"[\\/\[\]]", "_", original_name).lstrip(".") or "file"
    if "." not in original_name:
        return f"{truncate_bytes(original_name, NAME_PART_LIMIT)}_{attachment_id}"

    name, ext = original_name.rsplit(".", 1)
    return f"{truncate_bytes(name, NAME_PART_LIMIT)}_{attachment_id}.{truncate_bytes(ext, EXTENSION_LIMIT)}"
