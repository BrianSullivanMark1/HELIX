"""ConversationService — an autonomous agent run is denied build/spend/self-mod/delete tools."""
from __future__ import annotations

from datetime import datetime

from helix.ports.llm import Reply, Text, ToolSpec
from helix.services.conversation import ConversationService


class _CaptureChat:
    """Records the tool list it was offered, then ends the turn with a plain (no-tool) reply."""

    def __init__(self) -> None:
        self.last_tools: list[ToolSpec] = []

    def chat(self, turns, *, system=None, tools=None) -> Reply:
        self.last_tools = list(tools or [])
        return Reply(blocks=(Text("done"),))


class _FakeTools:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def dispatch(self, *a, **k) -> str:
        return "ok"


class _FakeStore:
    def __init__(self) -> None:
        self.msgs: list = []

    def append(self, m) -> None:
        self.msgs.append(m)

    def recent(self, limit: int = 100) -> list:
        return list(self.msgs)


class _FakeMemory:
    def record_usage(self, *a) -> None:
        pass


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 6, 26, 12, 0, 0)


def _service() -> tuple[ConversationService, _CaptureChat]:
    chat = _CaptureChat()
    specs = [
        ToolSpec("build_app", "build an app", {"type": "object", "properties": {}}),
        ToolSpec("build_task", "build a task", {"type": "object", "properties": {}}),
        ToolSpec("create_agent", "save an agent", {"type": "object", "properties": {}}),
        ToolSpec("delete_build", "delete a build", {"type": "object", "properties": {}}),
        ToolSpec("improve_helix", "self-modify", {"type": "object", "properties": {}}),
        ToolSpec("list_apps", "list builds", {"type": "object", "properties": {}}),
    ]
    svc = ConversationService(chat, _FakeTools(specs), _FakeStore(), _FakeMemory(), _FixedClock(), "sys")
    return svc, chat


def test_agent_run_denies_build_spend_and_delete_tools():
    svc, chat = _service()
    svc.run_turn("do the thing", allow_builds=False)
    offered = {t.name for t in chat.last_tools}
    # An autonomous agent may read/think/report, but never build, spend, self-modify, or delete.
    assert offered == {"list_apps"}
    for denied in ("build_app", "build_task", "create_agent", "delete_build", "improve_helix"):
        assert denied not in offered


def test_normal_console_run_keeps_every_tool():
    svc, chat = _service()
    svc.run_turn("build me a timer", allow_builds=True)
    offered = {t.name for t in chat.last_tools}
    assert "build_app" in offered and "improve_helix" in offered and "list_apps" in offered
