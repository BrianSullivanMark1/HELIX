"""Barge-in — the name "HELIX" interrupts speech; everything else in a noisy room does not.

Covers the echo test (HELIX must not wake on its own reply leaking into the mic) and the controller's
barge flow: wake+command redirects mid-sentence, chatter is ignored, stop still stops.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QApplication

from helix.ui.voice import VoiceController, is_echo

_app = QApplication.instance() or QApplication([])


class _Stt:
    def available(self) -> bool:
        return True

    def ready(self) -> bool:
        return True

    def transcribe(self, _path) -> str:
        return ""


class _Tts:
    def __init__(self) -> None:
        self.spoke: list[str] = []
        self.stops = 0

    def available(self) -> bool:
        return True

    def speak(self, text: str, allow_fallback: bool = True) -> None:
        self.spoke.append(text)

    def stop(self) -> None:
        self.stops += 1


class _Settings:
    def __init__(self) -> None:
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


def _controller() -> tuple[VoiceController, _Tts, list[str], list[int]]:
    tts = _Tts()
    ctrl = VoiceController(_Stt(), tts, _Settings())
    ctrl._run = lambda fn, on_done: (fn(lambda _s: None), on_done(""))  # run workers synchronously
    heard: list[str] = []
    stops: list[int] = []
    ctrl.recognized.connect(heard.append)
    ctrl.stopRequested.connect(lambda: stops.append(1))
    return ctrl, tts, heard, stops


# ---------- the echo test ----------

def test_wake_word_is_never_echo_when_reply_lacks_the_name():
    # TTS that never says "HELIX" cannot put the name in the mic — always the user.
    assert not is_echo("helix make it blue", "I've updated the button colour for you, sir.")
    assert not is_echo("helix", "The forecast is sunny all afternoon.")


def test_own_reply_containing_the_name_is_echo():
    spoken = "HELIX is ready to build that timer for you, sir."
    assert is_echo("helix is ready to build that timer", spoken)
    assert is_echo("helix", spoken)  # name + nothing fresh, and the reply says the name


def test_fresh_command_is_not_echo_even_when_reply_says_the_name():
    spoken = "HELIX is ready to build that timer for you, sir."
    assert not is_echo("helix actually paint the kitchen dashboard green", spoken)


def test_short_real_command_not_dropped_when_reply_says_the_name():
    # Regression: the wake word must not count as overlap evidence. "HELIX, make it red" against a reply
    # that also says "HELIX" would otherwise score the name as a free hit (2/3 >= 0.6) and be dropped.
    spoken = "As HELIX, I can make that change now, sir."
    assert not is_echo("helix make it red", spoken)  # only 'make' overlaps → 1/2 = 0.5 < 0.6, lands


def test_no_speech_means_no_echo():
    assert not is_echo("helix hello", "")


# ---------- the barge flow ----------

def test_name_plus_command_interrupts_and_redirects():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "speaking"
    ctrl._speaking_text = "the report shows revenue climbing in the third quarter"
    ctrl._on_barge_text("HELIX actually make it blue")
    assert tts.stops >= 1          # speech was cut off mid-sentence
    assert heard == ["actually make it blue"]
    assert ctrl._state == "thinking"
    assert stops == []             # a redirect is not a build-stop


def test_random_noisy_house_chatter_is_ignored():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "speaking"
    ctrl._speaking_text = "the report shows revenue climbing"
    ctrl._on_barge_text("can somebody let the dog out please")
    assert tts.stops == 0 and heard == [] and stops == []
    assert ctrl._state == "speaking"  # HELIX keeps talking


def test_own_voice_echo_does_not_interrupt():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "speaking"
    ctrl._speaking_text = "HELIX is ready to build that timer for you, sir."
    ctrl._on_barge_text("helix is ready to build that timer")
    assert tts.stops == 0 and heard == [] and ctrl._state == "speaking"


def test_short_stop_still_stops_without_the_name():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "speaking"
    ctrl._speaking_text = "here is the summary of your inbox"
    ctrl._on_barge_text("stop talking")
    assert tts.stops >= 1 and stops == [1] and heard == []


def test_bare_name_mid_speech_gets_an_acknowledgement():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "speaking"
    ctrl._speaking_text = "the long answer about the weather continues"
    ctrl._on_barge_text("HELIX")
    assert heard == []
    assert "Yes?" in tts.spoke  # acknowledged, now listening
    assert ctrl._session          # a session opened so the next utterance needs no wake word


def test_thinking_turn_cannot_be_redirected_only_stopped():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "thinking"
    ctrl._speaking_text = ""
    ctrl._on_barge_text("HELIX build a chess app")
    assert heard == [] and tts.stops == 0  # mid-turn redirect refused
    ctrl._on_barge_text("stop")
    assert stops == [1]  # but a stop always lands


def test_stale_speak_completion_cannot_reset_a_newer_turn():
    ctrl, tts, heard, stops = _controller()
    ctrl._state = "thinking"  # a barge-in already moved the turn on
    ctrl._speak_gen = 7
    ctrl._speak_done(7)  # the killed utterance finally unwinds
    assert ctrl._state == "thinking"  # and must NOT knock the new turn back to idle


def test_narration_never_clobbers_speaking_text_during_a_reply():
    # Regression: a build-progress note arriving while a reply is audibly playing must NOT overwrite the
    # echo slot (which would make HELIX judge its own reply against the note and obey it).
    ctrl, tts, heard, stops = _controller()
    ctrl._settings.set("voice_input_on", True)  # narrate() checks enabled()
    ctrl._state = "speaking"
    ctrl._speaking_text = "HELIX is opening the dashboard for you now"
    ctrl.narrate("wiring the layout")
    assert ctrl._speaking_text == "HELIX is opening the dashboard for you now"  # untouched
    assert "wiring the layout" not in tts.spoke  # the note was skipped, not queued behind the reply
