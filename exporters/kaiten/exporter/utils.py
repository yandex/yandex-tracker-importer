import re
import yaml
from typing import Any, Optional
from urllib.parse import unquote

from .transliterate import translit


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


def title_to_key(title: Optional[str]) -> Optional[str]:
    if title is None:
        return None

    letters_only = "".join(c for c in title if c.isalpha() or c.isspace())

    if not letters_only.strip():
        return None
    translitted_title = translit(letters_only)
    words = translitted_title.split()

    if len(words) == 0:
        return None

    camel_case_key = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    # translit only covers Cyrillic; drop any other non-ASCII letters (accented
    # Latin, Greek, …) so the key stays valid for Tracker.
    ascii_key = camel_case_key.encode("ascii", "ignore").decode("ascii")
    return ascii_key or None


def to_tracker_datetime(value: Any) -> Optional[str]:
    """Normalize a Kaiten timestamp to the format the importer expects
    (``%Y-%m-%dT%H:%M:%S.%f%z``, e.g. ``2025-01-10T10:00:00.000+0000``).

    Kaiten returns ISO strings ending in ``Z`` (``2021-09-07T08:45:34.708Z``) and
    date-only values (``2026-06-02``); both fail the importer's strict ``strptime``,
    so we coerce timezone/fraction and expand bare dates to midnight UTC.
    """
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


def to_tracker_date(value: Any) -> Optional[str]:
    """Kaiten dates/datetimes -> ``YYYY-MM-DD`` (Tracker ``deadline``/``start``/``end``)."""
    if not value:
        return None
    return str(value)[:10]


def prepare_attachment_filename(original_name: str, attachment_id: str) -> str:
    ENCODED_PATTERN = r"%[0-9A-Fa-f]{2}"
    if re.search(ENCODED_PATTERN, original_name):
        original_name = unquote(original_name)
    if "." not in original_name:
        return f"{original_name}_{attachment_id}"

    name, ext = original_name.rsplit(".", 1)
    return f"{name}_{attachment_id}.{ext}"
