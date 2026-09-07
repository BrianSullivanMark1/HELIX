"""ConversationService — an autonomous agent run is denied build/spend/self-mod/delete tools."""
from __future__ import annotations

from datetime import datetime

import pytest

from helix.ports.llm import Reply, Text, ToolSpec, ToolUse
from helix.services.conversation import MAX_STEPS, ConversationService


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


class _FakeSubscription:
    """Both rails, recording: the persistent orb turn and the hermetic escalation."""

    def __init__(self, hermetic_fails: bool = False) -> None:
        self.orb_calls: list[str] = []
        self.hermetic_calls: list[tuple] = []
        self.refreshed = 0
        self._fails = hermetic_fails

    def active(self) -> bool:
        return True

    def run_orb_turn(self, prompt, names, **kw) -> str:
        self.orb_calls.append(prompt)
        return "orb answer"

    def run_hermetic(self, prompt, names=(), **kw) -> str:
        self.hermetic_calls.append((prompt, kw))
        if self._fails:
            raise RuntimeError("deep rail down")
        return "deep answer"

    def refresh_session(self) -> None:
        self.refreshed += 1


class _FakeGrowth:
    def resolve(self) -> str:
        return "claude-fable-5"


class _FakeSettings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)


HARD_TURN = "Why does the slicer refuse this part — walk me through the root cause and how to fix it"
EASY_TURN = "what time is it"


def _deep_service(sub, **settings):
    svc = ConversationService(
        _CaptureChat(), _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys",
        subscription=sub, growth_model=_FakeGrowth(), settings=_FakeSettings(**settings),
    )
    return svc


def test_looks_hard_reads_reasoning_words_not_chitchat():
    from helix.services.conversation import _looks_hard

    assert _looks_hard(HARD_TURN)
    assert _looks_hard("Help me architect the data layer for the irrigation app, three services deep")
    assert _looks_hard("word " * 95)                    # a wall of context IS a hard turn
    assert not _looks_hard(EASY_TURN)
    assert not _looks_hard("compare them")              # reasoning word, no substance — a follow-up
    assert not _looks_hard("go to sleep")
    assert not _looks_hard("build me a notes app")


def test_a_hard_turn_escalates_to_the_growth_model_and_refreshes_the_session():
    sub = _FakeSubscription()
    svc = _deep_service(sub)
    reply = svc.run_turn(HARD_TURN)
    assert reply == "deep answer"
    assert sub.orb_calls == []                          # the everyday session never ran this one
    assert len(sub.hermetic_calls) == 1
    _prompt, kw = sub.hermetic_calls[0]
    assert kw.get("model") == "claude-fable-5" and kw.get("web") is True
    assert sub.refreshed == 1                           # next turn reseeds with this exchange


def test_an_easy_turn_stays_on_the_everyday_brain():
    sub = _FakeSubscription()
    svc = _deep_service(sub)
    assert svc.run_turn(EASY_TURN) == "orb answer"
    assert sub.hermetic_calls == []


def test_the_auto_deep_toggle_turns_escalation_off():
    sub = _FakeSubscription()
    svc = _deep_service(sub, auto_deep_turns=False)
    assert svc.run_turn(HARD_TURN) == "orb answer"
    assert sub.hermetic_calls == []


def test_an_agent_run_never_escalates():
    # Autonomous runs chew on untrusted content — a hard-LOOKING email must not be able to spend
    # the top tier. persist=False/allow_builds=False is the same discriminator every fence uses.
    sub = _FakeSubscription()
    svc = _deep_service(sub)
    svc.run_turn(HARD_TURN, allow_builds=False, persist=False)
    # The agent's ONE hermetic call is the run itself — default model, no growth override, no web.
    assert len(sub.hermetic_calls) == 1
    _prompt, kw = sub.hermetic_calls[0]
    assert "model" not in kw and kw.get("web") is False


def test_a_failed_deep_turn_falls_back_to_the_orb():
    sub = _FakeSubscription(hermetic_fails=True)
    svc = _deep_service(sub)
    assert svc.run_turn(HARD_TURN) == "orb answer"      # the user still gets an answer
    assert sub.refreshed == 0


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


class _ScriptChat:
    """Returns a scripted sequence of replies (so we can drive the tool loop), else a plain 'done'."""

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.last_turns: list = []

    def chat(self, turns, *, system=None, tools=None) -> Reply:
        self.calls += 1
        self.last_turns = list(turns)
        return self._replies.pop(0) if self._replies else Reply(blocks=(Text("done"),))


class _RaisingTools:
    def specs(self):
        return [ToolSpec("list_apps", "list", {"type": "object", "properties": {}})]

    def dispatch(self, *a, **k):
        raise RuntimeError("boom")


class _OkTools:
    def specs(self):
        return [ToolSpec("list_apps", "list", {"type": "object", "properties": {}})]

    def dispatch(self, *a, **k):
        return "two apps"


def test_api_loop_recovers_from_a_tool_error():
    # A tool that raises must NOT crash the turn — the error is fed back to the model, which then answers.
    chat = _ScriptChat([
        Reply(blocks=(ToolUse("t1", "list_apps", {}),)),  # first: call the (raising) tool
        Reply(blocks=(Text("Here you go."),)),            # then: recover with a plain answer
    ])
    svc = ConversationService(chat, _RaisingTools(), _FakeStore(), _FakeMemory(), _FixedClock(), "sys")
    out = svc.run_turn("what apps do I have")
    assert out == "Here you go."   # recovered
    assert chat.calls == 2         # the loop continued past the tool error


def test_api_loop_stops_at_max_steps():
    # A model that keeps calling tools forever is capped at MAX_STEPS, then bows out gracefully.
    chat = _ScriptChat([Reply(blocks=(ToolUse(f"t{i}", "list_apps", {}),)) for i in range(MAX_STEPS + 3)])
    svc = ConversationService(chat, _OkTools(), _FakeStore(), _FakeMemory(), _FixedClock(), "sys")
    out = svc.run_turn("loop please")
    assert chat.calls == MAX_STEPS
    assert "stuck" in out.lower()


def test_successful_tool_result_is_remembered_as_a_tool_row():
    from helix.domain.models import Role

    chat = _ScriptChat([
        Reply(blocks=(ToolUse("t1", "list_apps", {}),)),
        Reply(blocks=(Text("Two apps."),)),
    ])
    store = _FakeStore()
    svc = ConversationService(chat, _OkTools(), store, _FakeMemory(), _FixedClock(), "sys")
    svc.run_turn("what apps")
    # what the tool learned is persisted as a TOOL row so a later turn can answer from it
    assert any(m.role == Role.TOOL for m in store.msgs)


class _FakeMemSvc:
    """Stand-in MemoryService: returns a per-user context block, records after_turn calls."""

    def __init__(self, facts_by_user):
        self._by = facts_by_user
        self.after_calls = []

    def context(self, user=""):
        f = self._by.get(user)
        return f"[memory: {f}]" if f else ""

    def after_turn(self, user=""):
        self.after_calls.append(user)


def test_per_speaker_memory_is_injected_for_the_right_person():
    # Brian's long-term memory is injected on his turn; Sarah (no memory) gets none — household keying.
    mem = _FakeMemSvc({"brian": "is a contractor"})
    chat = _CaptureChat()
    svc = ConversationService(
        chat, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys", user_memory=mem
    )
    svc.run_turn("hey", speaker="Brian")
    assert "is a contractor" in _all_text(chat.last_turns)
    assert mem.after_calls == ["brian"]  # captured under the speaker key

    chat2 = _CaptureChat()
    svc2 = ConversationService(
        chat2, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys", user_memory=mem
    )
    svc2.run_turn("hey", speaker="Sarah")
    assert "is a contractor" not in _all_text(chat2.last_turns)  # not Sarah's memory


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


def test_escalation_is_fenced_from_autonomous_runs():
    from helix.services.conversation import BUILD_TOOLS

    # think_harder hands its argument to the STRONGEST model WITH web access. An unattended watcher
    # processing untrusted content (an email, a web page) must not be able to launder that text into a
    # web-enabled deep reasoner — every peer egress/escalation faculty is fenced, and this one was
    # missed until an audit caught it.
    assert "think_harder" in BUILD_TOOLS
    # Reading stays open to an agent — fencing escalation must not have fenced the read faculties a
    # morning brief is made of.
    assert "read_file" not in BUILD_TOOLS and "search_knowledge" not in BUILD_TOOLS


# ----- THE DREAM TIER (READ_ME/DREAM_MIND.md §10): DREAM_TOOLS, tool_names, the verified block -----

def _spec(name: str) -> ToolSpec:
    return ToolSpec(name, name, {"type": "object", "properties": {}})


_TIER_SPECS = [_spec(n) for n in (
    "list_apps", "research_search", "research_read", "verified_facts",   # readable
    "note_verified_fact", "note_improvement", "remember",                # the DREAM writes
    "build_app", "forget_verified", "view_screen", "think_harder",       # fenced
)]
_READABLE = {"list_apps", "research_search", "research_read", "verified_facts"}


class _RecordingTools(_FakeTools):
    def __init__(self, specs):
        super().__init__(specs)
        self.dispatched: list[str] = []

    def dispatch(self, name, args, **k) -> str:
        self.dispatched.append(name)
        return f"{name} ran"


def _tier_service(chat=None, tools=None, **kw):
    chat = chat or _CaptureChat()
    tools = tools or _RecordingTools(_TIER_SPECS)
    svc = ConversationService(chat, tools, _FakeStore(), _FakeMemory(), _FixedClock(), "sys", **kw)
    return svc, chat, tools


def test_dream_tools_is_every_readable_tool_plus_the_three_writes():
    from helix.services.conversation import BUILD_TOOLS, DREAM_TOOLS, DREAM_WRITES

    svc, _, _ = _tier_service()
    assert svc.dream_tools() == _READABLE | DREAM_WRITES
    assert DREAM_WRITES == {"note_verified_fact", "note_improvement", "remember"}
    # the sentinel is a frozenset (types like any allowlist) that names no real tool
    assert isinstance(DREAM_TOOLS, frozenset) and not (DREAM_TOOLS & _READABLE)
    # note_verified_fact is the one DREAM write that is NOT fenced (the orb has it too);
    # forget_verified is fenced like every other record-rewriting tool
    assert "note_verified_fact" not in BUILD_TOOLS and "forget_verified" in BUILD_TOOLS
    # composed live: a faculty attached later is in the tier next time it is asked
    svc2, _, tools = _tier_service(tools=_RecordingTools([_spec("list_apps")]))
    assert svc2.dream_tools() == {"list_apps"} | DREAM_WRITES
    tools._specs.append(_spec("research_read"))
    assert "research_read" in svc2.dream_tools()


def test_the_dream_tier_is_offered_the_writes_only_because_it_names_them():
    from helix.services.conversation import DREAM_TOOLS, DREAM_WRITES

    svc, chat, _ = _tier_service()
    svc.run_turn("research esp32 psram", allow_builds=False, persist=False, tool_names=DREAM_TOOLS)
    offered = {t.name for t in chat.last_tools}
    assert offered == _READABLE | DREAM_WRITES
    for fenced in ("build_app", "forget_verified", "view_screen", "think_harder"):
        assert fenced not in offered
    # an explicit set narrows further — and can never re-admit a fenced tool
    svc.run_turn("read", allow_builds=False, persist=False,
                 tool_names={"research_read", "build_app", "view_screen"})
    assert {t.name for t in chat.last_tools} == {"research_read"}
    # on a human turn the allowlist only narrows what the full set offers
    svc.run_turn("read", allow_builds=True, tool_names={"research_read", "build_app"})
    assert {t.name for t in chat.last_tools} == {"research_read", "build_app"}
    # the sentinel anywhere else narrows to nothing rather than widening anything
    svc.run_turn("x", allow_builds=True, tool_names=frozenset(DREAM_TOOLS | {"list_apps"}))
    assert {t.name for t in chat.last_tools} == {"list_apps"}


def test_a_watcher_never_gets_the_dream_writes():
    from helix.services.conversation import DREAM_WRITES

    svc, chat, _ = _tier_service()
    svc.run_turn("process this email", allow_builds=False, persist=False)   # no tool_names: a watcher
    offered = {t.name for t in chat.last_tools}
    assert offered == _READABLE
    assert not (offered & DREAM_WRITES)
    svc.run_turn("process this email", allow_builds=False)                  # a persisted autonomous turn too
    assert not ({t.name for t in chat.last_tools} & DREAM_WRITES)


def test_the_orb_keeps_note_verified_fact_but_the_fence_holds_on_forget():
    svc, chat, _ = _tier_service()
    svc.run_turn("note what you verified", allow_builds=True)
    offered = {t.name for t in chat.last_tools}
    assert {"note_verified_fact", "forget_verified", "research_read"} <= offered


def test_tool_names_is_enforced_at_dispatch_on_the_api_rail():
    from helix.ports.llm import ToolResult

    chat = _ScriptChat([
        Reply(blocks=(ToolUse("t1", "list_apps", {}), ToolUse("t2", "note_verified_fact", {}),
                      ToolUse("t3", "build_app", {}))),
        Reply(blocks=(Text("done"),)),
    ])
    svc, chat, tools = _tier_service(chat=chat)
    out = svc.run_turn("go", allow_builds=False, persist=False, tool_names={"list_apps"})
    assert out == "done"
    assert tools.dispatched == ["list_apps"]          # the model's other two calls never ran
    results = [b for b in chat.last_turns[-1].blocks if isinstance(b, ToolResult)]
    errors = {r.tool_use_id: r for r in results if r.is_error}
    assert set(errors) == {"t2", "t3"}
    assert "isn't available in this run" in errors["t2"].content


def test_the_dream_tier_bridges_only_its_names_on_the_subscription_rail():
    from helix.services.conversation import DREAM_TOOLS, DREAM_WRITES

    sub = _FakeSubscription()
    svc, _, _ = _tier_service(subscription=sub, growth_model=_FakeGrowth())
    svc.run_turn("research", allow_builds=False, persist=False, tool_names=DREAM_TOOLS)
    _prompt, kw = sub.hermetic_calls[-1]
    assert kw.get("web") is False                    # the model's own web tools stay OFF
    # Fable or nothing (DREAM_MIND.md §13): the tier runs on the growth model at high effort — a
    # research turn on the orb's Sonnet was two of the six cycles running below the night's model.
    assert kw["model"] == "claude-fable-5" and kw["effort"] == "high"
    bare = _FakeSubscription()
    svc_bare, _, _ = _tier_service(subscription=bare)  # no resolver wired (a bare rig): the rail's default
    svc_bare.run_turn("research", allow_builds=False, persist=False, tool_names=DREAM_TOOLS)
    assert "model" not in bare.hermetic_calls[-1][1]
    svc.run_turn("watch", allow_builds=False, persist=False)  # a watcher never escalates
    assert "model" not in sub.hermetic_calls[-1][1]
    # the names the rail bridges are exactly the tier's
    svc2, _, _ = _tier_service(subscription=(sub2 := _NamesSubscription()))
    svc2.run_turn("research", allow_builds=False, persist=False, tool_names=DREAM_TOOLS)
    assert set(sub2.names) == _READABLE | DREAM_WRITES
    svc2.run_turn("watch", allow_builds=False, persist=False)
    assert set(sub2.names) == _READABLE


class _NamesSubscription(_FakeSubscription):
    def __init__(self):
        super().__init__()
        self.names: tuple = ()

    def run_hermetic(self, prompt, names=(), **kw) -> str:
        self.names = tuple(names)
        return super().run_hermetic(prompt, names, **kw)


class _LimitSubscription(_FakeSubscription):
    """The plan's limit mid-turn — after a tool already ran when `after_tool` is set."""

    def __init__(self, text="usage limit reached — resets at 3pm", after_tool=False):
        super().__init__()
        self.text, self.after_tool = text, after_tool

    def run_hermetic(self, prompt, names=(), **kw) -> str:
        self.hermetic_calls.append((prompt, kw))
        if self.after_tool and kw.get("on_tool") is not None:
            kw["on_tool"]("list_apps", "ran", False)
        raise RuntimeError(self.text)


def test_a_dream_turn_never_falls_to_the_api_key_when_the_plan_fails():
    """DREAM_MIND.md §13: a limit on the subscription is raised with the provider's own words — the
    mind pauses on it — never softened into a sentence, never retried on the metered API key (a
    downgrade to Sonnet with tools that also hid the limit from the pause discipline)."""
    from helix.services.conversation import DREAM_TOOLS

    for after_tool in (False, True):
        sub = _LimitSubscription(after_tool=after_tool)
        svc, chat, _ = _tier_service(subscription=sub, growth_model=_FakeGrowth())
        with pytest.raises(RuntimeError, match="usage limit reached — resets at 3pm"):
            svc.run_turn("research", allow_builds=False, persist=False, speaker="dream", tool_names=DREAM_TOOLS)
        assert chat.last_tools == [] and chat.last_turns == []  # the API chat was never asked
        assert len(sub.hermetic_calls) == 1
    # A watcher (no tool_names) keeps the orb's safety net: the API leg answers.
    sub = _LimitSubscription()
    svc, chat, _ = _tier_service(subscription=sub)
    assert svc.run_turn("watch", allow_builds=False, persist=False) == "done" and chat.last_turns


class _FakeVerified:
    """Stand-in VerifiedStore: a block when the turn mentions PSRAM, records what it was asked."""

    def __init__(self, fail: bool = False) -> None:
        self.asked: list[str] = []
        self.fail = fail

    def for_turn(self, text: str, project: str = "") -> str:
        self.asked.append(text)
        if self.fail:
            raise OSError("disk")
        if "psram" in text.lower():
            return ("[VERIFIED KNOWLEDGE — records: 1) XIAO ESP32S3 Sense PSRAM: 8 MB — verified "
                    "2026-09-04 from wiki.seeedstudio.com]")
        return ""


def test_verified_knowledge_is_injected_on_orb_turns_but_never_persisted():
    ver = _FakeVerified()
    chat = _CaptureChat()
    store = _FakeStore()
    svc = ConversationService(chat, _FakeTools([]), store, _FakeMemory(), _FixedClock(), "sys",
                              verified=ver)
    svc.run_turn("how much PSRAM does the xiao have", speaker="Brian")
    seen = _all_text(chat.last_turns)
    assert "verified 2026-09-04 from wiki.seeedstudio.com" in seen
    assert "wiki.seeedstudio.com" not in "".join(m.text for m in store.msgs)   # ephemeral, like lessons
    assert ver.asked == ["how much PSRAM does the xiao have"]
    chat2 = _CaptureChat()
    svc2 = ConversationService(chat2, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys",
                               verified=ver)
    svc2.run_turn("what time is it")
    assert "VERIFIED KNOWLEDGE" not in _all_text(chat2.last_turns)   # nothing relevant → no block


def test_verified_knowledge_reaches_the_dream_tier_but_not_a_plain_watcher():
    from helix.services.conversation import DREAM_TOOLS

    ver = _FakeVerified()
    chat = _CaptureChat()
    svc = ConversationService(chat, _RecordingTools(_TIER_SPECS), _FakeStore(), _FakeMemory(),
                              _FixedClock(), "sys", verified=ver)
    svc.run_turn("verify the PSRAM claim", allow_builds=False, persist=False, tool_names=DREAM_TOOLS)
    assert "verified 2026-09-04 from wiki.seeedstudio.com" in _all_text(chat.last_turns)
    svc.run_turn("an email about PSRAM", allow_builds=False, persist=False)   # a watcher
    assert "VERIFIED KNOWLEDGE" not in _all_text(chat.last_turns)
    assert ver.asked == ["verify the PSRAM claim"]   # the watcher never even consulted the store


def test_a_verified_store_failure_never_costs_the_turn():
    chat = _CaptureChat()
    svc = ConversationService(chat, _FakeTools([]), _FakeStore(), _FakeMemory(), _FixedClock(), "sys",
                              verified=_FakeVerified(fail=True))
    assert svc.run_turn("psram?") == "done"
    assert "VERIFIED KNOWLEDGE" not in _all_text(chat.last_turns)


def test_no_verified_store_means_no_block_and_no_error():
    svc, chat, _ = _tier_service()
    assert svc.run_turn("psram?") == "done"
    assert "VERIFIED KNOWLEDGE" not in _all_text(chat.last_turns)
