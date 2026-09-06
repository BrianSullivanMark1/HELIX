"""Restarter — relaunch HELIX so a merged self-change takes effect.

Edge/I-O. Spawns a fresh process running the same entry point; the caller then quits the Qt loop. In a
frozen build sys.executable is the app itself, which is the right thing to relaunch; in dev it's the
Python interpreter, so we pass the entry script.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class Restarter:
    def __init__(self, entry: Path, root: Path) -> None:
        self._entry = entry
        self._root = root

    def restart(self) -> None:
        frozen = getattr(sys, "frozen", False)
        cmd = [sys.executable] if frozen else [sys.executable, str(self._entry)]
        # Mark this as a self-relaunch so the new process WAITS for us to release the single-instance lock
        # and then takes over, instead of treating us (still shutting down) as a rival and bouncing off.
        cmd.append("--relaunch")
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # Explicit std handles: the new process must not inherit this (dying) one's descriptors — a
        # corrupted inherited handle kills a Windows child at spawn (WinError 50), which here would
        # mean HELIX quits and never comes back.
        subprocess.Popen(cmd, cwd=str(self._root), close_fds=True, creationflags=creationflags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # The caller (UI) quits the current app; the freshly spawned process loads the new code.
