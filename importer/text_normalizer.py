import re
from typing import Callable, Dict

from yandex_tracker_client.objects import Resource

from .mapper import EntityMapper


class TextNormalizer:
    _IMAGE_EXTENSIONS = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".tif",
        ".tiff",
        ".avif",
        ".jfif",
        ".pjpeg",
        ".pjp",
        ".webp"
    )

    def __init__(self, mapper: EntityMapper, created_attachments: Dict[str, Resource], url_generator: Callable[[str], str]):
        self._mapper = mapper
        self._created_attachments = created_attachments
        self._url_generator = url_generator

    def normalize_text(self, text: str) -> str:
        if not text:
            return text

        new_text = re.sub(
            r"(?<!\w)@([@\w\-\.]+)(?=\W|$)",
            lambda match: f"@{self._mapper.get_user(match.group(1))}",
            text,
            flags=re.MULTILINE
        )
        return self.normalize_attachment_links(new_text)

    def normalize_attachment_links(self, text: str) -> str:
        if not text:
            return text

        def replace_attachment_link(match: re.Match) -> str:
            filename = match.group(1)
            attachment_obj = self._created_attachments.get(filename)

            if not attachment_obj:
                return match.group(0)

            url = self._url_generator(attachment_obj.id)
            prefix = "!" if self._is_image(filename) else ""
            return f"{prefix}[{attachment_obj.name}]({url})"

        return re.sub(r"\!?\[([^\]]+)\]\(([^\)]*)\)", replace_attachment_link, text, flags=re.MULTILINE)

    def _is_image(self, filename: str) -> bool:
        return filename.lower().endswith(self._IMAGE_EXTENSIONS)
