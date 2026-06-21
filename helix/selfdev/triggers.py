"""Crash triggers: turn logged tracebacks into self-improvement drafts (§selfdev).

`reliability.py` logs unhandled exceptions to `data/helix.log` ("Unhandled exception (app kept alive):"
followed by a Python traceback). This module parses those tracebacks, de-dups them by signature
(exception type + crash site), and drafts a fix with the coder — recorded as a PENDING change for
approval, **never auto-merged**. Pure parsing + an injectable `run` so it's unit-testable; the edge
call just reads the log file.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from helix.core.config import load_config
from helix.selfdev import coder, engine, mailer

SELFDEV_HANDLED_CRASHES_SETTING = "selfdev_handled_crashes"
SELFDEV_AUTOFIX_SETTING = "selfdev_autofix_crashes"  # default OFF (opt-in); set True to auto-draft crash fixes

# A traceback block: from the "Traceback" header to the next timestamped log line (or another
# traceback, or end of text). reliability.py writes the traceback as a multi-line message with no
# per-line timestamp, so the next "YYYY-MM-DD " line marks the end.
_TB_RE = re.compile(
    r"Traceback \(most recent call last\):.*?"
    r"(?=\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d |\nTraceback \(most recent call last\):|\Z)",
    re.DOTALL,
)


def parse_tracebacks(log_text: str) -> list[str]:
    """Extract Python traceback blocks from log text, oldest first."""
    return [m.group(0).strip() for m in _TB_RE.finditer(log_text or "")]


def signature(traceback_text: str) -> str:
    """A stable key for a traceback: exception type + last crash site, so the same bug de-dups."""
    lines = [ln for ln in (traceback_text or "").splitlines() if ln.strip()]
    exc_type = (lines[-1].split(":", 1)[0].strip() if lines else "Error")
    frames = re.findall(r'File "([^"]+)", line (\d+)', traceback_text or "")
    site = f"{frames[-1][0]}:{frames[-1][1]}" if frames else ""
    return hashlib.sha1(f"{exc_type}|{site}".encode("utf-8")).hexdigest()[:12]


def maybe_fix_crashes(
    settings: Any,
    *,
    log_path: str | None = None,
    max_fix: int = 1,
    run: Callable[..., Any] | None = None,
    repo_dir: str | None = None,
) -> list[dict]:
    """Draft fixes for NEW logged crashes. Returns a list of drafted-change records (possibly empty).

    De-dups by signature (persisted in settings) so a repeating crash is drafted once. Each draft is
    recorded as pending for approval — nothing is merged. Opt-in: OFF unless the user enables it."""
    if settings.get(SELFDEV_AUTOFIX_SETTING) is not True:
        return []
    run = run or coder.run_coding_task
    path = log_path or str(load_config().data_dir / "helix.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    handled = set(settings.get(SELFDEV_HANDLED_CRASHES_SETTING) or [])
    drafted: list[dict] = []
    for tb in parse_tracebacks(text):
        if len(drafted) >= max_fix:
            break
        sig = signature(tb)
        if sig in handled:
            continue
        handled.add(sig)  # mark handled even if drafting fails, so we never loop on the same crash
        task = (
            "HELIX logged this unhandled exception while running. Find the root cause and fix it with "
            "a minimal, correct change. Do not just swallow the error — fix what caused it.\n\n" + tb[:4000]
        )
        result = run(task, repo_dir=repo_dir)
        if result and getattr(result, "ok", False):
            rec = engine.record_pending(settings, result)
            mailer.notify_drafted(settings, rec)  # best-effort; no-op if email isn't configured
            drafted.append({"signature": sig, "branch": result.branch, "summary": result.summary})
    settings.set(SELFDEV_HANDLED_CRASHES_SETTING, sorted(handled))
    return drafted
