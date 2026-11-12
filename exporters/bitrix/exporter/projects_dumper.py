import logging
import os

from tqdm import tqdm
from typing import Any, Dict, List, Optional, Iterable

from .bitrix_client import SimpleBitrixClient

from .base_dumper import BaseDumper, UsersFetchError
from .utils import to_tracker_datetime, to_tracker_day_start

logger = logging.getLogger(__name__)


class ProjectsDumper(BaseDumper):

    def __init__(self, client: SimpleBitrixClient, dest_path: str, max_workers: Optional[int]):
        super().__init__(client, dest_path, max_workers)
        self._exported_project_ids = set()

    @property
    def _filename(self) -> str:
        return "project.yaml"

    @staticmethod
    def _team_access(project_data: Dict[str, Any]) -> bool:
        """A closed (``OPENED`` N) or hidden (``VISIBLE`` N) group is accessible to its members only."""
        return project_data.get("OPENED") != "Y" or project_data.get("VISIBLE") != "Y"

    @staticmethod
    def _tags(project_data: Dict[str, Any]) -> List[str]:
        return [tag.strip() for tag in (project_data.get("KEYWORDS") or "").split(",") if tag.strip()]

    def _permissions(self, project_data: Dict[str, Any]) -> Dict[str, Any]:
        """GRANT: moderators of the group. WRITE: groups of the departments attached to a group accessible to its members
        only; an open group is editable by all employees."""
        acl = {}
        moderators = [self._get_user(user_id) for user_id in project_data.get("MODERATOR_MEMBERS") or []]
        if moderators:
            acl["GRANT"] = {"users": moderators}
        if self._team_access(project_data):
            department_ids = (project_data.get("UF_SG_DEPT") or {}).get("VALUE") or []
            if not isinstance(department_ids, list):
                department_ids = [department_ids]
            groups = []
            for department_id in department_ids:
                group = self._get_department(department_id)
                if not group:
                    raise Exception(f"Department {department_id} of group {project_data['ID']} is not exported")
                groups.append(group)
            groups = list(dict.fromkeys(groups))
            if groups:
                acl["WRITE"] = {"groups": groups}
        return {"acl": acl}

    def _dump_projects(self, projects: List[Dict[str, Any]], pbar_desc: str = "Dumping projects") -> Dict[str, bool]:
        user_ids = set()
        for project_data in projects:
            user_ids.add(str(project_data["OWNER_ID"]))
            for member_id in project_data.get("MEMBERS") or []:
                user_ids.add(str(member_id))
            for moderator_id in project_data.get("MODERATOR_MEMBERS") or []:
                user_ids.add(str(moderator_id))
        try:
            self._load_users(user_ids)
        except UsersFetchError:
            logger.exception("Failed to preload users, they are requested per project")

        results = {}
        for project_data in tqdm(projects, desc=pbar_desc):
            project_id = str(project_data["ID"])
            result_key = f"project {project_id}"
            try:
                dump_path = os.path.join(self._dest_path, "projects", project_id)
                attachments = self._fetcher.fetch_attachments(project_id)
                owner = self._get_user(project_data["OWNER_ID"])

                prepared_data = {
                    "summary": project_data["NAME"],
                    "id": self._project_dump_id(project_id),
                    "createdAt": to_tracker_datetime(project_data.get("DATE_CREATE")),
                    "createdBy": owner,
                    "teamAccess": self._team_access(project_data),
                    "author": owner,
                    "lead": owner,
                    "description": project_data.get("DESCRIPTION"),
                    "teamUsers": [self._get_user(user_id) for user_id in project_data.get("MEMBERS") or []],
                    "start": to_tracker_day_start(project_data.get("PROJECT_DATE_START")),
                    "end": to_tracker_day_start(project_data.get("PROJECT_DATE_FINISH")),
                    "tags": self._tags(project_data),
                    "permissions": self._permissions(project_data),
                    "attachments": [
                        {
                            "name": attachment["NAME"],
                            "filename": self._get_attachment_filename(attachment),
                        }
                        for attachment in attachments
                    ],
                }

                attachments_path = os.path.join(dump_path, "attachments")
                self._download_all_attachments_threaded(attachments, attachments_path)

                self._clean_and_save_data(prepared_data, dump_path)
                self._exported_project_ids.add(project_id)
                results[result_key] = True
            except Exception:
                logger.exception(f"Failed to dump project {project_id} '{project_data.get('NAME')}'")
                results[result_key] = False

        return results

    def dump_by_filter(self, filter: Dict[str, Any], pbar_desc: str = "Dumping projects by filter") -> Dict[str, bool]:
        project_ids = self._fetcher.fetch_project_ids_by_filter(filter)
        if project_ids is None:
            return {f"projects by filter {filter}": False}

        return self.dump_by_ids(project_ids, pbar_desc)

    def dump_by_ids(self, ids: Iterable[str], pbar_desc: str = "Dumping projects by ids") -> Dict[str, bool]:
        """Dump projects not dumped yet; ``False`` for every requested project that is not dumped."""
        not_exported_ids = [str(project_id) for project_id in ids if str(project_id) not in self._exported_project_ids]
        if not not_exported_ids:
            return {}
        projects = self._fetcher.fetch_projects_by_ids(not_exported_ids)
        if projects is None:
            return {f"project {project_id}": False for project_id in not_exported_ids}

        returned_ids = {str(project["ID"]) for project in projects}
        results = {f"project {project_id}": False for project_id in not_exported_ids if project_id not in returned_ids}
        results.update(self._dump_projects(projects, pbar_desc))
        return results
