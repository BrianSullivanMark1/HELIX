"""TaskService — runnable 'action' apps: the built apps that *do a thing* rather than open a screen.

In V2 these are the Python-kind builds; running one launches it in its own console. (HTML apps open in
the browser from the menu instead.) Launched processes are tracked so the UI can report status and a
later 'stop' / close can reason about them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from helix.domain.models import App, AppKind, BuildKind, slugify
from helix.logging_setup import get_logger
from helix.services.builds import BuildService

if TYPE_CHECKING:
    from helix.services.connections import ConnectionsService

_LOG = get_logger("tasks")


def _python() -> str:
    """The interpreter to run a task with. In a PyInstaller-frozen app sys.executable is HELIX.exe — so
    launching it would relaunch HELIX, not the task. Fall back to a real Python on PATH."""
    if getattr(sys, "frozen", False):
        return shutil.which("pythonw") or shutil.which("python") or shutil.which("py") or sys.executable
    return sys.executable


class TaskService:
    def __init__(self, builds: BuildService, connections: "ConnectionsService | None" = None) -> None:
        self._builds = builds
        self._connections = connections  # injects the build's declared API keys as env vars at launch
        self._procs: dict[str, subprocess.Popen] = {}  # slug -> live process (for status / cleanup)

    def runnable(self) -> list[App]:
        return [a for a in self._builds.list() if a.build_kind == BuildKind.TASK]

    def find(self, name: str) -> App | None:
        """Resolve a task by slug or case-insensitive display name (for the orb's run-by-voice)."""
        slug = slugify(name)
        target = (name or "").strip().lower()
        return next(
            (a for a in self.runnable() if a.slug == slug or a.name.strip().lower() == target), None
        )

    def is_running(self, slug: str) -> bool:
        proc = self._procs.get(slug)
        return proc is not None and proc.poll() is None

    def run(self, slug: str, *, port: int | None = None, headless: bool = False) -> bool:
        """Launch a Python build. A console TASK runs in its own window (headless=False). An APP with a
        backend runs HEADLESS (no window) on the given PORT, with its output captured to server.log, so
        HELIX can show its page inside the app instead of a console/browser."""
        self._prune()
        app = next((a for a in self._builds.list() if a.slug == slug), None)
        if app is None or app.kind != AppKind.PYTHON or not app.entry_point:
            _LOG.warning("task %s is not runnable (kind=%s entry=%s)", slug, getattr(app, "kind", None),
                         getattr(app, "entry_point", None))
            return False
        # Inject the build's declared API keys as environment variables, so its code reads them from
        # os.environ instead of ever hardcoding a secret. Keys the user hasn't connected are simply absent.
        env = dict(os.environ)
        if self._connections is not None:
            env.update(self._connections.env_for(slug))
        if port is not None:
            env["PORT"] = str(port)  # HELIX assigns the port so backend apps never collide
        ws = self._builds.workspace(slug)
        stdout = stderr = None
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if headless:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window — shown inside HELIX
            try:
                stdout = open(ws / "server.log", "w", encoding="utf-8", errors="replace")
                stderr = subprocess.STDOUT
            except OSError:
                stdout = stderr = None
        try:
            proc = subprocess.Popen(
                [_python(), app.entry_point], cwd=str(ws), env=env,
                stdout=stdout, stderr=stderr, creationflags=flags,
            )
            self._procs[slug] = proc
            return True
        except Exception:
            _LOG.exception("could not launch task %s", slug)
            return False

    def stop(self, slug: str) -> None:
        """Terminate one running build (e.g. a backend app's server when its viewer closes)."""
        proc = self._procs.pop(slug, None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def terminate_all(self) -> None:
        """Best-effort stop of any task processes HELIX launched (available; not called on a normal close,
        since a task runs in its own console and is the user's to keep running)."""
        for proc in list(self._procs.values()):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        self._procs.clear()

    def _prune(self) -> None:
        for slug in [s for s, p in self._procs.items() if p.poll() is not None]:
            self._procs.pop(slug, None)
