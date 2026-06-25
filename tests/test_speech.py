"""Speech adapter tests — the neural-TTS fallback must NOT fire when playback is stopped on purpose
(closing the app or saying 'stop'), or a desktop voice would keep talking after the window is gone."""
from __future__ import annotations

from helix.adapters.speech import EdgeSpeechOut, _rate_string


class _Fallback:
    def __init__(self) -> None:
        self.spoke: list[str] = []

    def available(self) -> bool:
        return True

    def speak(self, text: str) -> None:
        self.spoke.append(text)

    def stop(self) -> None:
        pass


def _edge(fb: _Fallback) -> EdgeSpeechOut:
    return EdgeSpeechOut(lambda: "en-GB-RyanNeural", lambda: 1.0, fallback=fb)


def test_rate_string():
    assert _rate_string(1.0) == "+0%"
    assert _rate_string(1.5) == "+50%"
    assert _rate_string(0.8) == "-20%"


def test_stopped_during_synth_neither_plays_nor_falls_back():
    fb = _Fallback()
    e = _edge(fb)
    played: list[str] = []
    e._synthesize = lambda text: (setattr(e, "_stopped", True), "x.mp3")[1]  # type: ignore[method-assign]
    e._play = lambda p: played.append(p)  # type: ignore[method-assign]
    e.speak("hello")
    assert played == [] and fb.spoke == []


def test_killed_playback_does_not_fall_back_to_os_voice():
    fb = _Fallback()
    e = _edge(fb)

    def play(_p):  # simulate stop() killing the player mid-play
        e._stopped = True
        raise RuntimeError("killed")

    e._synthesize = lambda text: "x.mp3"  # type: ignore[method-assign]
    e._play = play  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == []  # the stop was intentional — no desktop voice


def test_real_playback_failure_still_falls_back():
    fb = _Fallback()
    e = _edge(fb)

    def boom(_p):
        raise RuntimeError("real failure")

    e._synthesize = lambda text: "x.mp3"  # type: ignore[method-assign]
    e._play = boom  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == ["hello"]  # genuine failure → OS voice, as intended
