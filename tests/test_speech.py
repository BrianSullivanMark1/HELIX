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
    e._synthesize = lambda text, gen=None: (setattr(e, "_stopped_gen", e._gen), "x.mp3")[1]  # type: ignore[method-assign]
    e._play = lambda p, gen=None: played.append(p)  # type: ignore[method-assign]
    e.speak("hello")
    assert played == [] and fb.spoke == []


def test_killed_playback_does_not_fall_back_to_os_voice():
    fb = _Fallback()
    e = _edge(fb)

    def play(_p, gen=None):  # simulate stop() killing the player mid-play
        e._stopped_gen = e._gen
        raise RuntimeError("killed")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = play  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == []  # the stop was intentional — no desktop voice


def test_real_playback_failure_still_falls_back():
    fb = _Fallback()
    e = _edge(fb)

    def boom(_p, gen=None):
        raise RuntimeError("real failure")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = boom  # type: ignore[method-assign]
    e.speak("hello")
    assert fb.spoke == ["hello"]  # genuine failure → OS voice, as intended


def test_narration_never_switches_to_the_os_voice():
    # Progress narration passes allow_fallback=False: a transient neural-TTS failure must SKIP the note,
    # not speak it in the desktop voice — otherwise consecutive notes flip between voices mid-build.
    fb = _Fallback()
    e = _edge(fb)

    def boom(_p, gen=None):
        raise RuntimeError("transient blip")

    e._synthesize = lambda text, gen=None: "x.mp3"  # type: ignore[method-assign]
    e._play = boom  # type: ignore[method-assign]
    e.speak("shaping the body", allow_fallback=False)
    assert fb.spoke == []  # stayed in one voice (skipped), never the OS voice


def test_a_later_utterance_cannot_unstop_an_earlier_killed_one():
    # The race #27 guards: while utterance A plays, B starts and stop()s A; A's killed playback must NOT
    # be mistaken for a failure and spoken in the OS voice just because B reset a shared flag.
    fb = _Fallback()
    e = _edge(fb)
    e._gen = 5  # pretend A is generation 5
    e._stopped_gen = 5  # stop() recorded A as stopped…
    e._gen = 6  # …and B (gen 6) then started
    assert e._is_stopped(5)  # A is still considered stopped
    assert not e._is_stopped(6)  # B is not
