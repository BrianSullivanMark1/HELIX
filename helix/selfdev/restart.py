"""Deliberate self-restart so a merged self-improvement goes live (§selfdev).

The supervisor (`scripts/run_helix.py`) relaunches the app when it exits with RESTART_EXIT_CODE. After
a change is merged into main, HELIX flags a restart; the main window performs it on a safe tick (never
mid trade-cycle), the supervisor relaunches, and the new code loads. Settings-backed and Qt-free.
"""
from __future__ import annotations

from typing import Any

RESTART_EXIT_CODE = 42  # the supervisor treats this exit code as "relaunch immediately" (not a crash)
RESTART_PENDING_SETTING = "selfdev_restart_pending"


def request_restart(settings: Any) -> None:
    settings.set(RESTART_PENDING_SETTING, True)


def restart_pending(settings: Any) -> bool:
    return bool(settings.get(RESTART_PENDING_SETTING))


def clear_restart(settings: Any) -> None:
    settings.set(RESTART_PENDING_SETTING, False)
