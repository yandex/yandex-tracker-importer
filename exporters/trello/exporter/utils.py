import yaml
import re
from typing import Any, Optional
from urllib.parse import unquote

from .transliterate import translit


def dump_to_yaml(prepared_data: Any, file_path: str) -> None:
    with open(file_path, "w", encoding='utf-8') as f:
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
    return camel_case_key


def prepare_attachment_filename(original_name: str, attachment_id: str) -> str:
    ENCODED_PATTERN = r"%[0-9A-Fa-f]{2}"
    if re.search(ENCODED_PATTERN, original_name):
        original_name = unquote(original_name)
    if "." not in original_name:
        return f"{original_name}_{attachment_id}"

    name, ext = original_name.rsplit(".", 1)
    return f"{name}_{attachment_id}.{ext}"


def prepare_attachment_links(text: str) -> str:
    if not text:
        return text

    ATTACHMENT_LINK_PATTERN = r"\!?\[([^\]]+)\]\([^\)]*/attachments/(\w+)[^\)]*\)"
    for match in re.finditer(ATTACHMENT_LINK_PATTERN, text):
        new_name = prepare_attachment_filename(match.group(1), match.group(2))
        prefix = "!" if match.group(0).startswith("!") else ""
        text = text.replace(match.group(0), f"{prefix}[{new_name}]()")
    return text
