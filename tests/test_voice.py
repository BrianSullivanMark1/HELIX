"""Voice tests — the pure pieces only (wake-word parsing + VAD segmentation). No Qt, no mic, no model.

These lock the logic that decides 'is HELIX being addressed?' and 'has an utterance finished?' — the two
places a regression would silently break hands-free voice.
"""
from __future__ import annotations

import array

import pytest

from helix.ui.voice import (
    VadSegmenter,
    _pcm_rms,
    device_id_str,
    is_dismissal,
    is_sleep,
    is_stop,
    is_wake,
    speakable,
    split_visuals,
    split_wake,
)


class _FakeDevice:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def id(self) -> bytes:
        return self._raw


def test_device_id_str_round_trips_every_byte():
    # latin-1 keeps all 256 byte values, so a saved id matches the same device exactly on the next run.
    raw = bytes(range(256))
    encoded = device_id_str(_FakeDevice(raw))
    assert encoded.encode("latin-1") == raw
    assert device_id_str(_FakeDevice(b"")) == ""


def _peak(pcm: bytes) -> int:
    a = array.array("h")
    a.frombytes(pcm)
    return max((abs(s) for s in a), default=0)


def test_normalize16_lifts_quiet_speech_and_leaves_loud_alone():
    pytest.importorskip("numpy")
    from helix.ui.voice import _normalize16

    quiet = array.array("h", [1500, -1500] * 400).tobytes()
    boosted = _normalize16(quiet)
    assert 4 * 1500 <= _peak(boosted) <= 32767  # meaningfully lifted toward full scale, never clips over

    loud = array.array("h", [30000, -30000] * 400).tobytes()
    assert _normalize16(loud) == loud  # already near full scale — untouched

    assert _normalize16(b"") == b""  # empty is a no-op
    silence = array.array("h", [0] * 100).tobytes()
    assert _normalize16(silence) == silence  # pure silence — no divide-by-zero, no boost


def _pcm(amplitude: int, samples: int) -> bytes:
    return array.array("h", [amplitude] * samples).tobytes()


def test_split_wake_matches_and_returns_command():
    assert split_wake("HELIX build me a timer") == (True, "build me a timer")
    assert split_wake("hey helix, what's the weather") == (True, "what's the weather")
    assert split_wake("okay helix") == (True, "")  # bare wake, no command


def test_split_wake_tolerates_mishearings():
    matched, command = split_wake("heelix open the menu")
    assert matched and command == "open the menu"


def test_split_wake_ignores_unaddressed_text():
    assert split_wake("just talking to myself") == (False, "")
    assert split_wake("") == (False, "")


def test_speakable_strips_markdown_and_symbols():
    assert speakable("Here's the **plan**: ship it") == "Here's the plan: ship it"
    assert speakable("use `code` and a # heading") == "use code and a heading"
    assert speakable("- one\n- two\n- three") == "one two three"
    assert speakable("See [the docs](https://x.com) now") == "See the docs now"
    # ordinary words and punctuation are untouched
    assert speakable("Hello, sir. Ready when you are?") == "Hello, sir. Ready when you are?"


def test_split_visuals_extracts_table_and_leaves_spoken_prose():
    reply = 'Here are the quarters.\n```viz\n{"type":"table","columns":["Q"],"rows":[["Q1"]]}\n```'
    spoken, specs = split_visuals(reply)
    assert spoken == "Here are the quarters."
    assert len(specs) == 1 and specs[0]["type"] == "table"


def test_split_visuals_chart_inline_and_never_spoken():
    reply = 'Revenue climbed.\n```viz {"type":"chart","data":[{"label":"Q1","value":10}]} ```'
    spoken, specs = split_visuals(reply)
    assert spoken == "Revenue climbed."
    assert specs[0]["type"] == "chart"
    # speakable also strips a stray viz block, so the numbers are never read aloud
    spoken_aloud = speakable(reply)
    assert "value" not in spoken_aloud and "viz" not in spoken_aloud


def test_split_visuals_ignores_malformed_or_unknown_blocks():
    assert split_visuals('Oops.\n```viz\nnot json\n```') == ("Oops.", [])
    assert split_visuals("just a normal answer") == ("just a normal answer", [])


def test_is_stop():
    for phrase in ("stop", "stop talking", "be quiet", "never mind", "cancel that", "shut up", "that's enough"):
        assert is_stop(phrase), phrase
    assert not is_stop("build a stopwatch app")  # 'stopwatch' must not trigger
    assert not is_stop("show me the canceled orders")
    assert not is_stop("")


def test_is_stop_includes_explicit_build_stop_phrases():
    for phrase in ("stop build", "stop the build", "stop building", "cancel build", "cancel the build",
                   "abort", "halt"):
        assert is_stop(phrase), phrase
    assert not is_stop("build a halting problem demo")  # not a whole-utterance stop


def test_is_sleep_and_wake():
    for phrase in ("sleep", "go to sleep", "going to sleep", "go to sleep now", "sleep mode", "take a nap",
                   "mute", "mute the mic", "stop listening", "pause listening", "mic off"):  # legacy phrasings still work
        assert is_sleep(phrase), phrase
    for phrase in ("wake", "wake up", "wake up now", "wakey wakey", "awake", "you awake",
                   "unmute", "un mute", "un-mute", "unmuted", "resume listening", "start listening",
                   "start listening again", "mic on", "you can listen again"):  # legacy + real mis-hearings
        assert is_wake(phrase), phrase
    # sleep/wake and stop never cross-fire (sleep must NOT stop a build, stop must NOT sleep the mic)
    assert not is_sleep("stop") and not is_stop("sleep")
    assert not is_sleep("build a sleep timer app")  # not a whole-utterance sleep
    assert not is_wake("wake me up at seven")        # not a whole-utterance wake
    assert not is_wake("") and not is_sleep("")


def test_is_dismissal():
    assert is_dismissal("goodbye")
    assert is_dismissal("that's all")
    assert is_dismissal("thanks HELIX")
    assert not is_dismissal("hello there")
    assert not is_dismissal("")


def test_pcm_rms_constant_signal():
    assert abs(_pcm_rms(_pcm(3000, 200)) - 3000) < 1.0
    assert _pcm_rms(b"") == 0.0


def test_vad_emits_a_completed_utterance_after_trailing_silence():
    seg = VadSegmenter()
    # Loud speech well above the adaptive threshold, longer than the minimum, then >3s of silence.
    assert seg.push(_pcm(4000, 6000)) is None  # speech started, not yet ended
    utter = seg.push(_pcm(0, 60000))           # 60000 samples = 120000 bytes ≈ 3.75s of silence
    assert utter is not None and len(utter) > 0


def test_vad_drops_a_too_short_blip():
    seg = VadSegmenter()
    seg.push(_pcm(4000, 400))            # ~25 ms — below WAKE_MIN_SPEECH_S
    assert seg.push(_pcm(0, 60000)) is None
