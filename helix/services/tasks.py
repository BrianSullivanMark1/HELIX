"""TaskService — runnable 'action' apps: the built apps that *do a thing* rather than open a screen.

In V2 these are the Python-kind builds; running one launches it in its own console. (HTML apps open in
the browser from the menu instead.)
"""
from __future__ import annotations

import subprocess
import sys

from helix.domain.models import App, AppKind
from helix.services.builds import BuildService


class TaskService:
    def __init__(self, builds: BuildService) -> None:
        self._builds = builds

    def runnable(self) -> list[App]:
        return [a for a in self._builds.list() if a.kind == AppKind.PYTHON]

    def run(self, slug: str) -> bool:
        app = next((a for a in self._builds.list() if a.slug == slug), None)
        if app is None or app.kind != AppKind.PYTHON or not app.entry_point:
            return False
        try:
            subprocess.Popen(
                [sys.executable, app.entry_point],
                cwd=str(self._builds.workspace(slug)),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return True
        except Exception:
            return False
