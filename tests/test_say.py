"""The test line: a HELIX bubble, then the real speak path - or a timed mime without a voice."""
from __future__ import annotations

import time
from types import SimpleNamespace

from helix.api import say as say_mod


class _Voice:
    def __init__(self, speaks=True):
        self.calls, self._state, self._speaks = [], "idle", speaks

    def speak(self, text):
        self.calls.append(text)
        self._state = "speaking" if self._speaks else "idle"

    def state(self):
        return self._state


def test_with_a_voice_the_line_is_bubbled_then_spoken():
    pushed = []
    v = _Voice()
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=v), "  Hello Brian.  ")
    assert r == {"ok": True, "spoken": True} and v.calls == ["Hello Brian."]
    assert pushed[0]["t"] == "msg" and pushed[0]["role"] == "helix" and pushed[0]["text"] == "Hello Brian."
    assert len(pushed) == 1                      # the voice pushes its own orb states


def test_without_a_voice_the_orb_is_mimed_at_reading_pace():
    pushed = []
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "one two three")
    assert r["ok"] and r["spoken"] is False and r["seconds"] == 1.8
    assert [p["t"] for p in pushed] == ["msg", "orb"] and pushed[1]["state"] == "speaking"
    time.sleep(2.0)
    assert pushed[-1] == {"t": "orb", "state": "idle"}


def test_a_voice_whose_tts_is_unavailable_falls_back_to_the_mime():
    pushed = []
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=_Voice(speaks=False)), "x")
    assert r["spoken"] is False and pushed[1] == {"t": "orb", "state": "speaking"}


def test_empty_is_refused_and_long_lines_are_cut():
    pushed = []
    assert say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "   ")["ok"] is False
    say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "a" * 1000)
    assert len(pushed[0]["text"]) == say_mod.MAX_CHARS
