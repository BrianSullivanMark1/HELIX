"""Rebuilder — schedules the detached rebuild-and-relaunch of a FROZEN HELIX (READ_ME/DREAM.md §6).

A packaged HELIX runs the code bundled under dist/HELIX/_internal; a self-change merged into the
SOURCE repository cannot reach it until build.py runs again. So when a dream session applied changes,
this adapter writes a job file under data/rebuild/ and spawns `<dev_python> <source_root>/scripts/
rebuild_and_relaunch.py <job>` DETACHED (its own process group, no console, stdio to
data/rebuild/rebuild.log), then returns. The CALLER quits the app: the script waits for HELIX.exe to
exit, sets the current build aside as dist/HELIX.prev, runs build.py, relaunches, waits for the new
app to answer, and restores the previous build if anything fails — writing data/rebuild/
last_result.json for the morning report either way.

Edge/I-O only. Every decision that needs a fact from the machine (frozen? which exe? is there a
desktop shortcut?) is injectable so tests drive it without freezing anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("rebuild")

JOB_DIR = "rebuild"                      # under data/
LOG_NAME = "rebuild.log"
RESULT_NAME = "last_result.json"
SCRIPT_NAME = "rebuild_and_relaunch.py"  # under <source_root>/scripts/
SHORTCUT_NAME = "HELIX.lnk"
# The web shell's port + token settings — the SAME keys helix.api.server reads and writes (an adapter
# must not import the api layer, so they are spelled out; tests/test_rebuild.py pins them equal).
PORT_SETTING = "web_port"
TOKEN_SETTING = "web_token"
DEFAULT_PORT = 8737
# Where build.py puts the app, relative to the source root — the folder the script sets aside/restores.
DIST_REL = Path("dist") / "HELIX"


def _detached_flags() -> int:
    """The child outlives its parent (which is about to quit) and never flashes a console."""
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


# The bundle-only environment a frozen HELIX must not hand to the dev Python that runs build.py —
# the same list the gate scrubs before running the suite (services/selfdev.py: PyInstaller's
# interpreter home and Qt plugin folder would point the child at the bundle's internals).
_FROZEN_ENV_POISON = ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
                      "QT_QPA_PLATFORM_PLUGIN_PATH", "TCL_LIBRARY", "TK_LIBRARY")


def child_env(frozen: bool | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if getattr(sys, "frozen", False) if frozen is None else frozen:
        for key in _FROZEN_ENV_POISON:
            env.pop(key, None)
    return env


def _same_path(a: Path, b: Path) -> bool:
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        return False
    if sys.platform == "win32":
        return str(ra).casefold() == str(rb).casefold()
    return ra == rb


class Rebuilder:
    def __init__(self, paths, settings, *, spawn=None, exe: str | Path | None = None,
                 home: str | Path | None = None, clock=None) -> None:
        self._paths = paths
        self._settings = settings
        self._spawn = spawn or self._spawn_detached
        self._exe = Path(exe) if exe else Path(sys.executable)
        self._home = Path(home) if home else Path(os.environ.get("USERPROFILE") or Path.home())
        self._clock = clock

    # ----- facts -----
    def job_dir(self) -> Path:
        return Path(self._paths.data) / JOB_DIR

    def source_root(self) -> Path | None:
        return getattr(self._paths, "source_root", None)

    def script(self) -> Path | None:
        root = self.source_root()
        return None if root is None else Path(root) / "scripts" / SCRIPT_NAME

    def dist_dir(self) -> Path:
        """The folder the running app lives in (dist/HELIX): the exe's parent."""
        return self._exe.parent

    def launch_path(self) -> Path:
        """What the script starts afterwards: the desktop shortcut when there is one (it carries the
        user's own arguments and working folder), else the exe itself."""
        for candidate in (self._home / "OneDrive" / "Desktop" / SHORTCUT_NAME,
                          self._home / "Desktop" / SHORTCUT_NAME):
            if candidate.is_file():
                return candidate
        return self._exe

    def why_unavailable(self) -> str | None:
        """None when a rebuild can be scheduled; else one plain line the status shows."""
        if not getattr(self._paths, "is_frozen", False):
            return "HELIX is running from source — a restart loads applied changes; nothing to rebuild"
        root = self.source_root()
        if root is None:
            return "the source repository isn't reachable (set source_root in helix_settings.json)"
        if not getattr(self._paths, "dev_python", None):
            return "no Python interpreter is known for the build (set dev_python in helix_settings.json)"
        script = self.script()
        if script is None or not script.is_file():
            return f"the rebuild script is missing from {Path(root) / 'scripts'}"
        if not _same_path(self.dist_dir(), Path(root) / DIST_REL):
            return (f"the running app isn't the build in {Path(root) / DIST_REL} — a rebuild there "
                    "wouldn't replace it")
        return None

    def available(self) -> bool:
        return self.why_unavailable() is None

    # ----- the job -----
    def schedule(self, *, reason: str) -> Path:
        """Write the job file and spawn the detached script; returns the job path. Raises when a
        rebuild can't be scheduled (the caller journals why) — never quits anything itself."""
        why = self.why_unavailable()
        if why:
            raise RuntimeError(why)
        root = Path(self.source_root())
        now = self._clock.now() if self._clock is not None else datetime.now().astimezone()
        job_dir = self.job_dir()
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            port = int(self._settings.get(PORT_SETTING) or DEFAULT_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        # Exactly the §6 contract — the script derives the image name from `exe`.
        job = {
            "source_root": str(root),
            "python": str(self._paths.dev_python),
            "exe": str(self._exe),
            "data_dir": str(self._paths.data),
            "port": port,
            "token": str(self._settings.get(TOKEN_SETTING) or ""),
            "launch": str(self.launch_path()),
            "reason": str(reason or ""),
            "requested_at": now.isoformat(timespec="seconds"),
        }
        path = job_dir / f"job-{now:%Y%m%d-%H%M%S}.json"
        # The job carries the web token: an older job has served (the script deletes its own when
        # it finishes; one it never got to run is stale) — only the newest may exist.
        for old in job_dir.glob("job-*.json"):
            if old != path:
                try:
                    old.unlink()
                except OSError:
                    _LOG.warning("could not remove an old rebuild job %s", old, exc_info=True)
        path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        cmd = [job["python"], str(self.script()), str(path)]
        self._spawn(cmd, root, job_dir / LOG_NAME)
        _LOG.info("rebuild scheduled (%s): %s", reason, path)
        return path

    def last_result(self) -> dict | None:
        """What the last rebuild did — written by the script; read by the morning report."""
        try:
            data = json.loads((self.job_dir() / RESULT_NAME).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _spawn_detached(cmd: list[str], cwd: Path, log_path: Path) -> None:
        with open(log_path, "ab") as log:
            log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} spawning: {' '.join(cmd)}\n".encode("utf-8"))
            kwargs: dict = {
                "cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": log,
                "stderr": subprocess.STDOUT, "close_fds": True, "env": child_env(),
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = _detached_flags()
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **kwargs)
