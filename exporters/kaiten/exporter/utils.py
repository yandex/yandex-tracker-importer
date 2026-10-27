import logging
import re
import yaml
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from .transliterate import translit

logger = logging.getLogger(__name__)

TRACKER_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def str_representer(dumper, data):
    """Use literal block style ('|') for multiline strings so markdown descriptions
    and comments don't break the produced YAML."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_representer)


def dump_to_yaml(prepared_data: Any, file_path: str) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(prepared_data, f, allow_unicode=True)


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

    def __init__(self, max_length: int = NAME_MAX_LENGTH):
        self._max_length = max_length
        self._name_by_source: Dict[str, str] = {}
        self._taken: set = set()

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


def issue_types_by_card_type_id(card_types: List[Dict[str, Any]]) -> Dict[Any, Tuple[str, str]]:
    """Issue type key and name of every named card type by its id. Card types with one name share
    an issue type."""
    keys, names = KeyAllocator(), NameAllocator()
    result = {}
    for card_type in sorted(card_types, key=lambda card_type: card_type["id"]):
        if (card_type.get("name") or "").strip():
            name = names.name(card_type["name"].strip())
            result[card_type["id"]] = (keys.key(name, f"type{card_type['id']}"), name)
    return result


def issue_type_keys(card_types: List[Dict[str, Any]]) -> Dict[Any, str]:
    return {type_id: key for type_id, (key, _) in issue_types_by_card_type_id(card_types).items()}


def local_fields_by_property_id(custom_properties: List[Dict[str, Any]]) -> Dict[Any, Tuple[str, str]]:
    """Local field key and name of every named custom property by its id. Names of local fields
    are unique within a queue: a taken name gets « (2)», « (3)» in the order of property ids."""
    keys, names = KeyAllocator(), NameAllocator()
    result = {}
    for prop in sorted(custom_properties, key=lambda prop: prop["id"]):
        if (prop.get("name") or "").strip():
            name = names.new_name(prop["name"].strip())
            result[prop["id"]] = (keys.new_key(name, f"property{prop['id']}"), name)
    return result


def to_tracker_datetime(value: Any) -> Optional[str]:
    """Kaiten timestamp or date -> ``YYYY-MM-DDThh:mm:ss.sss±hhmm``; a date is midnight UTC."""
    if not value:
        return None
    s = str(value).strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s}T00:00:00.000+0000"

    s = re.sub(r"Z$", "+0000", s)
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)

    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{4})?$", s)
    if match:
        base = match.group(1)
        digits = (match.group(2)[1:] if match.group(2) else "000")[:3].ljust(3, "0")
        tz = match.group(3) or "+0000"
        return f"{base}.{digits}{tz}"
    return s


def latest_tracker_datetime(values: Iterable[Any]) -> Optional[str]:
    """The latest of Kaiten timestamps in the importer format; empty and unrecognized values are skipped."""
    moments = {}
    for value in values:
        date = to_tracker_datetime(value)
        if not date:
            continue
        try:
            moments[date] = datetime.strptime(date, TRACKER_DATETIME_FORMAT)
        except ValueError:
            logger.warning(f"Unrecognized date {value} is skipped")
    return max(moments, key=moments.get) if moments else None


def to_tracker_date(value: Any) -> Optional[str]:
    """Kaiten date or timestamp -> ``YYYY-MM-DD`` as written, without time zone conversion."""
    if not value:
        return None
    return str(value)[:10]


def to_local_date(value: Any, timezone: str, midnight_utc_is_date: bool = False) -> Optional[str]:
    """Kaiten timestamp -> ``YYYY-MM-DD`` of its day in `timezone`. With `midnight_utc_is_date` a
    timestamp at midnight UTC is a date without time and keeps its day."""
    if not value:
        return None
    try:
        moment = datetime.strptime(to_tracker_datetime(value), TRACKER_DATETIME_FORMAT)
    except ValueError:
        logger.warning(f"Unrecognized date {value!r} is taken as written")
        return to_tracker_date(value)
    if midnight_utc_is_date and moment.utcoffset() == timedelta(0) and moment.time() == datetime.min.time():
        return moment.date().isoformat()
    return moment.astimezone(ZoneInfo(timezone)).date().isoformat()


def prepare_attachment_filename(original_name: str, attachment_id: str) -> str:
    ENCODED_PATTERN = r"%[0-9A-Fa-f]{2}"
    if re.search(ENCODED_PATTERN, original_name):
        original_name = unquote(original_name)
    if "." not in original_name:
        return f"{original_name}_{attachment_id}"

    name, ext = original_name.rsplit(".", 1)
    return f"{name}_{attachment_id}.{ext}"


def sort_by_created(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cards in the order of their issue numbers: by creation, cards without a date last."""
    return sorted(cards, key=lambda card: (not card.get("created"), card.get("created") or ""))
