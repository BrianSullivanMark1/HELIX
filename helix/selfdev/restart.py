"""Deliberate self-restart so a merged self-improvement goes live (§selfdev).

After a change is merged into main, HELIX flags a restart; the main window performs it on a safe tick
(never mid trade-cycle) and the new code loads. Two relaunch paths, picked automatically:

  - **Supervised** — launched under `scripts/run_helix.py` (it sets `SUPERVISOR_ENV`). The app just
    exits with `RESTART_EXIT_CODE` and the supervisor relaunches it.
  - **Standalone** — launched directly (`python main.py`). No supervisor is watching, so the app
    spawns a fresh copy of itself before exiting, so the merge still goes live with no manual restart.

Settings-backed and Qt-free, so the policy stays unit-testable.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

RESTART_EXIT_CODE = 42  # the supervisor treats this exit code as "relaunch immediately" (not a crash)
RESTART_PENDING_SETTING = "selfdev_restart_pending"
SUPERVISOR_ENV = "HELIX_SUPERVISED"  # set by scripts/run_helix.py so we know it will relaunch us


def request_restart(settings: Any) -> None:
    settings.set(RESTART_PENDING_SETTING, True)


def restart_pending(settings: Any) -> bool:
    return bool(settings.get(RESTART_PENDING_SETTING))


def clear_restart(settings: Any) -> None:
    settings.set(RESTART_PENDING_SETTING, False)


def supervised() -> bool:
    """True when launched under `scripts/run_helix.py`, which relaunches us on RESTART_EXIT_CODE."""
    return os.environ.get(SUPERVISOR_ENV) == "1"


def spawn_replacement() -> bool:
    """Start a fresh, detached HELIX process from the same entry point (`main.py`).

    Used standalone (no supervisor) so an approved merge still goes live without a manual restart. The
    child outlives this process; best-effort — returns True if it was launched."""
    root = Path(__file__).resolve().parents[2]  # helix/selfdev/restart.py -> repo root
    main_py = root / "main.py"
    kwargs: dict[str, Any] = {"cwd": str(root)}
    if os.name == "nt":  # detach so the child survives this process's exit and gets its own group
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([sys.executable, str(main_py)], **kwargs)
        return True
    except OSError:
        return False


def perform_restart(settings: Any) -> None:
    """Carry out a pending restart. Clears the flag, then — when running standalone — spawns a fresh
    process to take over. The caller still exits with RESTART_EXIT_CODE (which the supervisor, if any,
    relaunches on). Clearing first means the replacement starts clean and can't loop."""
    clear_restart(settings)
    if not supervised():
        spawn_replacement()
