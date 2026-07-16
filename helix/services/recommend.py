"""RecommendService — HELIX learns which builds the user actually uses, and resurfaces the useful ones.

A tiny LOCAL usage ledger (opens + runs + last-used per build), persisted in a dedicated JSON file. From
it, `suggestions()` produces a short list to surface as a "Suggested" strip in the Menu — the builds the
user reaches for most, plus one they used before but haven't opened in a while. Privacy-local: it's just
counts of the user's own builds on this PC, no content and no network. Also seeds the Anticipate surface
later.
"""
from __future__ import annotations

import threading
from datetime import datetime

from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.stores import SettingsStore

_LOG = get_logger("recommend")

_KEY = "usage"                 # {slug: {"opens": int, "runs": int, "last": iso}}
_NEGLECT_DAYS = 14             # "used before, but not in a while"
_MAX_SUGGESTIONS = 3


class RecommendService:
    def __init__(self, store: SettingsStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()

    # ----- record -----
    def record_open(self, slug: str) -> None:
        self._bump(slug, "opens")

    def record_run(self, slug: str) -> None:
        self._bump(slug, "runs")

    def _bump(self, slug: str, field: str) -> None:
        slug = (slug or "").strip()
        if not slug:
            return
        with self._lock:
            ledger = self._ledger()
            entry = dict(ledger.get(slug) or {"opens": 0, "runs": 0, "last": ""})
            entry[field] = int(entry.get(field, 0)) + 1
            entry["last"] = self._clock.now().isoformat()
            ledger[slug] = entry
            try:
                self._store.set(_KEY, ledger)
            except Exception:  # noqa: BLE001 — a usage-write hiccup must never disturb the app
                _LOG.warning("could not save usage", exc_info=True)

    # ----- read -----
    def _ledger(self) -> dict:
        try:
            return dict(self._store.get(_KEY) or {})
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _total(entry: dict) -> int:
        return int(entry.get("opens", 0)) + int(entry.get("runs", 0))

    def suggestions(self, apps: list, limit: int = _MAX_SUGGESTIONS) -> list[tuple[object, str]]:
        """(App, reason) pairs to surface. Most-used first; then one neglected-but-previously-used build.
        `apps` is the current build list (so deleted builds drop out automatically)."""
        ledger = self._ledger()
        by_slug = {a.slug: a for a in apps}
        used = [(s, e) for s, e in ledger.items() if s in by_slug and self._total(e) > 0]
        ranked = sorted(used, key=lambda x: self._total(x[1]), reverse=True)

        out: list[tuple[object, str]] = []
        chosen: set[str] = set()
        for slug, entry in ranked[: max(0, limit - 1)]:
            out.append((by_slug[slug], f"used {self._total(entry)}×"))
            chosen.add(slug)

        neglected = self._neglected(used, by_slug, chosen)
        if neglected is not None and len(out) < limit:
            out.append(neglected)
        elif len(out) < limit:  # no neglected one — fill with the next most-used
            for slug, entry in ranked[max(0, limit - 1):]:
                if slug not in chosen:
                    out.append((by_slug[slug], f"used {self._total(entry)}×"))
                    break
        return out[:limit]

    def _neglected(self, used, by_slug, chosen) -> tuple[object, str] | None:
        now = self._clock.now()
        best = None
        best_age = -1.0
        for slug, entry in used:
            if slug in chosen:
                continue
            last = entry.get("last") or ""
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds() / 86400.0
            except (TypeError, ValueError):
                continue
            if age >= _NEGLECT_DAYS and age > best_age:
                best, best_age = slug, age
        if best is None:
            return None
        return by_slug[best], f"not opened in {int(best_age)} days"
