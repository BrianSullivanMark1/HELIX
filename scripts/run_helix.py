"""HELIX always-on supervisor (§39): relaunch the desktop app if it hard-crashes.

Brian runs HELIX permanently. The app already survives SOFT errors (a crash guard keeps it alive
through unhandled UI-callback exceptions, §39). A HARD crash (segfault / OOM / interpreter death)
still takes the process down — this thin, dependency-free supervisor relaunches it then.

    python scripts/run_helix.py

A CLEAN exit (you close the window) stops the supervisor. A crash (non-zero exit) relaunches it with
exponential backoff that resets after a healthy run. For launch-at-login, run
scripts/install_autostart.ps1 once (points a Startup shortcut at this supervisor)."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "main.py"
LOG = REPO_ROOT / "data" / "helix.log"

MIN_BACKOFF = 5            # seconds before the first relaunch after a crash
MAX_BACKOFF = 300          # cap the backoff at 5 minutes
HEALTHY_RUN_SECONDS = 120  # a session at least this long is "healthy" -> reset the backoff


def next_action(exit_code: int, ran_seconds: float, backoff: int) -> tuple[bool, int, int]:
    """Pure relaunch policy. Returns (stop, sleep_seconds, next_backoff).

    Clean exit (0) -> stop. A crash relaunches after `backoff`s — reset to MIN first if the crashed
    session had been running healthily — then the backoff doubles, capped at MAX. Pure so the policy
    is unit-testable without spawning processes."""
    if exit_code == 0:
        return True, 0, backoff
    sleep_seconds = MIN_BACKOFF if ran_seconds >= HEALTHY_RUN_SECONDS else backoff
    return False, sleep_seconds, min(sleep_seconds * 2, MAX_BACKOFF)


def _log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO [supervisor] {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    backoff = MIN_BACKOFF
    _log(f"supervisor starting; launching {MAIN}")
    while True:
        started = time.monotonic()
        try:
            code = subprocess.call([sys.executable, str(MAIN)], cwd=str(REPO_ROOT))
        except KeyboardInterrupt:
            _log("interrupted (Ctrl+C); stopping")
            return 0
        ran = time.monotonic() - started
        stop, sleep_seconds, backoff = next_action(code, ran, backoff)
        if stop:
            _log("HELIX exited cleanly; supervisor stopping")
            return 0
        _log(f"HELIX crashed (exit {code}) after {ran:.0f}s; relaunching in {sleep_seconds}s")
        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            _log("interrupted during backoff; stopping")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
