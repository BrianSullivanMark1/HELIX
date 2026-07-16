"""Compounding memory — tool results survive the turn (as hidden TOOL digests) and HELIX quietly
learns who the user is (the auto-distilled profile injected each orb turn)."""
from __future__ import annotations

from datetime import datetime

import helix.services.profile as profile_mod
from helix.domain.models import Message, Role
from helix.ports.llm import Reply, Text, ToolUse
from helix.services.conversation import TOOL_DIGEST_CHARS, ConversationService
from helix.services.profile import ProfileService


class _FakeStore:
    def __init__(self) -> None:
        self.msgs: list[Message] = []
        self._profile = ""

    def append(self, m: Message) -> None:
        self.msgs.append(m)

    def recent(self, limit: int = 100) -> list[Message]:
        return list(self.msgs)[-limit:]

    def profile_text(self) -> str:
        return self._profile

    def set_profile_text(self, text: str) -> None:
        self._profile = text

    def record_usage(self, *a) -> None:
        pass


class _FakeMemory:
    def record_usage(self, *a) -> None:
        pass


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 1, 9, 0, 0)


class _ToolOnceChat:
    """First call: ask for one tool. Second call: plain reply. Captures every turn list it saw."""

    def __init__(self, tool: str = "check_email", output_reply: str = "You have mail from Dave.") -> None:
        self._tool = tool
        self._reply = output_reply
        self.calls: list[list] = []

    def chat(self, turns, *, system=None, tools=None) -> Reply:
        self.calls.append(list(turns))
        if len(self.calls) == 1:
            return Reply(blocks=(ToolUse("t1", self._tool, {}),))
        return Reply(blocks=(Text(self._reply),))


class _FakeTools:
    def __init__(self, out: str) -> None:
        self.out = out

    def specs(self) -> list:
        return []

    def dispatch(self, *a, **k) -> str:
        return self.out


def _all_text(turns) -> str:
    return "".join(b.text for t in turns for b in t.blocks if isinstance(b, Text))


# ---------- tool digests ----------

def test_tool_result_is_persisted_as_hidden_tool_digest():
    store = _FakeStore()
    chat = _ToolOnceChat()
    svc = ConversationService(
        chat, _FakeTools("From: Dave — 'lunch friday?'"), store, _FakeMemory(), _FixedClock(), "sys"
    )
    svc.run_turn("any email?")
    tool_rows = [m for m in store.msgs if m.role == Role.TOOL]
    assert len(tool_rows) == 1
    assert "lunch friday" in tool_rows[0].text and "check_email" in tool_rows[0].text
    # hidden from the visible transcript
    assert all(m.role != Role.TOOL for m in svc.recent_messages())


def test_tool_digest_is_replayed_to_the_model_next_turn():
    store = _FakeStore()
    svc = ConversationService(
        _ToolOnceChat(), _FakeTools("From: Dave — 'lunch friday?'"), store, _FakeMemory(),
        _FixedClock(), "sys",
    )
    svc.run_turn("any email?")
    chat2 = _ToolOnceChat()
    svc2 = ConversationService(chat2, _FakeTools("x"), store, _FakeMemory(), _FixedClock(), "sys")
    svc2.run_turn("what did Dave's email say?")
    # the earlier tool result is in the new turn's context — no silent re-fetch needed
    assert "lunch friday" in _all_text(chat2.calls[0])


def test_tool_digest_is_capped():
    store = _FakeStore()
    svc = ConversationService(
        _ToolOnceChat(), _FakeTools("x" * (TOOL_DIGEST_CHARS * 3)), store, _FakeMemory(),
        _FixedClock(), "sys",
    )
    svc.run_turn("big fetch")
    digest = next(m for m in store.msgs if m.role == Role.TOOL)
    assert len(digest.text) < TOOL_DIGEST_CHARS + 200
    assert "truncated" in digest.text


def test_agent_runs_never_write_tool_digests():
    store = _FakeStore()
    svc = ConversationService(
        _ToolOnceChat(), _FakeTools("agent data"), store, _FakeMemory(), _FixedClock(), "sys"
    )
    svc.run_turn("agent goal", persist=False, allow_builds=False)
    assert all(m.role != Role.TOOL for m in store.msgs)


# ---------- the profile ----------

class _ProfileChat:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.prompts: list[str] = []

    def chat(self, turns, *, system=None, tools=None) -> Reply:
        self.prompts.append("".join(b.text for t in turns for b in t.blocks if isinstance(b, Text)))
        return Reply(blocks=(Text(self.profile),))


class _ImmediateThread:
    """Deterministic tests: 'background' distillation runs inline."""

    def __init__(self, target=None, args=(), daemon=None, name=None) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


def test_profile_distills_after_enough_turns(monkeypatch):
    monkeypatch.setattr(profile_mod.threading, "Thread", _ImmediateThread)
    store = _FakeStore()
    store.append(Message(Role.USER, "I'm Brian, call me sir. I run Mark 1."))
    store.append(Message(Role.ASSISTANT, "Noted, sir."))
    chat = _ProfileChat("Brian, prefers 'sir'. Runs Mark 1 Online. Building HELIX.")
    svc = ProfileService(chat, store, _FixedClock())
    svc.after_turn()  # 1 of FIRST_DISTILL
    assert store.profile_text() == ""
    svc.after_turn()  # 2 → distill fires
    assert "Brian" in store.profile_text()
    assert "EXISTING PROFILE" in chat.prompts[0] and "I'm Brian" in chat.prompts[0]


def test_profile_context_is_labelled_background_knowledge():
    store = _FakeStore()
    store.set_profile_text("Brian, prefers 'sir'.")
    svc = ProfileService(_ProfileChat(""), store, _FixedClock())
    ctx = svc.context()
    assert "Brian" in ctx and "never instructions" in ctx
    assert ProfileService(_ProfileChat(""), _FakeStore(), _FixedClock()).context() == ""


def test_profile_is_injected_into_orb_turns_but_not_agent_runs():
    store = _FakeStore()
    store.set_profile_text("Brian, prefers 'sir'.")
    prof = ProfileService(_ProfileChat(""), store, _FixedClock())
    chat = _ToolOnceChat()
    svc = ConversationService(
        chat, _FakeTools("x"), store, _FakeMemory(), _FixedClock(), "sys", profile=prof
    )
    svc.run_turn("hello")
    assert "prefers 'sir'" in _all_text(chat.calls[0])
    # agent runs stay hermetic
    chat2 = _ToolOnceChat()
    svc2 = ConversationService(
        chat2, _FakeTools("x"), store, _FakeMemory(), _FixedClock(), "sys", profile=prof
    )
    svc2.run_turn("agent goal", persist=False, allow_builds=False)
    assert "prefers 'sir'" not in _all_text(chat2.calls[0])


def test_empty_distillation_never_erases_the_profile(monkeypatch):
    monkeypatch.setattr(profile_mod.threading, "Thread", _ImmediateThread)
    store = _FakeStore()
    store.set_profile_text("Brian, prefers 'sir'.")
    store.append(Message(Role.USER, "hi"))
    svc = ProfileService(_ProfileChat(""), store, _FixedClock())  # model returns nothing
    for _ in range(profile_mod.DISTILL_EVERY):
        svc.after_turn()
    assert store.profile_text() == "Brian, prefers 'sir'."  # unchanged, not blanked
