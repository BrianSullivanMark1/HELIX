"""FaceBuilder - is the built face behind its source, and build it on request.

Edge/I-O. The web face (web/src, Vite) is served from web/dist; every edit to the source needs an
`npm run build` before the browser sees it. This adapter answers two questions the Settings page
asks - "is dist older than src?" and "has the Python changed since this process started?" - and
runs the one build the Update button triggers. Nothing here touches git, the fleet, or the model.

Windows: the Node installer ships `npm.cmd`, not `npm.exe`, and CreateProcess does not consult
PATHEXT, so the binary is resolved once through shutil.which (the gcloud lesson, 2026-09-17).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# What counts as "the face's source": an edit to any of these makes dist stale.
FACE_SOURCES: tuple[str, ...] = ("src", "index.html", "package.json", "vite.config.ts", "public")
PY_ROOTS: tuple[str, ...] = ("helix", "main.py")

NO_NPM = "npm is not installed on this machine (or not on PATH), so the face cannot be built here."
BUSY = "A build is already running."


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    seconds: float


Runner = Callable[[list[str], Path, float], Ran]


def _real_runner(argv: list[str], cwd: Path, timeout_s: float) -> Ran:
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace")
        return Ran(p.returncode, (p.stdout or "") + (p.stderr or ""), time.monotonic() - t0)
    except FileNotFoundError:
        return Ran(127, "not found", time.monotonic() - t0)
    except subprocess.TimeoutExpired:
        return Ran(124, "timed out", time.monotonic() - t0)
    except OSError as exc:  # noqa: BLE001
        return Ran(1, str(exc), time.monotonic() - t0)


def newest_mtime(paths: Iterable[Path], *, suffixes: tuple[str, ...] | None = None,
                 skip_dirs: tuple[str, ...] = ("node_modules", "__pycache__", "dist", ".git")) -> float:
    """The newest modification time under these paths (files only), 0.0 when nothing is there."""
    newest = 0.0
    for p in paths:
        if p.is_file():
            if suffixes is None or p.suffix in suffixes:
                newest = max(newest, p.stat().st_mtime)
            continue
        if not p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for f in filenames:
                if suffixes is not None and not f.endswith(suffixes):
                    continue
                try:
                    newest = max(newest, (Path(dirpath) / f).stat().st_mtime)
                except OSError:
                    pass
    return newest


class FaceBuilder:
    def __init__(self, root: Path, *, runner: Runner | None = None, npm: str = "npm",
                 started_at: float | None = None) -> None:
        self._root = Path(root)
        self._run = runner or _real_runner
        self._npm = shutil.which(npm) or npm
        self._npm_present = shutil.which(npm) is not None or runner is not None
        self._started_at = started_at if started_at is not None else time.time()
        self._lock = threading.Lock()
        self._building = False
        self._last: dict | None = None

    # ---------------------------------------------------------------- questions

    @property
    def web(self) -> Path:
        return self._root / "web"

    def face_status(self) -> dict:
        web = self.web
        src = newest_mtime([web / s for s in FACE_SOURCES])
        dist_index = web / "dist" / "index.html"
        built = dist_index.stat().st_mtime if dist_index.is_file() else 0.0
        return {
            "src_changed_at": src or None,
            "built_at": built or None,
            "stale": bool(src and src > built),
            "never_built": built == 0.0,
            "npm": self._npm_present,
        }

    def backend_status(self) -> dict:
        py = newest_mtime([self._root / r for r in PY_ROOTS], suffixes=(".py",))
        return {
            "started_at": self._started_at,
            "py_changed_at": py or None,
            "stale": bool(py and py > self._started_at),
        }

    def status(self) -> dict:
        return {
            "face": self.face_status(),
            "backend": self.backend_status(),
            "building": self._building,
            "last": self._last,
        }

    # ---------------------------------------------------------------- the build

    def build(self, *, timeout_s: float = 420.0) -> dict:
        """`npm run build` in web/. One at a time; a second caller is told BUSY rather than queued.
        The result (ok, seconds, the output's tail) is kept as `last` for the page."""
        if not self._npm_present:
            return {"ok": False, "error": NO_NPM}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": BUSY, "busy": True}
        self._building = True
        try:
            ran = self._run([self._npm, "run", "build"], self.web, timeout_s)
            tail = "\n".join((ran.out or "").strip().splitlines()[-40:])
            result = {
                "ok": ran.rc == 0,
                "rc": ran.rc,
                "seconds": round(ran.seconds, 1),
                "at": time.time(),
                "output": tail,
                "error": None if ran.rc == 0 else (
                    NO_NPM if ran.rc == 127 else "The build timed out." if ran.rc == 124
                    else "The build failed - the output says where."),
            }
            self._last = result
            return result
        finally:
            self._building = False
            self._lock.release()
