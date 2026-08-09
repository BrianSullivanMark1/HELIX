"""ToolRegistry tests — build tools ENQUEUE on the background BuildQueue (with the right kind/prompt),
and the queue-status tools (list_builds / prioritize_build / cancel_build) route correctly."""
from __future__ import annotations

from helix.domain.models import BuildKind
from helix.services.tools import ToolRegistry


class _App:
    def __init__(self, name: str) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")


class _FakeForge:
    def remove_build(self, name) -> bool:
        return False  # no workspace build by that name — let the agent path handle deletes in tests


class _Build:
    """Minimal stand-in for a workspace Build — the fields open_build actually reads."""

    def __init__(self, name: str, kind: BuildKind) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")
        self.build_kind = kind


class _FakeBuilds:
    def __init__(self, *builds: _Build) -> None:
        self._builds = list(builds)

    def list(self):
        return list(self._builds)


class _FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)


class _FakeQueue:
    """Records enqueue() and the control calls so we can assert how a tool routed."""

    def __init__(self, ahead: int = 0, active=None, queued=()) -> None:
        self.enqueued: list[dict] = []
        self._ahead = ahead
        self._active = active
        self._queued = list(queued)
        self.moved: list[str] = []
        self.cancelled_queued: list[str] = []
        self.cancelled_active = 0

    def enqueue(self, name, request, *, kind, prompt=None):
        self.enqueued.append({"name": name, "request": request, "kind": kind, "prompt": prompt})
        return self._ahead

    def status_line(self):
        return f"Building now: {self._active}." if self._active else "Nothing building right now."

    def active_name(self):
        return self._active

    def move_first(self, name):
        if name in self._queued:
            self.moved.append(name)
            return True
        return False

    def cancel_queued(self, name):
        if name in self._queued:
            self.cancelled_queued.append(name)
            return True
        return False

    def cancel_active(self):
        self.cancelled_active += 1
        return [self._active] if self._active else []

    def is_active_named(self, name):
        return self._active is not None and self._active.lower() == name.strip().lower()

    def cancel_active_named(self, name):
        if self.is_active_named(name):
            self.cancelled_active += 1
            return True
        return False


class _FakeAgents:
    def __init__(self, existing=()) -> None:
        self.added: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self._existing = list(existing)

    def add(self, name, goal, schedule_hint=None):
        self.added.append((name, goal))
        agent = _App(name)
        agent.schedule = None  # manual agent — dispatch reads this to phrase the acknowledgement
        return agent

    def list(self):
        return [_App(n) for n in self._existing]

    def exists(self, name) -> bool:
        return any(a.name.strip().lower() == name.strip().lower() for a in self.list())

    def remove(self, name) -> None:
        self.removed.append(name)


def _registry(ahead: int = 0, active=None, queued=()):
    queue = _FakeQueue(ahead=ahead, active=active, queued=queued)
    return ToolRegistry(_FakeForge(), _FakeBuilds(), queue=queue), queue


def test_build_tools_are_exposed():
    reg, _ = _registry()
    names = {t.name for t in reg.specs()}
    assert {"build_app", "build_task", "build_3d_model", "list_builds"} <= names


def test_build_3d_model_enqueues_with_no_prompt_and_model_kind():
    # The registry no longer precomputes prompts: the Forge picks build_* vs edit_* itself once it
    # knows whether the name resolves to an existing build (the edit-trust fix).
    reg, queue = _registry()
    out = reg.dispatch("build_3d_model", {"name": "Wall Camera Unit", "request": "camera, speaker and mic"})
    assert len(queue.enqueued) == 1
    job = queue.enqueued[0]
    assert job["name"] == "Wall Camera Unit"
    assert job["kind"] == BuildKind.MODEL
    assert job["prompt"] is None
    assert "Starting" in out and "hologram" in out  # fast acknowledgement, not "Built"


def test_build_app_enqueues_with_no_prompt_and_app_kind():
    reg, queue = _registry()
    out = reg.dispatch("build_app", {"name": "Tip Calc", "request": "a tip calculator"})
    assert queue.enqueued[0]["prompt"] is None
    assert queue.enqueued[0]["kind"] == BuildKind.APP
    assert "Starting" in out


def test_build_task_enqueues_with_no_prompt_and_task_kind():
    reg, queue = _registry()
    out = reg.dispatch("build_task", {"name": "Rename Downloads", "request": "tidy my downloads"})
    job = queue.enqueued[0]
    assert job["kind"] == BuildKind.TASK
    assert job["prompt"] is None  # the Forge picks build_task_prompt vs edit_task_prompt itself
    assert "protocol" in out.lower()  # the V3 label (the build_task tool name is unchanged)


def test_second_build_while_one_runs_is_queued_not_started():
    reg, _ = _registry(ahead=1)  # something already building
    out = reg.dispatch("build_app", {"name": "Habit Tracker", "request": "track habits"})
    assert "Queued" in out and "Habit Tracker" in out


def test_list_builds_is_read_only_status():
    reg, _ = _registry(active="Tip Calculator")
    out = reg.dispatch("list_builds", {})
    assert "Tip Calculator" in out  # reports status, starts/stops nothing


def test_prioritize_moves_a_queued_build():
    reg, queue = _registry(active="Weather", queued=["To-Do List"])
    out = reg.dispatch("prioritize_build", {"name": "To-Do List"})
    assert queue.moved == ["To-Do List"] and "next" in out.lower()


def test_prioritize_cannot_reorder_the_running_build():
    reg, _ = _registry(active="Weather", queued=[])
    out = reg.dispatch("prioritize_build", {"name": "Weather"})
    assert "already building" in out.lower()


def test_cancel_build_drops_a_queued_one():
    reg, queue = _registry(active="Weather", queued=["To-Do List"])
    out = reg.dispatch("cancel_build", {"name": "To-Do List"})
    assert queue.cancelled_queued == ["To-Do List"] and "Dropped" in out


def test_cancel_build_stops_the_active_one():
    reg, queue = _registry(active="Weather", queued=[])
    out = reg.dispatch("cancel_build", {"name": "Weather"})
    assert queue.cancelled_active == 1 and "Stopping" in out


def test_create_agent_is_exposed_only_once_agents_are_bound():
    reg, _ = _registry()
    assert "create_agent" not in {t.name for t in reg.specs()}
    reg.bind_agents(_FakeAgents())
    assert "create_agent" in {t.name for t in reg.specs()}


def test_create_agent_routes_to_the_agent_store():
    reg, _ = _registry()
    agents = _FakeAgents()
    reg.bind_agents(agents)
    out = reg.dispatch("create_agent", {"name": "Morning Brief", "goal": "summarize my day"})
    assert agents.added == [("Morning Brief", "summarize my day")]
    assert "Morning Brief" in out


def test_delete_build_falls_through_to_a_matching_agent():
    reg, _ = _registry()  # _FakeForge.remove_build returns False, so the agent path handles it
    agents = _FakeAgents(existing=["Morning Brief"])
    reg.bind_agents(agents)
    out = reg.dispatch("delete_build", {"name": "morning brief"})
    assert agents.removed == ["Morning Brief"] and "Morning Brief" in out


def test_think_harder_routes_to_the_deep_thinker_when_wired():
    asked: list[str] = []

    def deep(question, on_progress=None, cancel=None):
        asked.append(question)
        return "a carefully reasoned answer"

    reg = ToolRegistry(_FakeForge(), _FakeBuilds(), deep_think=deep, queue=_FakeQueue())
    assert "think_harder" in {t.name for t in reg.specs()}
    out = reg.dispatch("think_harder", {"question": "why is the sky blue?"})
    assert asked == ["why is the sky blue?"] and out == "a carefully reasoned answer"


def test_think_harder_absent_without_a_deep_thinker():
    reg, _ = _registry()
    assert "think_harder" not in {t.name for t in reg.specs()}


# ---------- open_build: OPEN means open, never run ----------

def _open_registry(*builds: _Build, bus=None):
    return ToolRegistry(_FakeForge(), _FakeBuilds(*builds), queue=_FakeQueue(), bus=bus)


def test_open_build_promises_run_task_for_protocols():
    # The contract the implementation below has to keep: the tool the model is shown says a protocol
    # that should DO its thing goes through run_task.
    reg = _open_registry()
    spec = next(t for t in reg.specs() if t.name == "open_build")
    assert "run_task" in spec.description


def test_open_build_refuses_a_protocol_instead_of_running_it():
    # Opening a protocol used to publish an open request, which the shell services by launching the
    # build's main.py headlessly — so "show me the tidy-downloads protocol" silently RAN it, the exact
    # capability open_build's description hands to run_task.
    bus = _FakeBus()
    reg = _open_registry(_Build("Tidy Downloads", BuildKind.TASK), bus=bus)
    out = reg.dispatch("open_build", {"name": "tidy downloads"})
    assert bus.published == []  # nothing was asked to open — and therefore nothing was run
    assert "protocol" in out and "run_task" in out


def test_open_build_refuses_a_protocol_headlessly_too():
    # With no bus (agent/test context) the refusal must still come first — not fall through to a launch.
    reg = _open_registry(_Build("Tidy Downloads", BuildKind.TASK))
    assert "run_task" in reg.dispatch("open_build", {"name": "Tidy Downloads"})


def test_open_build_still_opens_an_app():
    bus = _FakeBus()
    reg = _open_registry(_Build("Tip Calc", BuildKind.APP), bus=bus)
    out = reg.dispatch("open_build", {"name": "tip calc"})
    assert [e.slug for e in bus.published] == ["tip-calc"] and "Opening Tip Calc" in out


def test_open_build_still_opens_a_hologram():
    bus = _FakeBus()
    reg = _open_registry(_Build("Garden", BuildKind.MODEL), bus=bus)
    assert "Opening Garden" in reg.dispatch("open_build", {"name": "garden"})
    assert [e.slug for e in bus.published] == ["garden"]
