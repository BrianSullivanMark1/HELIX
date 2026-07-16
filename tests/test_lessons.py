"""LessonsService — HELIX learns HOW the user wants it to behave, from their corrections/confirmations.

No model, no threads in the assertions: the chat is faked at the seam, and the background distiller is
driven synchronously via _distill(). Invariants: the cheap feedback gate fires on corrections and
confirmations but not on ordinary requests; a distillation writes the parsed rules; the injected context
block carries them; and the parser strips stray bullets/numbering and dedupes.
"""
from __future__ import annotations

from datetime import datetime

from helix.domain.models import Message, Role
from helix.ports.llm import Reply, Text
from helix.services.lessons import LessonsService


class _Chat:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def chat(self, turns, system=None):
        self.calls += 1
        return Reply(blocks=(Text(self.text),))


class _Store:
    def __init__(self, msgs=None) -> None:
        self._m = list(msgs or [])

    def recent(self, limit):
        return self._m[-limit:]

    def record_usage(self, *a) -> None:
        pass


class _Rules:
    def __init__(self) -> None:
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v) -> None:
        self.d[k] = v


class _Clock:
    def now(self):
        return datetime(2026, 7, 14, 9, 0, 0).astimezone()


def _msgs():
    return [
        Message(Role.USER, "no, keep your answers shorter", _Clock().now()),
        Message(Role.ASSISTANT, "Understood.", _Clock().now()),
    ]


# ----- the feedback gate -----

def test_feedback_gate_fires_on_corrections_and_confirmations():
    for t in (
        "no, that's wrong", "actually, call it Falcon", "keep it shorter",
        "stop saying the tool names", "yes, that's right", "from now on use metric",
        "don't apologize so much", "that's not how I like it",
    ):
        assert LessonsService.looks_like_feedback(t), t


def test_feedback_gate_ignores_ordinary_requests():
    for t in (
        "build me a timer app", "what's the weather", "open the dashboard",
        "how many builds are running", "remind me at five",
    ):
        assert not LessonsService.looks_like_feedback(t), t


# ----- distillation + injection -----

def test_distill_writes_the_parsed_rules():
    chat = _Chat("Keep replies short.\nDon't say tool names out loud.")
    rules = _Rules()
    svc = LessonsService(chat, _Store(_msgs()), rules, _Clock())
    svc._distill()
    assert chat.calls == 1
    # rules are now keyed per-speaker; the default ("") bucket holds them
    assert rules.get("lessons") == {"": ["Keep replies short.", "Don't say tool names out loud."]}
    assert svc._rules() == ["Keep replies short.", "Don't say tool names out loud."]


def test_context_injects_the_rules_as_settled_preferences():
    rules = _Rules()
    rules.set("lessons", ["Keep replies short.", "Call the project Falcon."])
    svc = LessonsService(_Chat(""), _Store(), rules, _Clock())
    ctx = svc.context()
    assert "Standing preferences" in ctx
    assert "Keep replies short." in ctx and "Call the project Falcon." in ctx
    assert "1)" in ctx and "2)" in ctx


def test_context_is_empty_with_no_rules():
    svc = LessonsService(_Chat(""), _Store(), _Rules(), _Clock())
    assert svc.context() == ""


def test_after_turn_only_distills_on_feedback():
    chat = _Chat("Keep it short.")
    svc = LessonsService(chat, _Store(_msgs()), _Rules(), _Clock())
    svc.after_turn("what's the weather")   # not feedback → no background distill
    assert chat.calls == 0


# ----- the parser -----

def test_parse_strips_bullets_numbers_and_dedupes():
    parsed = LessonsService._parse("1. Keep it short\n- Keep it short\n2) Be concise\n\n* Be concise")
    assert parsed == ["Keep it short", "Be concise"]
