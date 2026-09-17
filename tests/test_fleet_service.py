"""FleetService with scripted ports: drift is judged honestly, one HEAD read per repo, nothing raises."""
from __future__ import annotations

from datetime import datetime, timezone

from helix.adapters.memory_state import MemoryFleetState
from helix.domain import constitution, fleet
from helix.domain.fleet import Drift, Env, Health, Serving
from helix.ports.fleet import CellRead, CompareRead, RepoRead
from helix.services.fleet import FleetService

CO = fleet.OATS_OVERNIGHT
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class Reader:
    def __init__(self, by_key):
        self.by_key = by_key

    def available(self):
        return True, None

    def read_cell(self, s, *, timeout_s=30.0):
        return self.by_key.get(s.key) or CellRead(service=s, ok=True, api=None, site=None)

    def read_all(self, services, *, timeout_s=90.0):
        return [self.read_cell(s) for s in services]


class Repos:
    def __init__(self, heads, compares=None, ok=True):
        self.heads, self.compares, self.ok = heads, compares or {}, ok
        self.head_calls, self.compare_calls = [], []

    def available(self):
        return True, None

    def read_head(self, repo, branch, *, timeout_s=20.0):
        self.head_calls.append((repo, branch))
        sha = self.heads.get(repo)
        if not sha:
            return RepoRead(repo=repo, branch=branch, ok=False, problem="no head")
        return RepoRead(repo=repo, branch=branch, ok=True, commit=sha[:7], full_sha=sha)

    def compare(self, repo, serving, head, *, timeout_s=20.0):
        self.compare_calls.append((repo, serving, head))
        st, behind = self.compares.get((repo, serving), ("unknown", None))
        return CompareRead(repo=repo, serving=serving, head=head, ok=True, status=st, behind_by=behind)


def api(commit=None, dirty=False, health=Health.OK, percent=100, rev="r1"):
    return Serving(revision=rev, commit=commit, dirty=dirty, health=health, traffic_percent=percent)


def svc(app, env):
    return fleet.find(app, env)


def test_protected_because_it_is_the_enforcement_point():
    assert constitution.is_protected("helix/services/fleet.py")


def test_clean_behind_and_diverged_are_judged_from_the_compare():
    mes_repo = fleet.MES_REPO
    reads = {
        svc("MES", Env.DEV).key:  CellRead(service=svc("MES", Env.DEV),  ok=True, api=api("7e02b18")),
        svc("MES", Env.QA).key:   CellRead(service=svc("MES", Env.QA),   ok=True, api=api("a41f9c2")),
        svc("MES", Env.PROD).key: CellRead(service=svc("MES", Env.PROD), ok=True, api=api("f2b8e51")),
    }
    repos = Repos({mes_repo: "a41f9c2ffffffff"},
                  {(mes_repo, "7e02b18"): ("behind", 11), (mes_repo, "a41f9c2"): ("identical", 0),
                   (mes_repo, "f2b8e51"): ("diverged", 3)})
    fs = FleetService(Reader(reads), repos, MemoryFleetState(), clock=lambda: NOW)
    cells = {c.service.env: c for c in fs.read_app(CO, "MES")}
    assert cells[Env.DEV].drift is Drift.BEHIND and cells[Env.DEV].behind_by == 11
    assert cells[Env.QA].drift is Drift.CLEAN
    assert cells[Env.PROD].drift is Drift.DIVERGED
    assert all(c.checked_at == NOW for c in cells.values())


def test_one_head_read_per_repo_not_per_cell():
    reads = {s.key: CellRead(service=s, ok=True, api=api("7e02b18")) for s in fleet.services_for("MES")}
    repos = Repos({fleet.MES_REPO: "7e02b18aaaaaaa"}, {(fleet.MES_REPO, "7e02b18"): ("identical", 0)})
    FleetService(Reader(reads), repos, MemoryFleetState(), clock=lambda: NOW).read_app(CO, "MES")
    assert repos.head_calls == [(fleet.MES_REPO, "main")]


def test_no_serving_commit_means_unknown_with_a_plain_sentence_never_clean():
    """Today's reality for every unlabelled revision (§6.6)."""
    s = svc("WMS", Env.DEV)
    reads = {s.key: CellRead(service=s, ok=True, api=api(None))}
    repos = Repos({fleet.WMS_REPO: "c9d3a70bbbbbbb"})
    (cell,) = [c for c in FleetService(Reader(reads), repos, MemoryFleetState(), clock=lambda: NOW)
               .read_app(CO, "WMS") if c.service.env is Env.DEV]
    assert cell.drift is Drift.UNKNOWN
    assert "No commit was recorded" in cell.note
    assert repos.compare_calls == [], "nothing to compare against"


def test_a_failed_read_is_a_cell_that_says_why_not_an_exception():
    s = svc("MRP", Env.QA)
    reads = {s.key: CellRead(service=s, ok=False, problem="gcloud is installed but not logged in. Run: gcloud auth login")}
    fs = FleetService(Reader(reads), Repos({}), MemoryFleetState(), clock=lambda: NOW)
    (cell,) = [c for c in fs.read_app(CO, "MRP") if c.service.env is Env.QA]
    assert cell.drift is Drift.UNKNOWN and cell.health is Health.UNKNOWN
    assert "not logged in" in cell.note


def test_absent_cells_come_back_absent_and_unread():
    fs = FleetService(Reader({}), Repos({}), MemoryFleetState(), clock=lambda: NOW)
    cells = {c.service.env: c for c in fs.read_app(CO, "ECHO")}
    assert cells[Env.PROD].health is Health.ABSENT and cells[Env.QA].health is Health.ABSENT


def test_dirty_and_split_each_earn_a_sentence():
    a, b = svc("WMS", Env.DEV), svc("WMS", Env.QA)
    reads = {a.key: CellRead(service=a, ok=True, api=api("c9d3a70", dirty=True)),
             b.key: CellRead(service=b, ok=True, api=api("c9d3a70", percent=60))}
    repos = Repos({fleet.WMS_REPO: "c9d3a70bbbbbbb"}, {(fleet.WMS_REPO, "c9d3a70"): ("identical", 0)})
    cells = {c.service.env: c for c in FleetService(Reader(reads), repos, MemoryFleetState(),
                                                    clock=lambda: NOW).read_app(CO, "WMS")}
    assert "uncommitted" in cells[Env.DEV].note
    assert "split" in cells[Env.QA].note and "60%" in cells[Env.QA].note


def test_the_read_is_published_and_snapshot_returns_it():
    s = svc("MES", Env.DEV)
    reads = {s.key: CellRead(service=s, ok=True, api=api("7e02b18"))}
    state = MemoryFleetState()
    fs = FleetService(Reader(reads), Repos({fleet.MES_REPO: "7e02b18aaaaaaa"},
                                           {(fleet.MES_REPO, "7e02b18"): ("identical", 0)}),
                      state, clock=lambda: NOW)
    assert fs.snapshot(CO) == ([], None), "nothing read yet"
    fs.read_company(CO)
    cells, at = fs.snapshot(CO)
    assert len(cells) == 12 and at == NOW
    assert state.latest(CO)[0] and len(state.latest(CO)[0]) == 12


def test_a_broken_state_store_cannot_break_the_read():
    class Broken(MemoryFleetState):
        def publish(self, company, cells):
            raise RuntimeError("firestore is down")
    fs = FleetService(Reader({}), Repos({}), Broken(), clock=lambda: NOW)
    assert len(fs.read_company(CO)) == 12


def test_on_update_is_called_once_per_read_and_cannot_break_it():
    seen = []
    def cb(company, cells):
        seen.append(len(cells)); raise RuntimeError("ui blew up")
    fs = FleetService(Reader({}), Repos({}), MemoryFleetState(), clock=lambda: NOW, on_update=cb)
    fs.read_company(CO)
    assert seen == [12]


def test_attention_lists_only_ahead_and_diverged_or_unhealthy():
    a, b, c = svc("MES", Env.DEV), svc("MES", Env.QA), svc("MES", Env.PROD)
    reads = {a.key: CellRead(service=a, ok=True, api=api("7e02b18")),
             b.key: CellRead(service=b, ok=True, api=api("f2b8e51")),
             c.key: CellRead(service=c, ok=True, api=api("a41f9c2", health=Health.DOWN))}
    repos = Repos({fleet.MES_REPO: "a41f9c2ffffffff"},
                  {(fleet.MES_REPO, "7e02b18"): ("behind", 4), (fleet.MES_REPO, "f2b8e51"): ("diverged", 2),
                   (fleet.MES_REPO, "a41f9c2"): ("identical", 0)})
    fs = FleetService(Reader(reads), repos, MemoryFleetState(), clock=lambda: NOW)
    fs.read_app(CO, "MES")
    envs = sorted(c.service.env.value for c in fs.attention(CO))
    assert envs == ["prod", "qa"], "behind is a resting state; diverged and down are not"


def test_readiness_reads_the_ports_not_a_setting():
    class R(Reader):
        def available(self): return False, "The gcloud CLI is not installed on this machine, so the fleet cannot be read."
    fs = FleetService(R({}), Repos({}), MemoryFleetState())
    rows = dict((w, (ok, why)) for w, ok, why in fs.readiness())
    assert rows["cloud"][0] is False and "gcloud" in rows["cloud"][1]
    assert rows["repos"] == (True, None)


def test_lineage_is_append_only_in_the_memory_state():
    from helix.ports.fleet import FleetEvent
    st = MemoryFleetState()
    for i in range(3):
        st.record(FleetEvent(id=str(i), at=datetime(2026, 9, 1 + i, tzinfo=timezone.utc),
                             company=CO.id, app="MES", env="dev", action="deploy", by="mark1", ok=True))
    rows = st.history(CO, "MES")
    assert [r.id for r in rows] == ["2", "1", "0"], "newest first"
    assert not hasattr(st, "delete") and not hasattr(st, "update")
