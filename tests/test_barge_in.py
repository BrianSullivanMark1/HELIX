"""The focus shield + the echo test.

The echo test (is_echo) still guards HELIX from ever mistaking its own reply for a command. The barge
contract CHANGED: HELIX no longer lets the room interrupt it by voice while it is thinking, speaking, or
building — the mic is gated deaf during work and a deliberate stop is UI-only (tap the orb, Esc, or the
Stop button). These tests lock the new behavior: the mic only captures a command when HELIX is genuinely
idle, and ambient speech during work is dropped.
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
    # Run workers synchronously AND propagate the work's result to on_done (like the real QtWorker), so
    # a transcription flows through to the wake handler.
    ctrl._run = lambda fn, on_done: on_done(fn(lambda _s: None))
    heard: list[str] = []
    stops: list[int] = []
    ctrl.recognized.connect(heard.append)
    ctrl.stopRequested.connect(lambda: stops.append(1))
    return ctrl, tts, heard, stops


# ---------- the echo test (unchanged: HELIX must never obey its own reply) ----------

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
    spoken = "As HELIX, I can make that change now, sir."
    assert not is_echo("helix make it red", spoken)  # only 'make' overlaps → 1/2 = 0.5 < 0.6, lands


def test_no_speech_means_no_echo():
    assert not is_echo("helix hello", "")


# ---------- the focus shield: HELIX is deaf while it works ----------

def test_command_is_captured_when_idle():
    # Sanity: when genuinely idle, an utterance is transcribed and becomes a command.
    ctrl, _tts, heard, _stops = _controller()
    ctrl._stt.transcribe = lambda _p: "hey helix what time is it"
    ctrl._state = "idle"
    ctrl._on_utterance(b"\x20\x10" * 8000)
    assert heard == ["what time is it"]


def test_mic_is_deaf_while_thinking():
    ctrl, _tts, heard, _stops = _controller()
    ctrl._stt.transcribe = lambda _p: "hey helix build a chess app"
    ctrl._state = "thinking"  # a turn is already in flight
    ctrl._on_utterance(b"\x20\x10" * 8000)
    assert heard == [] and ctrl._state == "thinking"  # the utterance was dropped, not transcribed


def test_mic_is_deaf_while_speaking():
    ctrl, _tts, heard, stops = _controller()
    ctrl._stt.transcribe = lambda _p: "stop talking"
    ctrl._state = "speaking"
    ctrl._on_utterance(b"\x20\x10" * 8000)
    # Even "stop" doesn't cut in by voice anymore — stopping is a deliberate UI action.
    assert heard == [] and stops == [] and ctrl._state == "speaking"


def test_mic_is_deaf_while_working_on_a_background_build():
    ctrl, _tts, heard, _stops = _controller()
    ctrl._stt.transcribe = lambda _p: "hey helix cancel that"
    ctrl._state = "idle"          # a background build leaves the conversational state idle…
    ctrl.set_working(True)        # …so the working flag is what shields the mic
    ctrl._on_utterance(b"\x20\x10" * 8000)
    assert heard == [] and ctrl._state == "idle"


def test_set_working_clears_and_lets_commands_through_again():
    ctrl, _tts, heard, _stops = _controller()
    ctrl._stt.transcribe = lambda _p: "hey helix what's the weather"
    ctrl.set_working(True)
    ctrl.set_working(False)  # build finished — the shield lifts
    ctrl._state = "idle"
    ctrl._on_utterance(b"\x20\x10" * 8000)
    assert heard == ["what's the weather"]


# ---------- speak-state bookkeeping (unchanged) ----------

def test_stale_speak_completion_cannot_reset_a_newer_turn():
    ctrl, _tts, _heard, _stops = _controller()
    ctrl._state = "thinking"  # a newer turn already moved on
    ctrl._speak_gen = 7
    ctrl._speak_done(7)  # the killed utterance finally unwinds
    assert ctrl._state == "thinking"  # and must NOT knock the new turn back to idle


def test_narration_never_clobbers_speaking_text_during_a_reply():
    # A build-progress note arriving while a reply is audibly playing must NOT overwrite the echo slot.
    ctrl, tts, _heard, _stops = _controller()
    ctrl._settings.set("voice_input_on", True)  # narrate() checks enabled()
    ctrl._state = "speaking"
    ctrl._speaking_text = "HELIX is opening the dashboard for you now"
    ctrl.narrate("wiring the layout")
    assert ctrl._speaking_text == "HELIX is opening the dashboard for you now"  # untouched
    assert "wiring the layout" not in tts.spoke  # the note was skipped, not queued behind the reply
