import yaml
import os
from typing import Any, Dict, Optional


def get_config_file(dump_path: str, file_name: str) -> Optional[str]:
    names = [f".{file_name}", f"{file_name}.cfg", file_name, f"{file_name}.yaml"]
    for n in names:
        p = os.path.expanduser(os.path.join(dump_path, "configs", n))
        if os.path.exists(p):
            return p
    return None


def get_config_content(dump_path: str, file_name: str, validate_file_exist: bool = True) -> str:
    config_path = get_config_file(dump_path, file_name)
    if config_path:
        with open(config_path, 'r', encoding='utf-8-sig') as f:
            return f.read().strip()
    if validate_file_exist:
        raise Exception(f"{file_name} file not found")
    return ""


def get_yaml_config(dump_path: str, file_name: str, default: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    config_path = get_config_file(dump_path, file_name)
    if not config_path:
        return default

    with open(config_path, 'r', encoding='utf-8-sig') as f:
        return yaml.safe_load(f)
