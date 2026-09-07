"""ToolRegistry lifecycle/safety tools — rename_build, run_task, run_agent, and the delete-confirm gate."""
from __future__ import annotations

from helix.domain.events import BuildDeleteRequested, BuildRenamed
from helix.domain.models import BuildKind
from helix.services.conversation import BUILD_TOOLS
from helix.services.tools import ToolRegistry


class _B:
    def __init__(self, name, slug=None, kind=BuildKind.APP, request="desc"):
        self.name = name
        self.slug = slug or name.lower().replace(" ", "-")
        self.build_kind = kind
        self.request = request


class _FakeBuilds:
    def __init__(self, items=()):
        self._items = list(items)

    def list(self):
        return list(self._items)

    def rename(self, slug, new_name):
        for b in self._items:
            if b.slug == slug:
                b.name = new_name
                return b
        return None


class _FakeForge:
    def __init__(self, removable=()):
        self._removable = {n.lower() for n in removable}
        self.removed = []

    def remove_build(self, name):
        if name.strip().lower() in self._removable:
            self.removed.append(name)
            return True
        return False


class _FakeBus:
    def __init__(self):
        self.published = []

    def publish(self, e):
        self.published.append(e)

    def subscribe(self, *a):
        pass


class _FakeTasks:
    def __init__(self, found=True, ok=True):
        self._found = found
        self._ok = ok
        self.ran = []

    def find(self, name):
        return _B(name, kind=BuildKind.TASK) if self._found else None

    def run(self, slug):
        self.ran.append(slug)
        return self._ok


class _FakeAgents:
    def __init__(self, existing=()):
        self._e = list(existing)
        self.removed = []
        self.renamed = []
        self.ran = []

    def list(self):
        return [_B(n) for n in self._e]

    def remove(self, n):
        self.removed.append(n)

    def rename(self, old, new):
        self.renamed.append((old, new))
        return _B(new)

    def run(self, name, on_progress=None):
        self.ran.append(name)
        return f"ran {name}"

    def add(self, n, g):
        return _B(n)


def test_rename_build_renames_a_build_and_publishes_event():
    bus = _FakeBus()
    reg = ToolRegistry(_FakeForge(), _FakeBuilds([_B("Tip Calc")]), bus=bus)
    out = reg.dispatch("rename_build", {"name": "Tip Calc", "new_name": "Gratuity"})
    assert "Renamed" in out and "Gratuity" in out
    renamed = [e for e in bus.published if isinstance(e, BuildRenamed)]
    assert renamed and renamed[0].old_slug == "tip-calc"  # old slug rides along so an open viewer re-points


def test_rename_build_falls_through_to_an_agent():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), bus=_FakeBus())
    agents = _FakeAgents(existing=["Morning Brief"])
    reg.bind_agents(agents)
    out = reg.dispatch("rename_build", {"name": "morning brief", "new_name": "Daily Brief"})
    assert agents.renamed == [("Morning Brief", "Daily Brief")] and "Daily Brief" in out


def test_delete_build_requests_confirmation_and_removes_nothing():
    bus = _FakeBus()
    forge = _FakeForge(removable=["Tip Calc"])
    reg = ToolRegistry(forge, _FakeBuilds([_B("Tip Calc")]), bus=bus)
    out = reg.dispatch("delete_build", {"name": "Tip Calc"})
    assert "confirm" in out.lower()
    assert any(isinstance(e, BuildDeleteRequested) for e in bus.published)
    assert forge.removed == []  # NOTHING is removed from the model loop — only after a human click


def test_confirm_delete_actually_removes():
    forge = _FakeForge(removable=["Tip Calc"])
    reg = ToolRegistry(forge, _FakeBuilds([_B("Tip Calc")]), bus=_FakeBus())
    out = reg.confirm_delete("Tip Calc")
    assert forge.removed == ["Tip Calc"] and "Removed" in out


def test_delete_unknown_name_is_honest():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), bus=_FakeBus())
    out = reg.dispatch("delete_build", {"name": "nope"})
    assert "couldn't find" in out.lower()


def test_run_task_launches_by_name():
    tasks = _FakeTasks(found=True, ok=True)
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), tasks=tasks)
    out = reg.dispatch("run_task", {"name": "Rename Downloads"})
    assert tasks.ran and "Running" in out


def test_run_task_missing_is_honest():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), tasks=_FakeTasks(found=False))
    out = reg.dispatch("run_task", {"name": "ghost"})
    assert "don't see" in out.lower()


def test_run_agent_runs_and_relays():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds())
    agents = _FakeAgents(existing=["Morning Brief"])
    reg.bind_agents(agents)
    out = reg.dispatch("run_agent", {"name": "Morning Brief"})
    assert agents.ran == ["Morning Brief"] and "ran Morning Brief" in out


def test_lifecycle_tools_are_exposed_when_wired():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), tasks=_FakeTasks(), bus=_FakeBus())
    reg.bind_agents(_FakeAgents())
    names = {t.name for t in reg.specs()}
    assert {"rename_build", "run_task", "run_agent"} <= names


def test_autonomous_agent_runs_cannot_rename_run_or_delete():
    # These have side effects, so an autonomous agent run must be denied them (defense for run_agent loops).
    assert {"rename_build", "run_task", "run_agent", "delete_build"} <= BUILD_TOOLS


class _PC:
    def __init__(self, id, summary="did x"):
        self.id = id
        self.summary = summary


class _FakeSelfdev:
    def __init__(self, pend=()):
        self._pend = list(pend)
        self.approved = []
        self.rejected = []

    def pending(self):
        return list(self._pend)

    def approve(self, id):
        self.approved.append(id)
        return "Applied. Restart HELIX to load the new version."

    def reject(self, id):
        self.rejected.append(id)
        self._pend = [p for p in self._pend if p.id != id]


def test_self_change_tools_are_exposed():
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=_FakeSelfdev())
    names = {t.name for t in reg.specs()}
    assert {"improve_helix", "list_self_changes", "approve_self_change", "reject_self_change"} <= names


def test_approve_single_pending_change_without_naming_it():
    sd = _FakeSelfdev([_PC("selfdev/x-1")])
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=sd)
    out = reg.dispatch("approve_self_change", {})
    assert sd.approved == ["selfdev/x-1"] and "Applied" in out


def test_approve_is_ambiguous_when_several_pending():
    sd = _FakeSelfdev([_PC("selfdev/a"), _PC("selfdev/b")])
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=sd)
    out = reg.dispatch("approve_self_change", {})
    assert sd.approved == [] and "Which" in out  # never guess which code change to merge


def test_reject_self_change_by_name():
    sd = _FakeSelfdev([_PC("selfdev/a"), _PC("selfdev/b")])
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=sd)
    out = reg.dispatch("reject_self_change", {"which": "selfdev/a"})
    assert sd.rejected == ["selfdev/a"] and "Discarded" in out


def test_list_self_changes_reports_pending():
    sd = _FakeSelfdev([_PC("selfdev/a", "added a button")])
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=sd)
    out = reg.dispatch("list_self_changes", {})
    assert "selfdev/a" in out and "added a button" in out


def test_self_change_approve_reject_are_denied_to_agents():
    assert {"approve_self_change", "reject_self_change", "improve_helix"} <= BUILD_TOOLS


class _FakeLane:
    def __init__(self, busy=False):
        self._busy = busy
        self.started = []

    def busy(self):
        return self._busy

    def start(self, request):
        self.started.append(request)
        return True


def test_improve_helix_drafts_on_the_background_lane():
    lane = _FakeLane()
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=_FakeSelfdev(), selfdev_lane=lane)
    out = reg.dispatch("improve_helix", {"request": "make the orb bluer"})
    assert lane.started == ["make the orb bluer"]  # enqueued, not run inline (no orb freeze)
    assert "background" in out.lower()


def test_improve_helix_is_one_draft_at_a_time():
    lane = _FakeLane(busy=True)
    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), selfdev=_FakeSelfdev(), selfdev_lane=lane)
    out = reg.dispatch("improve_helix", {"request": "x"})
    assert lane.started == [] and "one at a time" in out.lower()
