"""Voice tests — the pure pieces only (wake-word parsing + VAD segmentation). No Qt, no mic, no model.

These lock the logic that decides 'is HELIX being addressed?' and 'has an utterance finished?' — the two
places a regression would silently break hands-free voice.
"""
from __future__ import annotations

import array

import pytest

from helix.ui.voice import (
    STT_PREWARM_ERROR_SETTING,
    VadSegmenter,
    _pcm_rms,
    _wants_wake,
    build_wake_re,
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


def test_speakable_unglues_snake_case_and_expands_acronyms():
    # The "calawpee" fix: an underscore becomes a SPACE (never deleted), so a stray tool name is spoken
    # as words, and a few tech initialisms are said letter-by-letter instead of slurred.
    assert speakable("I'll call_api for that") == "I'll call A P I for that"
    assert speakable("using build_3d_model now") == "using build 3d model now"
    assert speakable("open the url please") == "open the U R L please"


def test_split_sentences_for_streamed_speech():
    from helix.ui.voice import split_sentences

    # a short reply stays one chunk (no behavior change)
    assert split_sentences("On it.") == ["On it."]
    # a multi-sentence reply splits so the first can play while the rest synthesize
    assert split_sentences("The report is ready. Revenue climbed. Want the details?") == [
        "The report is ready.", "Revenue climbed.", "Want the details?",
    ]
    # a tiny fragment merges forward instead of becoming a choppy lone chunk
    assert split_sentences("Yes. The build finished and it's in the menu now.") == [
        "Yes. The build finished and it's in the menu now.",
    ]
    # an abbreviation doesn't split mid-sentence
    assert split_sentences("Dr. Smith called about the permit. I'll follow up.") == [
        "Dr. Smith called about the permit.", "I'll follow up.",
    ]
    assert split_sentences("") == []


def test_wake_word_config_matches_a_custom_word_only():
    from helix.ui.voice import build_wake_re

    re_helix = build_wake_re(None)  # default falls back to the curated HELIX matcher
    assert split_wake("hey helix open the door", re_helix) == (True, "open the door")
    re_athena = build_wake_re("Athena")
    assert split_wake("Athena what's the time", re_athena)[0]
    assert split_wake("hey Athena, open the garage", re_athena) == (True, "open the garage")
    assert not split_wake("hey helix open the door", re_athena)[0]  # only the chosen word wakes now


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


def test_v3_sleep_forms_are_robust():
    # The everyday ways a person says "go quiet" all rest the mic.
    for phrase in ("goodnight", "good night", "night night", "nighty night", "bedtime",
                   "time to sleep", "time for bed", "it's time for bed", "go to bed",
                   "go rest", "rest now", "could you go to sleep", "can you go to sleep",
                   "go to sleep for a bit", "take a nap for a while"):
        assert is_sleep(phrase), phrase


def test_mentioning_the_sleep_command_is_not_a_command():
    # Explaining HELIX to a neighbor must never sleep it: mention is not use.
    for phrase in ("the command word is sleep", "you just tell it to go to sleep",
                   "if I say sleep it goes quiet", "it has a sleep mode you can use",
                   "I told it to take a nap yesterday"):
        assert not is_sleep(phrase), phrase


def test_asleep_wakes_only_on_an_explicit_address():
    # Whole-utterance wake phrases and the name LEADING a short address wake it...
    for phrase in ("wake up", "mic on", "HELIX", "hey HELIX", "HELIX, you there?",
                   "okay HELIX wake up", "HELIX?"):
        assert _wants_wake(phrase), phrase
    # ...but the name or 'wake'/'listen' buried in longer speech is ABOUT it, not TO it.
    for phrase in ("the wake word is HELIX", "you wake it by saying HELIX",
                   "and then HELIX wakes up and listens to you",
                   "it just keeps listening all day", "did you listen to the game",
                   "I could not wake the baby", "so anyway HELIX can build apps too"):
        assert not _wants_wake(phrase), phrase


def test_asleep_strictness_respects_a_custom_wake_word():
    rx = build_wake_re("Nimbus")
    assert _wants_wake("hey Nimbus", rx)
    assert _wants_wake("Nimbus wake up please", rx)
    assert not _wants_wake("our assistant is called Nimbus by the way", rx)


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


def test_the_prewarm_error_key_matches_the_launcher_that_writes_it():
    """voice.py spells the settings key out instead of importing it, because main.py is the frozen
    build's entry SCRIPT and isn't importable there. Spelled twice means it can drift, and a drift
    would be silent: the Console would read "" forever and go back to offering a restart that can't
    help. So the two spellings are pinned equal here."""
    import main as launcher

    assert STT_PREWARM_ERROR_SETTING == launcher.STT_PREWARM_ERROR
