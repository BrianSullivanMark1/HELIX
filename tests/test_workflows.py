"""WorkflowService — chaining agents into an ordered pipeline that passes each result to the next."""
from __future__ import annotations

from datetime import datetime

from helix.services.workflows import WorkflowService


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Agents:
    def __init__(self, names):
        self._names = {n.lower() for n in names}
        self.calls = []

    def exists(self, name):
        return name.strip().lower() in self._names

    def run(self, name, on_progress=None, context=None):
        self.calls.append((name, context or ""))
        return f"output of {name}"


class _Clock:
    def now(self):
        return datetime(2026, 7, 14, 9, 0, 0).astimezone()


def _svc(agents):
    return WorkflowService(_Store(), agents, clock=_Clock())


def test_add_list_and_find():
    s = _svc(_Agents(["a", "b"]))
    wf = s.add("pipe", ["a", "b"])
    assert wf.steps == ["a", "b"]
    assert s.exists("pipe") and s.find("PIPE").steps == ["a", "b"]
    assert [w.name for w in s.list()] == ["pipe"]


def test_run_chains_output_into_the_next_step():
    agents = _Agents(["research", "draft"])
    s = _svc(agents)
    s.add("pipe", ["research", "draft"])
    out = s.run("pipe")
    assert out == "output of draft"                     # the pipeline's final result
    assert agents.calls[0] == ("research", "")          # first step gets no prior context
    assert agents.calls[1][0] == "draft"
    assert "output of research" in agents.calls[1][1]    # second step receives the first's output (fenced)


def test_run_flags_a_missing_step_agent():
    s = _svc(_Agents(["research"]))
    s.add("pipe", ["research", "ghost"])
    out = s.run("pipe")
    assert "ghost" in out and "doesn't" in out


def test_empty_workflow_reports_gracefully():
    s = _svc(_Agents([]))
    s.add("pipe", [])
    assert "no steps" in s.run("pipe")


def test_rename_remove_and_pause():
    s = _svc(_Agents(["a"]))
    s.add("pipe", ["a"])
    assert s.set_enabled("pipe", False).enabled is False
    assert s.rename("pipe", "flow").name == "flow"
    assert s.remove("flow")
    assert not s.exists("flow")


def test_retune_keeps_pause_state():
    s = _svc(_Agents(["a", "b"]))
    s.add("pipe", ["a"])
    s.set_enabled("pipe", False)
    s.add("pipe", ["a", "b"])  # update the steps
    assert s.find("pipe").enabled is False and s.find("pipe").steps == ["a", "b"]
