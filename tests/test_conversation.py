"""ConversationService — an autonomous agent run is denied build/spend/self-mod/delete tools."""
from __future__ import annotations

from datetime import datetime

from helix.ports.llm import Reply, Text, ToolSpec
from helix.services.conversation import ConversationService


class _CaptureChat:
    """Records the tool list and turns it was offered, then ends the turn with a plain reply."""

    def __init__(self) -> None:
        self.last_tools: list[ToolSpec] = []
        self.last_turns: list = []

    def chat(self, turns, *, system=None, tools=None) -> Reply:
        self.last_tools = list(tools or [])
        self.last_turns = list(turns)
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


def _all_text(turns) -> str:
    return "".join(
        b.text for t in turns for b in t.blocks if isinstance(b, Text)
    )


def test_attachments_reach_the_model_but_are_not_persisted():
    chat = _CaptureChat()
    store = _FakeStore()
    svc = ConversationService(chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys")
    svc.run_turn("look at this", attachments_text="<<<ATTACHMENTS secret-file-body ATTACHMENTS<<<")
    # The model saw the attachment content on this turn…
    assert "secret-file-body" in _all_text(chat.last_turns)
    assert "look at this" in _all_text(chat.last_turns)
    # …but only the short user text was written to history (so it isn't replayed every later turn).
    stored = "".join(m.text for m in store.msgs)
    assert "look at this" in stored
    assert "secret-file-body" not in stored


def test_no_attachments_leaves_the_turn_unchanged():
    chat = _CaptureChat()
    svc = ConversationService(chat, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys")
    svc.run_turn("plain message")
    seen = _all_text(chat.last_turns)
    assert "plain message" in seen
    assert "ATTACHMENTS" not in seen  # no attachment block was added (the time anchor rides along separately)


def test_agent_run_is_hermetic_when_not_persisting():
    from helix.domain.models import Message, Role

    chat = _CaptureChat()
    store = _FakeStore()
    store.append(Message(Role.USER, "a console message"))
    store.append(Message(Role.ASSISTANT, "a console reply"))
    svc = ConversationService(chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys")
    out = svc.run_turn("agent goal", allow_builds=False, persist=False)
    # The goal/report never enter the shared transcript…
    assert all("agent goal" not in m.text for m in store.msgs)
    assert out == "done"
    # …and the model saw ONLY the hermetic goal (plus the ephemeral time anchor), not the Console history.
    seen = _all_text(chat.last_turns)
    assert "agent goal" in seen
    assert "a console message" not in seen and "a console reply" not in seen


class _FakeKnowledge:
    """Stand-in KnowledgeService: records the queries it was asked and returns a fixed ambient block."""

    def __init__(self, text: str = "", sources: list | None = None) -> None:
        self.text = text
        self.sources = sources or []
        self.queries: list[str] = []

    def auto_context_with_sources(self, query: str):
        self.queries.append(query)
        return self.text, list(self.sources)


def test_ambient_knowledge_is_injected_on_orb_turns_but_not_persisted():
    chat = _CaptureChat()
    store = _FakeStore()
    know = _FakeKnowledge("<<<KNOWLEDGE-x the wifi password is hunter2 KNOWLEDGE-x<<<")
    svc = ConversationService(
        chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys", knowledge=know
    )
    svc.run_turn("what's my wifi password")
    # the orb saw the surfaced passage this turn…
    assert "hunter2" in _all_text(chat.last_turns)
    assert "what's my wifi password" in _all_text(chat.last_turns)
    # …but it's ephemeral — only the user's own words were written to history
    assert "hunter2" not in "".join(m.text for m in store.msgs)
    assert know.queries == ["what's my wifi password"]


def test_ambient_knowledge_sources_are_surfaced_for_a_citation():
    chat = _CaptureChat()
    know = _FakeKnowledge(
        "<<<KNOWLEDGE-x the wifi password is hunter2 KNOWLEDGE-x<<<", sources=[("Notes", "Wifi")]
    )
    svc = ConversationService(
        chat, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys", knowledge=know
    )
    sinks: list = []
    svc.run_turn("what's my wifi password", knowledge_sources=sinks)
    assert sinks == [("Notes", "Wifi")]  # the UI can render a 'from Notes › Wifi' chip


def test_ambient_knowledge_is_never_injected_into_an_agent_run():
    chat = _CaptureChat()
    know = _FakeKnowledge("<<<KNOWLEDGE-x should-not-appear KNOWLEDGE-x<<<")
    svc = ConversationService(
        chat, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys", knowledge=know
    )
    svc.run_turn("agent goal", allow_builds=False, persist=False)
    assert "should-not-appear" not in _all_text(chat.last_turns)
    assert know.queries == []  # an agent retrieves explicitly; auto_context isn't even consulted


def test_current_time_anchor_is_injected_each_turn_but_not_persisted():
    chat = _CaptureChat()
    store = _FakeStore()
    svc = ConversationService(chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys")
    svc.run_turn("what day is it")
    seen = _all_text(chat.last_turns)
    # the model is told the real 'now' + how to convert epoch timestamps — the date-accuracy fix
    assert "Current date & time" in seen and "2026" in seen and "Unix epoch" in seen
    assert "convert" in seen.lower()
    # ...but the anchor is ephemeral — only the user's words are written to history
    stored = "".join(m.text for m in store.msgs)
    assert "what day is it" in stored and "Current date & time" not in stored


def test_history_coalesces_consecutive_same_role_turns():
    from helix.domain.models import Message, Role

    chat = _CaptureChat()
    store = _FakeStore()
    store.append(Message(Role.USER, "first"))
    store.append(Message(Role.USER, "second"))  # two users in a row (e.g. a failed turn left an orphan)
    svc = ConversationService(chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys")
    svc.run_turn("third")
    # The API rejects user-then-user; the leading users must coalesce into one well-formed turn.
    assert [t.role for t in chat.last_turns] == [Role.USER]
    assert _all_text(chat.last_turns).startswith("firstsecondthird")  # + the ephemeral time anchor
