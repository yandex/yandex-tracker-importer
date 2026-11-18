import os
from typing import Optional, Dict


def get_config_file(dump_path: str, file_name: str) -> Optional[str]:
    names = [f".{file_name}", f"{file_name}.cfg", file_name]
    for n in names:
        p = os.path.expanduser(os.path.join(dump_path, "configs", n))
        if os.path.exists(p):
            return p
    return None


def get_config_content(dump_path: str, file_name: str, validate_file_exist: bool = True) -> Optional[str]:
    config_path = get_config_file(dump_path, file_name)
    if config_path:
        with open(config_path, encoding="utf-8-sig") as f:
            return f.read().strip()
    if validate_file_exist:
        raise Exception(f"{file_name} file not found")
    return None


def get_generic_keys_mapping(dump_path: str, mapping_file_name: str, validate_file_exist: bool = True) -> Dict[str, str]:
    mapping_path = get_config_file(dump_path, mapping_file_name)
    if not mapping_path:
        if validate_file_exist:
            raise Exception(f"{mapping_file_name} mapping file not found")
        else:
            return {}

    mapping = {}
    with open(mapping_path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split(':', 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid mapping format: {line}. Expected format: <source_key>:<target_key>")

            source_key = parts[0].strip()
            target_key = parts[1].strip()
            if source_key in mapping:
                raise ValueError(f"{mapping_file_name}: {source_key} is listed more than once")

            mapping[source_key] = target_key

    return mapping
