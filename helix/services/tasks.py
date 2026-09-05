"""TaskService — runnable 'action' apps: the built apps that *do a thing* rather than open a screen.

In V2 these are the Python-kind builds; running one launches it in its own console. (HTML apps open in
the browser from the menu instead.) Launched processes are tracked so the UI can report status and a
later 'stop' / close can reason about them.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from helix.domain.models import App, AppKind, BuildKind, slugify
from helix.logging_setup import get_logger
from helix.services.builds import BuildService

if TYPE_CHECKING:
    from helix.services.connections import ConnectionsService
    from helix.services.knowledge import KnowledgeService

# A task writes results it wants saved into the folder named by this env var; HELIX ingests them into a
# knowledge base when the task finishes. (See build_task_prompt.)
KNOWLEDGE_OUTBOX_ENV = "HELIX_KNOWLEDGE_OUTBOX"
_OUTBOX_DIR = ".knowledge_out"

_LOG = get_logger("tasks")


def _python() -> str:
    """The interpreter to run a task with. In a PyInstaller-frozen app sys.executable is HELIX.exe — so
    launching it would relaunch HELIX, not the task. Fall back to a real Python on PATH."""
    if getattr(sys, "frozen", False):
        return shutil.which("pythonw") or shutil.which("python") or shutil.which("py") or sys.executable
    return sys.executable


# A build's server writes its process id here. A HELIX that quit (or crashed, or was rebuilt)
# never used to stop the servers it launched: they lived on as orphans with the build's folder as
# their working directory, and Windows then refused to move that folder — so "remove the music
# player" failed with "it's open or running" long after the HELIX that opened it was gone. The pid
# file lets ANY later HELIX find and stop that server: at boot (a sweep), before a delete, on stop.
PID_FILE = ".server.pid"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def process_image(pid: int) -> str | None:
    """The executable path of a LIVE process with this id, or None. Windows asks the kernel
    (a pid can be recycled by an unrelated program — the caller checks the image is a Python);
    elsewhere a liveness probe only."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
                return None  # exited, handle still openable
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return "python"


def terminate_pid(pid: int) -> None:
    """Stop a process by id (TerminateProcess on Windows, SIGTERM elsewhere)."""
    os.kill(pid, signal.SIGTERM)


def _is_python_image(image: str | None) -> bool:
    return bool(image) and os.path.basename(image).lower().startswith("python")


class TaskService:
    def __init__(
        self, builds: BuildService, connections: "ConnectionsService | None" = None,
        knowledge: "KnowledgeService | None" = None, *, probe=None, terminate=None,
    ) -> None:
        self._builds = builds
        self._probe = probe or process_image      # pid -> live image path (injectable for tests)
        self._terminate = terminate or terminate_pid
        self._connections = connections  # injects the build's declared API keys as env vars at launch
        self._knowledge = knowledge       # ingests a task's outbox into a knowledge base when it finishes
        self._procs: dict[str, subprocess.Popen] = {}  # slug -> live process (for status / cleanup)
        # run() (a worker thread) and stop()/_prune() (the UI thread) both mutate _procs; guard every
        # access so a concurrent insert can't make _prune's iterate-then-pop raise "changed size during
        # iteration" (surfaced as a spurious launch failure).
        self._lock = threading.Lock()

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
        with self._lock:
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
        # Give the task an OUTBOX it can drop results into; HELIX ingests them into a knowledge base when it
        # finishes (see _watch_knowledge_outbox). Only console TASK runs are watched — a headless backend
        # app (port set) is a server, not a finishing job, so it's never harvested.
        outbox = ws / _OUTBOX_DIR
        try:
            outbox.mkdir(exist_ok=True)
            env[KNOWLEDGE_OUTBOX_ENV] = str(outbox)
        except OSError:
            pass
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
            with self._lock:
                self._procs[slug] = proc
            self._write_pid(ws, proc.pid)
            # Watch a finishing console task for results to harvest into the user's knowledge. A server app
            # (headless, runs indefinitely) is never harvested.
            if not headless and self._knowledge is not None:
                threading.Thread(
                    target=self._watch_knowledge_outbox, args=(app.name, proc, outbox),
                    daemon=True, name=f"helix-knowledge-{slug}",
                ).start()
            return True
        except Exception:
            _LOG.exception("could not launch task %s", slug)
            return False

    def _watch_knowledge_outbox(self, task_name: str, proc: subprocess.Popen, outbox) -> None:
        """Wait for a task to finish, then ingest anything it wrote to its outbox into a knowledge base
        named after the task (created on demand). Daemon thread — best-effort, never raises into the app."""
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            return
        if self._knowledge is None:
            return
        try:
            self._knowledge.ingest_outbox(f"{task_name} results", outbox)
        except Exception:  # noqa: BLE001 - a harvest failure must never disturb the app
            _LOG.warning("could not harvest knowledge from task %s", task_name, exc_info=True)

    def stop(self, slug: str) -> None:
        """Terminate one running build (e.g. a backend app's server when its viewer closes) —
        this HELIX's own child, or an orphan from an earlier HELIX found through its pid file."""
        self.release(slug)

    def stop_all(self) -> int:
        """Quit-time teardown: stop every server this HELIX launched, so none outlives it as an
        orphan holding its build folder open. Returns how many were stopped."""
        with self._lock:
            procs = dict(self._procs)
            self._procs.clear()
        n = 0
        for slug, proc in procs.items():
            try:
                if proc.poll() is None:
                    proc.terminate()
                    n += 1
            except Exception:  # noqa: BLE001
                pass
            self._clear_pid(self._builds.workspace(slug))
        return n

    def release(self, slug: str) -> bool:
        """Make a build's folder movable: stop its server — ours, or an orphan's found through the
        pid file. True when a process was actually running and is now stopped."""
        with self._lock:
            proc = self._procs.pop(slug, None)
        was = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    was = True
            except Exception:  # noqa: BLE001
                pass
        return self.reap(slug) or was

    def reap(self, slug: str) -> bool:
        """Stop a server recorded in the build's pid file that this HELIX doesn't track (it was
        launched by an earlier HELIX life). Only a Python image is ever killed — a recycled pid
        that now belongs to something else just gets its stale file cleared. True when killed."""
        ws = self._builds.workspace(slug)
        pid = self._read_pid(ws)
        if pid is None:
            return False
        killed = False
        image = self._probe(pid)
        if _is_python_image(image):
            try:
                self._terminate(pid)
                killed = True
            except OSError:
                pass
            for _ in range(20):  # let the folder unlock before a delete follows
                if self._probe(pid) is None:
                    break
                time.sleep(0.1)
        self._clear_pid(ws)
        if killed:
            _LOG.info("stopped an orphaned server for %s (pid %s)", slug, pid)
        return killed

    def reap_orphans(self) -> list[str]:
        """Boot-time sweep: every build with a pid file this HELIX doesn't own gets its stale server
        stopped. Returns the slugs whose servers were killed."""
        killed: list[str] = []
        try:
            folders = [d for d in self._builds.dir.iterdir() if d.is_dir() and (d / PID_FILE).is_file()]
        except OSError:
            return killed
        with self._lock:
            tracked = set(self._procs)
        for d in folders:
            if d.name in tracked:
                continue
            if self.reap(d.name):
                killed.append(d.name)
        return killed

    # ----- pid files -----
    @staticmethod
    def _write_pid(ws, pid: int) -> None:
        try:
            (ws / PID_FILE).write_text(json.dumps({"pid": int(pid)}), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _read_pid(ws) -> int | None:
        try:
            data = json.loads((ws / PID_FILE).read_text(encoding="utf-8"))
            pid = int(data.get("pid"))
            return pid if pid > 0 else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _clear_pid(ws) -> None:
        try:
            (ws / PID_FILE).unlink()
        except OSError:
            pass

    def _prune(self) -> None:
        with self._lock:
            for slug in [s for s, p in self._procs.items() if p.poll() is not None]:
                self._procs.pop(slug, None)
