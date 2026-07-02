"""Watchdog — keeps an always-on HELIX alive across runtime crashes.

Each healthy startup spawns ONE tiny detached watchdog process that waits for its parent HELIX to die.
A clean quit writes a sentinel first (bootstrap wires it to aboutToQuit), so the watchdog sees it and
stands down; a crash/kill leaves no sentinel and the watchdog relaunches the app. The relaunch journal
caps rapid crash-loops (a boot-crash bug must not spawn processes forever — self-heal owns that case).

Edge/I-O only. The watchdog process itself imports no Qt (see cli.py: the 'watchdog' command routes
here before any UI import), so it costs a few MB while it idles.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CLEAN_EXIT_SENTINEL = ".helix-clean-exit"
RELAUNCH_JOURNAL = ".helix-relaunches.json"
_MAX_RELAUNCHES = 3  # within the window below — beyond it, stand down instead of crash-looping
_WINDOW_MINUTES = 10


def _detached_flags() -> int:
    """Process-creation flags so the child outlives its parent and never flashes a console window."""
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _app_cmd(entry: Path) -> list[str]:
    """The command that (re)launches HELIX — the frozen exe relaunches itself; dev runs main.py."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(entry)]


def spawn_watchdog(data_dir: Path, entry: Path, root: Path) -> None:
    """Start the detached watchdog for THIS process. Best-effort: a machine that refuses to spawn it
    just runs without crash protection, exactly as before."""
    try:
        cmd = _app_cmd(entry) + [
            "watchdog", "--pid", str(os.getpid()),
            "--data", str(data_dir), "--entry", str(entry), "--root", str(root),
        ]
        kwargs: dict = {"close_fds": True, "cwd": str(root)}
        if sys.platform == "win32":
            kwargs["creationflags"] = _detached_flags()
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:  # noqa: BLE001 — never let the guardian break the guarded
        pass


def mark_clean_exit(data_dir: Path) -> None:
    """Called on aboutToQuit: tell the watchdog this death is intentional."""
    try:
        (data_dir / CLEAN_EXIT_SENTINEL).write_text("1", encoding="utf-8")
    except OSError:
        pass


def clear_clean_exit(data_dir: Path) -> None:
    """Called at startup, so a later crash isn't masked by a stale sentinel."""
    try:
        (data_dir / CLEAN_EXIT_SENTINEL).unlink(missing_ok=True)
    except OSError:
        pass


def _wait_for_pid(pid: int) -> None:
    """Block until the given process exits. Windows: a real kernel wait (no polling). Elsewhere: poll."""
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return  # already gone
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(2.0)


def _too_many_recent_relaunches(journal: Path) -> bool:
    """Read, prune, and update the relaunch journal; True when the cap is hit."""
    now = datetime.now()
    stamps: list[str] = []
    try:
        stamps = [s for s in json.loads(journal.read_text(encoding="utf-8")) if isinstance(s, str)]
    except (OSError, ValueError):
        stamps = []
    cutoff = now - timedelta(minutes=_WINDOW_MINUTES)
    recent = []
    for s in stamps:
        try:
            if datetime.fromisoformat(s) >= cutoff:
                recent.append(s)
        except ValueError:
            continue
    if len(recent) >= _MAX_RELAUNCHES:
        return True
    recent.append(now.isoformat())
    try:
        journal.write_text(json.dumps(recent), encoding="utf-8")
    except OSError:
        pass
    return False


def watchdog_main(pid: int, data_dir: Path, entry: Path, root: Path) -> int:
    """The watchdog process body: wait for the app to die; relaunch unless it quit cleanly."""
    _wait_for_pid(pid)
    sentinel = data_dir / CLEAN_EXIT_SENTINEL
    if sentinel.exists():
        return 0  # intentional quit (or restart — the new process brings its own watchdog)
    if _too_many_recent_relaunches(data_dir / RELAUNCH_JOURNAL):
        return 1  # crash-looping — stand down; self-heal / the user takes it from here
    try:
        kwargs: dict = {"close_fds": True, "cwd": str(root)}
        if sys.platform == "win32":
            kwargs["creationflags"] = _detached_flags()
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(_app_cmd(entry), **kwargs)
    except Exception:  # noqa: BLE001
        return 1
    return 0
