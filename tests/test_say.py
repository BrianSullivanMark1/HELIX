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
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=v), "  Hello Brian.  ", synth=None)
    assert r == {"ok": True, "spoken": True} and v.calls == ["Hello Brian."]
    assert pushed[0]["t"] == "msg" and pushed[0]["role"] == "helix" and pushed[0]["text"] == "Hello Brian."
    assert len(pushed) == 1                      # the voice pushes its own orb states


def test_without_a_voice_the_orb_is_mimed_at_reading_pace():
    pushed = []
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "one two three", synth=None)
    assert r["ok"] and r["spoken"] is False and r["seconds"] == 1.8
    assert [p["t"] for p in pushed] == ["msg", "orb"] and pushed[1]["state"] == "speaking"
    time.sleep(2.0)
    assert pushed[-1] == {"t": "orb", "state": "idle"}


def test_a_voice_whose_tts_is_unavailable_falls_back_to_the_mime():
    pushed = []
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=_Voice(speaks=False)), "x", synth=None)
    assert r["spoken"] is False and pushed[1] == {"t": "orb", "state": "speaking"}


def test_empty_is_refused_and_long_lines_are_cut():
    pushed = []
    assert say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "   ", synth=None)["ok"] is False
    say_mod.say(SimpleNamespace(push=pushed.append, voice=None), "a" * 1000, synth=None)
    assert len(pushed[0]["text"]) == say_mod.MAX_CHARS


# ------------------------------------------------------------------------------ word-timed

def _fake_synth(text, voice, rate):
    words = []
    t = 0.1
    for w in text.split():
        words.append({"t": round(t, 3), "d": 0.3, "w": w}); t += 0.35
    return b"ID3fake-mp3-" + voice.encode() + rate.__repr__().encode(), words


def test_the_synthesized_line_carries_word_timing_and_is_served_back():
    from fastapi import FastAPI
    from helix.api import say as say_mod
    pushed = []
    settings = {"tts_voice": "en-US-GuyNeural", "tts_rate": 1.1}
    lines = say_mod.Lines()
    shell = SimpleNamespace(push=pushed.append, voice=_Voice())
    r = say_mod.say(shell, "Hello Brian, the build is live.", settings=SimpleNamespace(get=settings.get), synth=_fake_synth, lines=lines)
    assert r["ok"] and r["spoken"] == "page" and r["url"] == f"/api/say/audio/{r['id']}"
    assert [w["w"] for w in r["words"]] == ["Hello", "Brian,", "the", "build", "is", "live."]
    assert r["seconds"] == round(0.1 + 5 * 0.35 + 0.3, 2)
    assert shell.voice.calls == []                       # the page speaks; the voice loop stays quiet
    assert pushed[0]["t"] == "msg" and len(pushed) == 1
    assert lines.get(r["id"]).startswith(b"ID3fake-mp3-en-US-GuyNeural")


def test_when_the_synthesizer_fails_the_voice_loop_speaks_instead():
    def broken(text, voice, rate):
        raise RuntimeError("offline")
    pushed = []
    v = _Voice()
    r = say_mod.say(SimpleNamespace(push=pushed.append, voice=v), "x y", synth=broken, lines=say_mod.Lines())
    assert r == {"ok": True, "spoken": True} and v.calls == ["x y"]


def test_rate_string_and_the_audio_route():
    assert say_mod.rate_string(1.0) == "+0%" and say_mod.rate_string(1.1) == "+10%" and say_mod.rate_string(0.8) == "-20%"
    from fastapi import FastAPI
    app = FastAPI()
    say_mod.mount_say(app, SimpleNamespace(push=lambda e: None, voice=None), SimpleNamespace(get=lambda k, d=None: None), synth=_fake_synth)
    paths = {r.path for r in app.routes}
    assert "/api/say" in paths and "/api/say/audio/{sid}" in paths
