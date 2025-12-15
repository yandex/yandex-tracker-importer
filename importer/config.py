from dataclasses import dataclass
from typing import Optional, Dict
import os

import yaml

from .exceptions import ConfigValidationError


@dataclass
class Config:
    dest_tracker_api: str
    dest_tracker_token: str
    dest_org_id: Optional[str] = None
    dest_cloud_org_id: Optional[str] = None

    def __post_init__(self):
        if not self.dest_tracker_api:
            raise ConfigValidationError("dest_tracker_api is required")

        if not self.dest_tracker_token:
            raise ConfigValidationError("dest_tracker_token is required")

        has_org_id = bool(self.dest_org_id)
        has_cloud_org_id = bool(self.dest_cloud_org_id)

        if has_org_id and has_cloud_org_id:
            raise ConfigValidationError(
                "Only one of dest_org_id or dest_cloud_org_id should be specified"
            )

        if not has_org_id and not has_cloud_org_id:
            raise ConfigValidationError(
                "One of dest_org_id or dest_cloud_org_id must be specified"
            )


def load_config(dump_path: str) -> Config:
    config_path = os.path.join(dump_path, "config.yaml")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"config.yaml not found at {config_path}")

    with open(config_path, "r") as f:
        config_data = yaml.safe_load(f)

    if not config_data:
        raise ValueError("config.yaml is empty or invalid")

    return Config(
        dest_tracker_api=config_data.get("dest_tracker_api", ""),
        dest_tracker_token=config_data.get("dest_tracker_token", ""),
        dest_org_id=config_data.get("dest_org_id"),
        dest_cloud_org_id=config_data.get("dest_cloud_org_id"),
    )


def get_config_file(dump_path: str, file_name: str) -> Optional[str]:
    names = [f".{file_name}", f"{file_name}.cfg", file_name]
    for n in names:
        p = os.path.expanduser(os.path.join(dump_path, n))
        if os.path.exists(p):
            return p
    return None


def get_generic_keys_mapping(dump_path: str, mapping_file_name: str, validate_file_exist: bool = True) -> Dict[str, str]:
    mapping_path = get_config_file(os.path.join(dump_path, "mapping"), mapping_file_name)
    if not mapping_path:
        if validate_file_exist:
            raise Exception(f"{mapping_file_name} mapping file not found")
        else:
            return {}

    mapping = {}
    with open(mapping_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split(':', 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid mapping format: {line}. Expected format: <source_key>:<target_key>")

            source_key = parts[0].strip()
            target_key = parts[1].strip()

            mapping[source_key] = target_key

    return mapping
