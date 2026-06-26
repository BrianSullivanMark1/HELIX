"""ToolRegistry tests — build_3d_model is exposed and routed through the Forge with the 3D prompt."""
from __future__ import annotations

from helix.domain.models import BuildKind
from helix.services.prompts import build_3d_model_prompt, build_task_prompt
from helix.services.tools import ToolRegistry


class _App:
    def __init__(self, name: str) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")


class _FakeForge:
    """Records build() calls so we can assert which prompt + kind the tool routed with."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build(self, name, request, *, prompt=None, kind=None, on_progress=None, cancel=None):
        self.calls.append(
            {"name": name, "request": request, "prompt": prompt, "kind": kind, "cancel": cancel}
        )
        return _App(name)

    def remove_build(self, name) -> bool:
        return False  # no workspace build by that name — let the agent path handle deletes in tests


class _FakeBuilds:
    def list(self):
        return []


class _FakeAgents:
    """Stand-in AgentService: records add/remove and answers list() from a seed of names."""

    def __init__(self, existing=()) -> None:
        self.added: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self._existing = list(existing)

    def add(self, name, goal):
        self.added.append((name, goal))
        return _App(name)

    def list(self):
        return [_App(n) for n in self._existing]

    def remove(self, name) -> None:
        self.removed.append(name)


def _registry() -> tuple[ToolRegistry, _FakeForge]:
    forge = _FakeForge()
    return ToolRegistry(forge, _FakeBuilds()), forge


def test_build_3d_model_is_exposed():
    reg, _ = _registry()
    names = {t.name for t in reg.specs()}
    assert "build_3d_model" in names
    assert "build_app" in names  # the new faculty sits alongside the existing ones


def test_build_3d_model_routes_with_the_model_prompt():
    reg, forge = _registry()
    out = reg.dispatch(
        "build_3d_model", {"name": "Wall Camera Unit", "request": "camera, speaker and mic"}
    )
    assert len(forge.calls) == 1
    call = forge.calls[0]
    assert call["name"] == "Wall Camera Unit"
    # Routed with the 3D-model instruction, not the default app-builder prompt.
    assert call["prompt"] == build_3d_model_prompt("Wall Camera Unit", "camera, speaker and mic")
    assert "3D MODEL" in call["prompt"]
    assert call["kind"] == BuildKind.MODEL  # so it lands in the Models tab
    assert "Modeled" in out


def test_build_app_still_uses_the_default_prompt():
    reg, forge = _registry()
    reg.dispatch("build_app", {"name": "Tip Calc", "request": "a tip calculator"})
    assert forge.calls[0]["prompt"] is None  # default app-builder path is untouched
    # build_app doesn't pass a kind — the Forge decides (new build -> app; iteration -> preserved).
    assert forge.calls[0]["kind"] is None


def test_build_task_routes_with_the_task_prompt_and_kind():
    reg, forge = _registry()
    assert "build_task" in {t.name for t in reg.specs()}
    out = reg.dispatch("build_task", {"name": "Rename Downloads", "request": "tidy my downloads"})
    call = forge.calls[0]
    assert call["prompt"] == build_task_prompt("Rename Downloads", "tidy my downloads")
    assert call["kind"] == BuildKind.TASK  # lands in the Tasks tab
    assert "task" in out.lower()


def test_create_agent_is_exposed_only_once_agents_are_bound():
    reg, _ = _registry()
    assert "create_agent" not in {t.name for t in reg.specs()}  # not wired yet
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
    out = reg.dispatch("delete_build", {"name": "morning brief"})  # case-insensitive match
    assert agents.removed == ["Morning Brief"]
    assert "Morning Brief" in out


def test_think_harder_routes_to_the_deep_thinker_when_wired():
    forge = _FakeForge()
    asked: list[str] = []

    def deep(question, on_progress=None):
        asked.append(question)
        return "a carefully reasoned answer"

    reg = ToolRegistry(forge, _FakeBuilds(), deep_think=deep)
    assert "think_harder" in {t.name for t in reg.specs()}
    out = reg.dispatch("think_harder", {"question": "why is the sky blue?"})
    assert asked == ["why is the sky blue?"]
    assert out == "a carefully reasoned answer"


def test_think_harder_absent_without_a_deep_thinker():
    reg, _ = _registry()  # no deep_think wired
    assert "think_harder" not in {t.name for t in reg.specs()}
