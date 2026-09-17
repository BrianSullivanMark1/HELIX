"""FleetReader + FleetState ports — how HELIX reads the fleet and shares what it saw.

Sits under helix/ports/, a PROTECTED_PREFIX: the nightly dream can never rewrite this seam. That is
the point. The contract HELIX reaches Google Cloud through is not part of the growable brain.

Two contracts, deliberately separate:

  FleetReader  READS the truth from wherever it lives (Cloud Run, Cloud Build, Hosting, GitHub).
               Phase 1 ships one adapter, gcloud_fleet (the gcloud CLI under the user's own login);
               Phase 5 adds rest_fleet (a read-only service account) for the CLOUD profile. Same
               port, so nothing above it moves when the second one lands.
  FleetState   SHARES what was read (Firestore): DESKTOP publishes, CLOUD reads, and every action
               from Phase 2 on appends an immutable LINEAGE row.

Like CadEngine, NOTHING here raises across the port. A missing gcloud, an expired login, a 403, a
timeout, a service that does not exist - all are ordinary outcomes carried in the result, with ONE
plain sentence a human may read and a `detail` string that exists only for the log and is never
spoken. A fleet read failing must never be able to take down the orb.

Contract: HELIX_MARK1_PLAN.md §7 (this port and its adapters), §8 (shared state), §17 (THE CULTURE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from helix.domain.fleet import Cell, Company, Serving, Service


@dataclass(frozen=True)
class CellRead:
    """The outcome of reading ONE cell. Never raised, always returned.

    `problem` is the ONE warm sentence the console may show (no paths, no stderr, ASCII - rule 7).
    `detail` is the tool's own words, for the log only. They are separate fields precisely so a caller
    cannot leak one where the other belongs - the same split CadResult makes, for the same reason.
    """

    service: Service
    ok: bool
    api: Serving | None = None
    site: Serving | None = None
    served_revisions: tuple[str, ...] = ()   # newest first; the live rollback targets (§6.5)
    problem: str | None = None
    detail: str | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class RepoRead:
    """HEAD of one repo's branch, for the drift half of a cell."""

    repo: str
    branch: str
    ok: bool
    commit: str | None = None            # short SHA
    full_sha: str | None = None
    subject: str | None = None
    committed_at: datetime | None = None
    problem: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CompareRead:
    """How `serving` relates to `head` in one repo's history. GitHub's compare API answers this in one
    call, which is why it is a port verb rather than something the service reconstructs from logs.

    `status` is one of "identical" | "behind" | "ahead" | "diverged" | "unknown", from the point of
    view of the SERVING commit: "behind" = serving is an ancestor of head (ordinary un-deployed work)."""

    repo: str
    serving: str
    head: str
    ok: bool
    status: str = "unknown"
    behind_by: int | None = None         # commits head has that serving does not
    ahead_by: int | None = None          # commits serving has that head does not
    problem: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FleetEvent:
    """One LINEAGE row. Append-only, never edited, never deleted (§8.3)."""

    id: str
    at: datetime
    company: str
    app: str
    env: str
    action: str            # "deploy" | "rollback" | "read" | "grant" | ...
    by: str
    ok: bool
    commit: str | None = None
    revision: str | None = None
    from_revision: str | None = None
    note: str = ""


class FleetReader(Protocol):
    def available(self) -> tuple[bool, str | None]:
        """(usable?, one plain sentence why not). CHEAP - no network, no subprocess beyond a cached
        version probe - so the page can pre-flight before spending a real read. A missing CLI must
        never be reported as a credentials problem; the sentence names which it actually is."""
        ...

    def read_cell(self, service: Service, *, timeout_s: float = 30.0) -> CellRead:
        """Everything Cloud Run + Hosting know about one cell right now."""
        ...

    def read_all(self, services: Sequence[Service], *, timeout_s: float = 90.0) -> list[CellRead]:
        """All of them, in parallel where the adapter can; one CellRead per input, same order,
        absent cells included as ok=True with both halves None."""
        ...


class RepoReader(Protocol):
    """The git HOST (GitHub today, GitLab pluggable - §17.10). Reads only; pushes stay local."""

    def available(self) -> tuple[bool, str | None]:
        ...

    def read_head(self, repo: str, branch: str, *, timeout_s: float = 20.0) -> RepoRead:
        """HEAD of `branch` on `repo` ('owner/name')."""
        ...

    def compare(self, repo: str, serving: str, head: str, *, timeout_s: float = 20.0) -> CompareRead:
        """How `serving` sits relative to `head`. The drift classification in one call."""
        ...


class FleetState(Protocol):
    def publish(self, company: Company, cells: Sequence[Cell]) -> None:
        """Replace the company's current grid. DESKTOP only."""
        ...

    def latest(self, company: Company) -> tuple[list[Cell], datetime | None]:
        """The last published grid and when. ([], None) when nothing has ever been published."""
        ...

    def record(self, event: FleetEvent) -> None:
        """Append one LINEAGE row. Must never update or delete an existing one."""
        ...

    def history(self, company: Company, app: str | None = None, *, limit: int = 50) -> list[FleetEvent]:
        """Newest first."""
        ...
