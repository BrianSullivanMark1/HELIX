"""WebVoice sleep/wake — the web shell's ears, driven as pure logic (no mic, no stream, no model).

The sleep engine has two entrances and both must work: a DIRECT phrase ("go to sleep") mutes
locally with no model turn, and a phrase the model judged once is CONSOLIDATED into a reflex so the
next time it is just as instant. Muted ears wake only on an explicit address — mentioning the wake
word in conversation must not wake them — and ears that cannot listen refuse to 'sleep' at all
(there is nothing to rest, and pretending otherwise is the old self-contradiction bug).
"""
from __future__ import annotations

from helix.api.voice_loop import WebVoice


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)  # voice_input_on absent → the constructor opens NO stream

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Stt:
    def available(self):
        return True

    def ready(self):
        return True


class _Tts:
    def available(self):
        return False  # confirmations fall to the quiet branch — no speech thread in tests

    def stop(self):
        pass


class _Reflexes:
    """A recording stand-in for the learned-phrase store."""

    def __init__(self, learned=()):
        self._learned = set(learned)
        self.taught: list[tuple] = []

    def matches(self, command, kind):
        return kind == "sleep" and (command or "").strip().lower() in self._learned

    def learn(self, command, kind):
        self.taught.append((command, kind))
        self._learned.add((command or "").strip().lower())


def _voice(reflexes=None) -> WebVoice:
    v = WebVoice(_Settings(), _Stt(), _Tts(), reflexes=reflexes)
    # Deterministic gates: no real mic/device probing inside a test.
    v.can_listen = lambda: True
    v.enabled = lambda: True
    return v


def test_a_spoken_sleep_phrase_mutes_locally_without_a_model_turn():
    heard: list[str] = []
    muted: list[bool] = []
    v = _voice()
    v.on_recognized = heard.append
    v.on_muted = muted.append
    v._on_wake_text("hey HELIX go to sleep")
    assert v.is_muted() is True
    assert heard == []          # no model turn — the grammar answered locally
    assert muted == [True]      # and the UI heard about it


def test_a_learned_reflex_phrase_also_mutes():
    v = _voice(_Reflexes(learned={"engage privacy mode"}))
    v._on_wake_text("HELIX engage privacy mode")
    assert v.is_muted() is True  # yesterday's model judgment is today's instant reflex


def test_muted_ears_wake_only_on_an_explicit_address():
    v = _voice()
    v.set_muted(True, announce=False)
    v._on_muted_text("the wake word is HELIX")  # ABOUT it, not TO it
    assert v.is_muted() is True
    v._on_muted_text("hey HELIX")
    assert v.is_muted() is False


def test_learn_sleep_consolidates_only_novel_phrases():
    rf = _Reflexes()
    v = _voice(rf)
    v.learn_sleep("go to sleep")            # canonical — the grammar already catches it
    v.learn_sleep("engage privacy mode")    # novel — worth remembering
    assert rf.taught == [("engage privacy mode", "sleep")]


def test_deaf_ears_refuse_to_sleep():
    v = _voice()
    v.can_listen = lambda: False
    v.set_muted(True)
    assert v.is_muted() is False  # nothing was listening; 'asleep' would be a lie the UI then tells
