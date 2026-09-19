"""Project links - where each app's code lives, as the user set it on the card's gear.

The FLEET table carries a default repo and branch per app; a link overrides them, and can add
the local folder the app is worked on from. Links are plain data in settings (`project_links`):
    {"MES": {"repo": "BrendanSullivanMark1/BRMS_MES_WEB_VERSION", "branch": "main", "folder": "C:\\..."}}
Pure: validation and the override, nothing else. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
import re

from helix.domain.fleet import Service

SETTING = "project_links"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")
MAX_FOLDER = 300

BAD_REPO = "A repo is written owner/name, like BrendanSullivanMark1/mrp_prod."
BAD_BRANCH = "A branch is a plain git ref, like main or v3."
BAD_FOLDER = "The folder path is too long."


@dataclass(frozen=True)
class Link:
    repo: str | None = None
    branch: str | None = None
    folder: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in (("repo", self.repo), ("branch", self.branch), ("folder", self.folder)) if v}


def parse_link(body: Mapping[str, Any]) -> tuple[Link | None, str | None]:
    """(link, None) or (None, one plain sentence). Empty fields mean 'back to the table's default'."""
    repo = str(body.get("repo") or "").strip().strip("/")
    branch = str(body.get("branch") or "").strip()
    folder = str(body.get("folder") or "").strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):].removesuffix(".git").strip("/")
    if repo and not _REPO.match(repo):
        return None, BAD_REPO
    if branch and not _BRANCH.match(branch):
        return None, BAD_BRANCH
    if len(folder) > MAX_FOLDER:
        return None, BAD_FOLDER
    return Link(repo or None, branch or None, folder or None), None


def links_from(setting: Any) -> dict[str, Link]:
    """The stored map, tolerant of anything odd that landed in the settings file."""
    out: dict[str, Link] = {}
    if not isinstance(setting, Mapping):
        return out
    for app, raw in setting.items():
        if isinstance(raw, Mapping):
            link, why = parse_link(raw)
            if link and why is None and link.as_dict():
                out[str(app).upper()] = link
    return out


def apply_link(svc: Service, links: Mapping[str, Link]) -> Service:
    """The service as the board should read it: the table's defaults under the user's link."""
    link = links.get(svc.app.upper())
    if link is None:
        return svc
    return replace(svc, repo=link.repo or svc.repo, branch=link.branch or svc.branch)
