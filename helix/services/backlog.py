"""The improvement BACKLOG — the user's queued ideas for HELIX's own code, and the night's material.

Until 2026-09-05 this lived inside the overnight Evolve pass (one proposal a night, always
human-approved). Evolve is gone: the DREAM SESSION (services/dream.py, READ_ME/DREAM.md) is the one
way HELIX improves itself at night, and it needed exactly two things Evolve owned — the queue of ideas
the user asked for on purpose (note_improvement, human-driven only) and the labelled material every
night's REFLECT mines first (that queue, the standing lessons the user taught HELIX, the tail of the
log). Both live here now, in one small service two readers share: the dream mind reads `material()`
and queues research-found ideas with `add()`; the session crosses a drafted idea off with `take()`.

The store is data/helix_backlog.json (on config.VOLATILE_STORE_NAMES: the night writes it while its
own coder draft runs, so both escape guards must skip it). A backlog left behind by Evolve
(evolve_backlog.json) is folded in once, on first use, and the old file removed.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("backlog")

BACKLOG_FILE = "helix_backlog.json"       # {"version": 1, "items": [...]}
LEGACY_FILE = "evolve_backlog.json"       # Evolve's queue, folded in once and removed
_CAP = 20                                 # ideas kept; older ones age out rather than pile up forever
_ITEM_CAP = 400                           # chars per idea
_TAIL_LINES = 80                          # how much of helix.log the material reads
_TAIL_CAP = 8_000                         # chars — a runaway log line never bloats the prompt


def default_log_tail() -> str:
    """The last ~80 lines of helix.log, found lazily via the live logger's file handler — so the
    service needs no path plumbing and follows wherever setup_logging pointed the log."""
    try:
        for h in logging.getLogger("helix").handlers:
            path = getattr(h, "baseFilename", None)
            if path:
                lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[-_TAIL_LINES:])
    except Exception:  # noqa: BLE001 — no log is just an empty material section
        pass
    return ""


class Backlog:
    def __init__(self, data_dir: Path | None, lessons=None,
                 log_tail: Callable[[], str] | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._lessons = lessons
        self._log_tail = log_tail or default_log_tail
        self._lock = threading.Lock()
        self._migrated = False

    # ----- the queue -----
    def add(self, text: str) -> bool:
        """Queue one improvement idea (the note_improvement tool — human-driven — and the dream
        mind's research-found ideas). Deduped case-insensitively, capped: past the cap the OLDEST
        idea ages out, because a list that only grows stops being a queue and starts being a
        graveyard. False when there is nothing to queue or nowhere to keep it."""
        text = " ".join((text or "").split())[:_ITEM_CAP]
        if not text or self._data_dir is None:
            return False
        with self._lock:
            items = self._read()
            if any(text.lower() == it.lower() for it in items):
                return True  # already queued — count it as done, don't duplicate
            items.append(text)
            self._write(items[-_CAP:])
        return True

    def items(self) -> list[str]:
        with self._lock:
            return self._read()

    def take(self, item: str) -> None:
        """Cross a drafted idea off the queue (matched loosely — the model quotes it back)."""
        if not item or self._data_dir is None:
            return
        want = " ".join(item.split()).lower()
        with self._lock:
            items = self._read()
            kept = [it for it in items if " ".join(it.split()).lower() != want]
            if len(kept) != len(items):
                self._write(kept)

    # ----- the night's material -----
    def material(self) -> str:
        """What the day produced, labelled: the backlog, every speaker's lessons, the log tail."""
        rules: list[str] = []
        if self._lessons is not None:
            try:
                # The lessons store is {user: [rules]}; the internal accessors already normalize +
                # cap, so read through them rather than re-parsing the raw store here.
                for user in sorted(self._lessons._all()):
                    who = user or "default"
                    rules.extend(f"[{who}] {r}" for r in self._lessons._rules(user))
            except Exception:  # noqa: BLE001 — no lessons is just an empty material section
                _LOG.warning("backlog: could not read lessons", exc_info=True)
        try:
            tail = (self._log_tail() or "").strip()[:_TAIL_CAP]
        except Exception:  # noqa: BLE001
            tail = ""
        queued = self.items()
        return (
            "IMPROVEMENT BACKLOG (ideas the user queued on purpose, via the human-driven "
            "note_improvement tool — prefer an actionable one of these):\n"
            + ("\n".join(f"- {it}" for it in queued) if queued else "(empty)")
            + "\n\nLESSONS (standing corrections the user has taught HELIX):\n"
            + ("\n".join(rules) if rules else "(none)")
            + "\n\nLOG TAIL (the last lines of helix.log):\n"
            + (tail or "(empty)")
        )

    # ----- the file -----
    def _path(self) -> Path:
        return self._data_dir / BACKLOG_FILE  # type: ignore[operator]

    def _read(self) -> list[str]:
        if self._data_dir is None:
            return []
        self._migrate_once()
        try:
            data = json.loads(self._path().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        raw = data.get("items") if isinstance(data, dict) else data
        return [str(x) for x in raw if str(x).strip()] if isinstance(raw, list) else []

    def _write(self, items: list[str]) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            _LOG.warning("could not write the improvement backlog", exc_info=True)

    def _migrate_once(self) -> None:
        """Fold Evolve's evolve_backlog.json into this store, once, and remove it. Ideas already
        here win; the legacy file's are appended in their order. Any trouble leaves both as they
        are — a migration must never lose an idea."""
        if self._migrated or self._data_dir is None:
            return
        self._migrated = True
        legacy = self._data_dir / LEGACY_FILE
        if not legacy.is_file():
            return
        try:
            old = json.loads(legacy.read_text(encoding="utf-8-sig"))
            old_items = [str(x) for x in old if str(x).strip()] if isinstance(old, list) else []
            current: list[str] = []
            if self._path().is_file():
                data = json.loads(self._path().read_text(encoding="utf-8-sig"))
                raw = data.get("items") if isinstance(data, dict) else data
                current = [str(x) for x in raw if str(x).strip()] if isinstance(raw, list) else []
            seen = {it.lower() for it in current}
            merged = current + [it for it in old_items if it.lower() not in seen]
            self._write(merged[-_CAP:])
            if self._path().is_file():
                legacy.unlink()
                _LOG.info("backlog: folded %d idea(s) from Evolve's queue into %s", len(old_items), BACKLOG_FILE)
        except Exception:  # noqa: BLE001
            _LOG.warning("backlog: could not fold Evolve's queue in; leaving both files", exc_info=True)


__all__ = ["BACKLOG_FILE", "Backlog", "LEGACY_FILE", "default_log_tail"]
