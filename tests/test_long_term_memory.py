"""MemoryService — durable per-speaker facts: explicit add, context injection, forget, and distillation."""
from __future__ import annotations

from datetime import datetime

from helix.domain.models import Message, Role
from helix.ports.llm import Reply, Text
from helix.services import memory as memory_mod
from helix.services.memory import MemoryService


class _Chat:
    def __init__(self, text=""):
        self.text = text
        self.prompts = []

    def chat(self, turns, *, system=None, tools=None):
        self.prompts.append("".join(b.text for t in turns for b in t.blocks if isinstance(b, Text)))
        return Reply(blocks=(Text(self.text),))


class _Store:
    def __init__(self, msgs=None):
        self.msgs = list(msgs or [])

    def recent(self, limit):
        return self.msgs[-limit:]

    def record_usage(self, *a):
        pass


class _Mem:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _Clock:
    def now(self):
        return datetime(2026, 7, 14, 9, 0, 0).astimezone()


class _ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def _svc(chat=None, store=None):
    return MemoryService(chat or _Chat(), store or _Store(), _Mem(), _Clock())


def test_explicit_add_and_context():
    s = _svc()
    assert "remember" in s.add("Brian is a general contractor").lower()
    ctx = s.context()
    assert "general contractor" in ctx and "never instructions" in ctx.lower()


def test_add_dedupes():
    s = _svc()
    s.add("Has a daughter named Ada")
    assert "already" in s.add("has a daughter named ada").lower()  # case-insensitive dedupe


def test_forget_removes_matching_facts():
    s = _svc()
    s.add("Hates cilantro")
    s.add("Loves hiking")
    assert "1" in s.forget("cilantro")
    facts = s.facts()
    assert "Loves hiking" in facts and all("cilantro" not in f.lower() for f in facts)


def test_per_speaker_isolation():
    s = _svc()
    s.add("Brian fact", user="brian")
    assert "Brian fact" in s.facts(user="brian")
    assert s.facts(user="sarah") == []
    assert s.context(user="sarah") == ""


def test_distillation_writes_facts(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    store = _Store([Message(Role.USER, "My dog is called Rex and I work at Acme", _Clock().now()),
                    Message(Role.ASSISTANT, "Noted.", _Clock().now())])
    chat = _Chat("Has a dog named Rex.\nWorks at Acme.")
    s = _svc(chat=chat, store=store)
    for _ in range(memory_mod._DISTILL_EVERY):
        s.after_turn()
    facts = s.facts()
    assert "Has a dog named Rex." in facts and "Works at Acme." in facts


def test_empty_distillation_keeps_existing(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    store = _Store([Message(Role.USER, "hi", _Clock().now())])
    s = _svc(chat=_Chat(""), store=store)  # model returns nothing
    s.add("A kept fact")
    for _ in range(memory_mod._DISTILL_EVERY):
        s.after_turn()
    assert "A kept fact" in s.facts()


# ----- V3 visual memory: an image turn TEACHES the long-term memory -----

def test_image_turn_distills_visual_facts(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    chat = _Chat("The garage breaker panel is a Square D QO 200A.")
    s = _svc(chat=chat)
    s.after_image_turn("", "what breaker panel is this?", "That's a Square D QO 200-amp panel.")
    assert any("Square D" in f for f in s.facts())
    # The distiller saw the exchange, not the pixels (images are ephemeral by design).
    assert "Square D QO 200-amp" in chat.prompts[-1]


def test_image_turn_facts_are_per_speaker_and_appended(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    s = _svc(chat=_Chat("Rex is a brindle boxer."))
    s.add("Works at Acme", user="brian")
    s.after_image_turn("brian", "what breed is my dog?", "He's a brindle boxer.")
    facts = s.facts(user="brian")
    assert "Works at Acme" in facts and "Rex is a brindle boxer." in facts
    assert s.facts(user="sarah") == []  # another speaker's memory untouched


def test_image_turn_with_nothing_durable_adds_nothing(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    s = _svc(chat=_Chat(""))  # model finds nothing durable
    s.add("A kept fact")
    s.after_image_turn("", "read this error", "It's a missing DLL error.")
    assert s.facts() == ["A kept fact"]


def test_image_turn_dedupes_and_caps_new_facts(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    s = _svc(chat=_Chat("A kept fact\nNew fact one\nNew fact two\nNew fact three\nNew fact four"))
    s.add("A kept fact")
    s.after_image_turn("", "look", "seen")
    facts = s.facts()
    assert facts.count("A kept fact") == 1          # no duplicate of a known fact
    assert "New fact four" not in facts             # at most three new facts per image turn
    assert {"New fact one", "New fact two", "New fact three"} <= set(facts)


def test_image_turn_with_empty_answer_never_calls_the_model(monkeypatch):
    monkeypatch.setattr(memory_mod.threading, "Thread", _ImmediateThread)
    chat = _Chat("should never be used")
    s = _svc(chat=chat)
    s.after_image_turn("", "look at this", "")
    assert chat.prompts == [] and s.facts() == []
