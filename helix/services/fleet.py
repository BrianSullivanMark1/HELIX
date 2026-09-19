"""FleetService — the use case behind THE STRAND and the board: read every cell, work out its drift,
share what was seen. Phase 1: reads only. No verb in this file changes a service.

In PROTECTED_FILES (HELIX_MARK1_PLAN.md §10.1) because this is where the fleet's laws are ENFORCED, as
selfdev.py is for the Constitution. The laws themselves live in domain/fleet.py; this file is the one
that calls them, so it is fixed to the dream for the same reason they are.

It knows three ports and nothing about gcloud, GitHub, Firestore, FastAPI or React:

  FleetReader  what is actually running (Cloud Run)         - gcloud_fleet today
  RepoReader   what the repo says should be running (git)   - github_fleet today
  FleetState   where the answer is shared                   - memory today, Firestore next

Nothing here raises. A reader that fails produces a Cell that says UNKNOWN and carries the reader's
one sentence as its note; the orb keeps breathing.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Sequence

from helix.domain.fleet import (
    Cell, Company, Drift, Health, Service, drift_from, services_for, FLEET,
)
from helix.ports.fleet import CellRead, CompareRead, FleetReader, FleetState, RepoRead, RepoReader

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FleetService:
    def __init__(self, reader: FleetReader, repos: RepoReader, state: FleetState, *,
                 clock: Clock | None = None,
                 on_update: Callable[[Company, list[Cell]], None] | None = None) -> None:
        self._reader = reader
        self._repos = repos
        self._state = state
        self._clock = clock or _utcnow
        self._on_update = on_update
        self._lock = threading.Lock()
        self._last: dict[str, tuple[list[Cell], datetime]] = {}

    # ---------------------------------------------------------------- pre-flight

    def readiness(self) -> list[tuple[str, bool, str | None]]:
        """[(what, ok, why-not)] for VITALS. Reads the artefacts (the CLI, the token), never a
        setting that was supposed to produce them (§17.6 rule 1)."""
        ok_r, why_r = self._reader.available()
        ok_g, why_g = self._repos.available()
        return [("cloud", ok_r, why_r), ("repos", ok_g, why_g)]

    # ---------------------------------------------------------------- the read

    def read_company(self, company: Company, *, timeout_s: float = 90.0) -> list[Cell]:
        services = [s for s in FLEET if s.company == company.id]
        return self.read_services(company, services, timeout_s=timeout_s)

    def read_app(self, company: Company, app: str, *, timeout_s: float = 45.0) -> list[Cell]:
        services = [s for s in services_for(app) if s.company == company.id]
        return self.read_services(company, services, timeout_s=timeout_s)

    def read_services(self, company: Company, services: Sequence[Service], *,
                      timeout_s: float = 90.0) -> list[Cell]:
        now = self._clock()
        reads = self._reader.read_all(list(services), timeout_s=timeout_s)

        # One HEAD read per (repo, branch), not per cell: three environments of one app share a repo.
        heads: dict[tuple[str, str], RepoRead] = {}
        for svc in services:
            key = (svc.repo, svc.branch)
            if key not in heads and svc.exists:
                heads[key] = self._repos.read_head(svc.repo, svc.branch)

        cells = [self._cell(r, heads.get((r.service.repo, r.service.branch)), now) for r in reads]

        with self._lock:
            self._last[company.id] = (cells, now)
        try:
            self._state.publish(company, cells)
        except Exception:  # noqa: BLE001 - sharing is best-effort; the read already happened
            pass
        if self._on_update is not None:
            try:
                self._on_update(company, cells)
            except Exception:  # noqa: BLE001
                pass
        return cells

    def _cell(self, r: CellRead, head: RepoRead | None, now: datetime) -> Cell:
        svc = r.service
        if not svc.exists:
            return Cell(service=svc, drift=Drift.UNKNOWN, checked_at=now)
        if not r.ok:
            return Cell(service=svc, api=None, site=None, drift=Drift.UNKNOWN, checked_at=now,
                        note=r.problem)

        repo_commit = head.commit if (head and head.ok) else None
        serving = r.api.commit if r.api else None
        behind_by: int | None = None
        in_history: bool | None = None

        if repo_commit and serving and head and head.ok:
            cmp = self._repos.compare(svc.repo, serving, head.full_sha or head.commit or "")
            if cmp.ok:
                behind_by = cmp.behind_by
                in_history = {"identical": True, "behind": True, "ahead": False,
                              "diverged": False}.get(cmp.status)
                if cmp.status == "identical":
                    repo_commit = serving  # let drift_from see equality even if one side is longer

        drift = drift_from(repo_commit, serving, behind_by=behind_by, in_history=in_history)
        note = self._note(r, head, drift)
        return Cell(service=svc, api=r.api, site=r.site, repo_commit=repo_commit, drift=drift,
                    behind_by=behind_by, checked_at=now, note=note)

    @staticmethod
    def _note(r: CellRead, head: RepoRead | None, drift: Drift) -> str | None:
        """ONE plain sentence, or none. The first thing that is odd wins; the rest is in the log."""
        if r.problem:
            return r.problem                       # e.g. the table/Cloud Run disagreement
        if r.api and r.api.health is Health.ABSENT:
            return None
        if head is not None and not head.ok:
            return head.problem
        if r.api and r.api.commit is None:
            return "No commit was recorded for this revision, so drift cannot be judged."
        if r.api and r.api.dirty:
            return "Built from a working tree with uncommitted changes - not reproducible from the repo."
        if r.api and r.api.is_split:
            return f"Traffic is split: this revision carries {r.api.traffic_percent}%."
        return None

    # ---------------------------------------------------------------- what we last saw

    def snapshot(self, company: Company) -> tuple[list[Cell], datetime | None]:
        with self._lock:
            got = self._last.get(company.id)
        if got:
            return got
        try:
            return self._state.latest(company)
        except Exception:  # noqa: BLE001
            return [], None

    def attention(self, company: Company) -> list[Cell]:
        cells, _ = self.snapshot(company)
        return [c for c in cells if c.needs_attention]

    def history(self, company: Company, app: str | None = None, *, limit: int = 50):
        """What was done to this company's fleet, newest first - the state port's ledger. Empty
        rather than raising when the state is unreachable: the board still renders."""
        try:
            return list(self._state.history(company, app, limit=max(1, min(500, int(limit)))))
        except Exception:  # noqa: BLE001
            return []
