import os
from typing import Optional

from .exceptions import ConfigFileNotFoundException


def get_config_file(dump_path: str, file_name: str) -> Optional[str]:
    path = os.path.expanduser(os.path.join(dump_path, "configs", f"{file_name}.cfg"))
    return path if os.path.exists(path) else None


def get_config_content(
    dump_path: str, file_name: str, validate_file_exist: bool = True, strip: bool = True
) -> Optional[str]:
    config_path = get_config_file(dump_path, file_name)
    if config_path:
        with open(config_path, encoding="utf-8-sig") as f:
            content = f.read()
            return content.strip() if strip else content
    if validate_file_exist:
        raise ConfigFileNotFoundException(file_name)
    return None
