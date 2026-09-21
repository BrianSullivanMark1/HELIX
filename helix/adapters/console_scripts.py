"""The console scripts, wrapped - never rewritten (plan section 10.6).

`dev.ps1` in the BRMS_MES_WEB_VERSION checkout is the one thing that knows how each app ships:
which folder, which flags, which env vars (MES from backend/deploy.ps1, ECHO from its own block),
labels for provenance. HELIX calls it headless (`-Action be-deploy -App echo -Env dev`) and streams
its lines to the page. A rollback is not a script: it is one gcloud traffic shift.

Windows only in practice (the scripts are PowerShell); elsewhere the runner reports NOT_WINDOWS.
Every spawn is hidden (no console window) and its output is captured line by line.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

NOT_WINDOWS = "The console scripts are PowerShell and run on Windows only."
NO_CONSOLE = "The console checkout is not set. Open a Deploy window and point HELIX at the folder that holds dev.ps1."
NO_SCRIPT = "dev.ps1 was not found in the console checkout: {path}"

ACTIONS = {"be-deploy", "fe-deploy", "preflight", "git-status", "rev-clean"}
_QUIET = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Spawned:
    argv: tuple[str, ...]
    cwd: str


class ConsoleScripts:
    """Runs dev.ps1 headless. `console_root` is read on every call (a setting)."""

    def __init__(self, console_root: Callable[[] , str | None], *, gcloud: str = "gcloud",
                 popen=subprocess.Popen, platform: str = sys.platform) -> None:
        self._root = console_root
        self._gcloud = shutil.which(gcloud) or gcloud
        self._popen = popen
        self._platform = platform

    # ---------------------------------------------------------------- where
    def root(self) -> Path | None:
        r = (self._root() or "").strip()
        return Path(r) if r else None

    def script(self) -> tuple[Path | None, str | None]:
        if not self._platform.startswith("win"):
            return None, NOT_WINDOWS
        root = self.root()
        if root is None:
            return None, NO_CONSOLE
        p = root / "dev.ps1"
        if not p.is_file():
            return None, NO_SCRIPT.format(path=str(p))
        return p, None

    # ---------------------------------------------------------------- the plan (what would run)
    def deploy_argv(self, app: str, env: str, action: str = "be-deploy") -> tuple[list[str], str | None]:
        script, why = self.script()
        if script is None:
            return [], why
        if action not in ACTIONS:
            return [], f"{action} is not an action HELIX runs."
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Action", action, "-App", app.lower(), "-Env", env.lower()]
        return argv, None

    def rollback_argv(self, project: str, region: str, service: str, revision: str) -> list[str]:
        return [self._gcloud, "run", "services", "update-traffic", service, "--to-revisions", f"{revision}=100",
                "--project", project, "--region", region, "--format", "json"]

    # ---------------------------------------------------------------- running, line by line
    def stream(self, argv: list[str], cwd: Path | None, on_line: Callable[[str], None],
               timeout_s: float = 1800.0, on_start: Callable[[object], None] | None = None) -> int:
        """Run argv, hand every output line to on_line (ASCII-cleaned), return the exit code.
        `on_start` gets the process as soon as it exists (the register's cancel needs its pid)."""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = self._popen(argv, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                               creationflags=_QUIET, env=env)
        except FileNotFoundError:
            on_line(f"[helix] not found: {argv[0]}")
            return 127
        if on_start is not None:
            try:
                on_start(proc)
            except Exception:  # noqa: BLE001
                pass
        timer = threading.Timer(timeout_s, lambda: proc.kill())
        timer.start()
        try:
            for raw in iter(proc.stdout.readline, ""):   # type: ignore[union-attr]
                line = raw.rstrip("\r\n")
                on_line(line.encode("ascii", "replace").decode("ascii"))
            proc.wait()
        finally:
            timer.cancel()
        return int(proc.returncode or 0)


def lines_of(text: str) -> Iterator[str]:
    for ln in text.splitlines():
        yield ln
