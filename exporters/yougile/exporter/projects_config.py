import re
from typing import Iterable, List, NamedTuple, Tuple


# the prefix of YouGile task keys: letters of any alphabet and digits
PREFIX_PATTERN = re.compile(r"^[^\W_]+$")


class ProjectLine(NamedTuple):
    prefix: str
    title: str


def parse_projects(lines: Iterable[str]) -> Tuple[List[ProjectLine], List[str]]:
    """Parse lines ``<prefix>:<YouGile project title>`` of ``projects.cfg``; return the projects and the errors."""
    projects, errors = [], []
    prefixes, titles = {}, {}
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        prefix, separator, title = line.partition(":")
        prefix, title = prefix.strip(), title.strip()
        if not separator or not prefix or not title:
            errors.append(f"projects.cfg, line {number}: expected <prefix>:<YouGile project title>, got '{line}'")
            continue
        if not PREFIX_PATTERN.match(prefix):
            errors.append(f"projects.cfg, line {number}: prefix '{prefix}' must consist of letters and digits only")
            continue
        # queue keys do not differ in letter case only
        if prefix.casefold() in prefixes:
            errors.append(f"projects.cfg, line {number}: prefix {prefix} is already set in line {prefixes[prefix.casefold()]}")
            continue
        if title in titles:
            errors.append(f"projects.cfg, line {number}: project '{title}' is already set in line {titles[title]}")
            continue
        prefixes[prefix.casefold()], titles[title] = number, number
        projects.append(ProjectLine(prefix, title))

    if not projects and not errors:
        errors.append("projects.cfg has no projects: add lines <prefix>:<YouGile project title>")
    return projects, errors
