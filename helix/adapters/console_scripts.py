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
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from helix.adapters import pulse as _pulse

NOT_WINDOWS = "The console scripts are PowerShell and run on Windows only."
NO_CONSOLE = "The console checkout is not set. Open a Deploy window and point HELIX at the folder that holds dev.ps1."
NO_SCRIPT = "dev.ps1 was not found in the console checkout: {path}"
NOT_SIGNED_IN = ("Your Google sign-in has expired (gcloud wants you to sign in again). In a terminal: "
                 "gcloud auth login   - then Ship again. Nothing ran.")
OLD_SCRIPT = ("{path} is older than this HELIX: it does not know {flags}. Pull the console checkout "
              "(the 2026-09-20 change), then try again. Nothing ran.")

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
    def deploy_argv(self, app: str, env: str, action: str = "be-deploy", *, typed: str = "",
                    create: bool = False) -> tuple[list[str], str | None]:
        """`typed` is dev.ps1's own production confirm (-Typed PROD), which the service passes only
        after HELIX's four conditions held; `create` is the first deploy of a cell that does not
        exist yet (-Create). Both need a dev.ps1 that knows them - an older one is refused in a
        sentence rather than run with a parameter it would choke on."""
        script, why = self.script()
        if script is None:
            return [], why
        if action not in ACTIONS:
            return [], f"{action} is not an action HELIX runs."
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Action", action, "-App", app.lower(), "-Env", env.lower()]
        if typed or create:
            missing = [flag for flag, want in (("$Typed", bool(typed)), ("$Create", create)) if want and not self.knows(flag)]
            if missing:
                return [], OLD_SCRIPT.format(path=str(script), flags=" and ".join("-" + m[1:] for m in missing))
            if typed:
                argv += ["-Typed", typed]
            if create:
                argv += ["-Create"]
        return argv, None

    def knows(self, param: str) -> bool:
        """Does the dev.ps1 on this PC declare `param` (e.g. '$Typed')? Read from the file itself."""
        script, _ = self.script()
        if script is None:
            return False
        try:
            head = script.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            return False
        return re.search(r"\]\s*" + re.escape(param) + r"\b", head) is not None   # "[string]$Typed", "[switch]$Create"

    def rollback_argv(self, project: str, region: str, service: str, revision: str) -> list[str]:
        return [self._gcloud, "run", "services", "update-traffic", service, "--to-revisions", f"{revision}=100",
                "--project", project, "--region", region, "--format", "json"]

    # ---------------------------------------------------------------- is anyone signed in?
    def signed_in(self, timeout_s: float = 25.0) -> tuple[bool, str | None]:
        """Can gcloud hand out a token right now, without a prompt? A deploy that starts on an
        expired sign-in would sit on gcloud's "Reauthentication required" forever (2026-09-22:
        seven quiet minutes on Brian's PC). Asked BEFORE every run; the sentence says what to do."""
        try:
            p = subprocess.run([self._gcloud, "auth", "print-access-token"], capture_output=True, text=True,
                               timeout=timeout_s, creationflags=_QUIET, stdin=subprocess.DEVNULL,
                               env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"})
        except FileNotFoundError:
            return False, "gcloud is not installed on this PC (Settings > The Board > The tools on this PC)."
        except subprocess.TimeoutExpired:
            return False, "gcloud did not answer in time. Run 'gcloud auth login' in a terminal, then try again."
        except OSError as exc:
            return False, f"gcloud could not start: {exc}"
        if p.returncode == 0 and (p.stdout or "").strip():
            return True, None
        err = (p.stderr or "").strip()
        if "reauth" in err.lower() or "login" in err.lower() or "credential" in err.lower() or not err:
            return False, NOT_SIGNED_IN
        return False, f"gcloud refused: {err[:200]}"

    # ---------------------------------------------------------------- running, line by line
    @staticmethod
    def wrap(argv: list[str]) -> list[str]:
        """`powershell -File dev.ps1 ...` -> a `-Command` that pushes EVERY stream (Write-Host is
        the information stream) through Console.Out, flushed per line, and hands back the script's
        exit code. Windows PowerShell with no console window and a redirected stdout kept
        Write-Host to itself - HELIX saw one line in seven minutes (2026-09-22)."""
        if "-File" not in argv:
            return argv
        i = argv.index("-File")
        head, script, rest = argv[:i], argv[i + 1], argv[i + 2:]

        def q(a: str) -> str:
            return "'" + a.replace("'", "''") + "'"

        call = "& " + q(script) + "".join(" " + (a if a.startswith("-") else q(a)) for a in rest)
        body = ("$ErrorActionPreference='Continue'; " + call + " *>&1 | ForEach-Object { "
                "$s = if ($_ -is [System.Management.Automation.InformationRecord]) { $_.MessageData.ToString() } else { $_.ToString() }; "
                "[Console]::Out.WriteLine($s); [Console]::Out.Flush() }; exit $LASTEXITCODE")
        return head + ["-Command", body]

    def stream(self, argv: list[str], cwd: Path | None, on_line: Callable[[str], None],
               timeout_s: float = 1800.0, on_start: Callable[[object], None] | None = None) -> int:
        """Run argv, hand every output line to on_line (ASCII-cleaned), return the exit code.
        `on_start` gets the process as soon as it exists (the register's cancel needs its pid)."""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"                  # gcloud is Python: unbuffered, its lines arrive as they happen
        env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"     # gcloud never waits on a question nobody can see: it fails, in words
        _pulse.spawned("dev.ps1" if any(str(a).endswith("dev.ps1") for a in argv) else os.path.basename(str(argv[0])).split('.')[0])
        argv = self.wrap(argv)
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


_CONSOLE_MARK = "BRMS MES"      # the first lines of the real dev.ps1 say so
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "venv", ".venv", "__pycache__", "AppData", "Library"}


def _homes() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    out = []
    for rel in ("OneDrive/Desktop", "Desktop", "OneDrive/Documents", "Documents", "OneDrive", "repos", "src", "code", "dev", "."):
        p = home / rel
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def find_console(homes: list[Path] | None = None, *, depth: int = 3) -> str | None:
    """Where the console checkout is on this PC, found rather than asked for: a `dev.ps1` that
    introduces itself as the BRMS MES helper, within a few folders of where people keep repos.
    Exactly one hit is the answer; none or several = None (the person is asked)."""
    hits: list[Path] = []
    seen: set[Path] = set()

    def walk(d: Path, left: int) -> None:
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name == "dev.ps1" and e.is_file():
                try:
                    if _CONSOLE_MARK in e.read_text(encoding="utf-8", errors="replace")[:400]:
                        r = e.parent.resolve()
                        if r not in seen:
                            seen.add(r)
                            hits.append(r)
                except OSError:
                    pass
        if left <= 0:
            return
        for e in entries:
            if e.is_dir() and not e.name.startswith(".") and e.name not in _SKIP_DIRS:
                walk(e, left - 1)

    for h in (homes if homes is not None else _homes()):
        walk(h, depth)
        if len(hits) > 1:
            break
    return str(hits[0]) if len(hits) == 1 else None


def lines_of(text: str) -> Iterator[str]:
    for ln in text.splitlines():
        yield ln
