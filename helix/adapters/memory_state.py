"""MemoryFleetState — FleetState kept in this process. The Phase 1 stand-in until Firestore lands
(HELIX_MARK1_PLAN.md §8), and the fake every test uses. LINEAGE rows are append-only here too: the
list is only ever extended."""
from __future__ import annotations

import threading
from datetime import datetime

from helix.domain.fleet import Cell, Company
from helix.ports.fleet import FleetEvent


class MemoryFleetState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, tuple[list[Cell], datetime]] = {}
        self._events: list[FleetEvent] = []

    def publish(self, company: Company, cells) -> None:
        from datetime import timezone
        with self._lock:
            self._current[company.id] = (list(cells), datetime.now(timezone.utc))

    def latest(self, company: Company):
        with self._lock:
            got = self._current.get(company.id)
        return (list(got[0]), got[1]) if got else ([], None)

    def record(self, event: FleetEvent) -> None:
        with self._lock:
            self._events.append(event)

    def history(self, company: Company, app: str | None = None, *, limit: int = 50):
        with self._lock:
            rows = [e for e in self._events if e.company == company.id and (app is None or e.app == app)]
        rows.sort(key=lambda e: e.at, reverse=True)
        return rows[:limit]
