"""Speech adapters — optional. Voice is purely additive; null fallbacks keep text-only fully working.

SpeechIn  : local, private STT via faster-whisper (audio never leaves the machine).
SpeechOut : the OS voice (Windows System.Speech / macOS `say`) — local, no extra dependency.
Both degrade to a null implementation when unavailable, so nothing here is ever a hard requirement.

The STT model is cached at MODULE level (not per-instance) so it can be pre-warmed before Qt starts:
on Windows, building faster-whisper's native ctranslate2 runtime AFTER QApplication has initialized
triggers a native access-violation crash. `prewarm()` is therefore called from main.py before any Qt
import; the container's WhisperSpeechIn then reuses the already-loaded model.
"""
from __future__ import annotations

import platform
import subprocess
import threading
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("speech")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_STT_MODEL = "base.en"  # good CPU default: fast, English-only, decent on short spoken commands

# One model instance per size, shared across every WhisperSpeechIn. Heavy to build (and downloads
# weights on first use), so we keep it alive once loaded.
_MODELS: dict[str, object] = {}


def stt_importable() -> bool:
    """True if faster-whisper is installed (does NOT load a model — that is deferred to prewarm)."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _build_model(model_size: str, device: str = "cpu"):
    from faster_whisper import WhisperModel
    # int8 on CPU is the lightest, broadly-compatible setting; first construction downloads weights.
    return WhisperModel(model_size, device=device, compute_type="int8")


def prewarm(model_size: str = DEFAULT_STT_MODEL, device: str = "cpu") -> bool:
    """Load + cache the STT model now and report whether it is ready. Never raises.

    MUST be called from the desktop entry point BEFORE constructing QApplication (see module docstring):
    building ctranslate2 after Qt is up crashes the process on Windows. Best-effort — returns False if
    faster-whisper isn't installed or the model can't be built, so the caller can keep voice disabled."""
    if not stt_importable():
        return False
    try:
        if model_size not in _MODELS:
            _MODELS[model_size] = _build_model(model_size, device)
        return True
    except Exception:
        return False


def stt_ready(model_size: str = DEFAULT_STT_MODEL) -> bool:
    """True if the model is already loaded in-process, so transcribe() will NOT construct it now."""
    return model_size in _MODELS


# ----- speech-in (STT) -----
class WhisperSpeechIn:
    """Local STT via faster-whisper, using the module-level (pre-warmed) model cache."""

    def __init__(self, model_size: str = DEFAULT_STT_MODEL, device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device

    def available(self) -> bool:
        """True if faster-whisper is importable (the engine could be used)."""
        return stt_importable()

    def ready(self) -> bool:
        """True if the model is already loaded (pre-warmed), so transcribe won't build it after Qt."""
        return stt_ready(self._model_size)

    def transcribe(self, wav_path: Path) -> str:
        # A single failure returns "" and is forgotten — one bad clip must NOT disable voice for the
        # session (the controller re-checks availability on every settle to decide whether to re-arm).
        try:
            model = _MODELS.get(self._model_size)
            if model is None:
                model = _build_model(self._model_size, self._device)
                _MODELS[self._model_size] = model
            # beam_size=1 (greedy) is fastest and plenty for short spoken commands.
            segments, _info = model.transcribe(str(wav_path), language="en", beam_size=1)
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            _LOG.warning("transcription failed: %s", exc)
            return ""


class NullSpeechIn:
    def available(self) -> bool:
        return False

    def ready(self) -> bool:
        return False

    def transcribe(self, wav_path: Path) -> str:
        return ""


# ----- speech-out (TTS) -----
class OsSpeechOut:
    """The built-in OS voice. Local, no network, no extra dependency. Text is piped via stdin.

    speak() BLOCKS until the utterance finishes, so a caller running it on a worker thread learns
    exactly when speech ends (the voice controller uses this to re-arm the mic only once HELIX has
    stopped talking, so it never transcribes its own reply). stop() — called from any thread — kills
    the process, which unblocks an in-flight speak() for instant barge-in.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()  # speak() runs on a worker; stop() on the UI thread

    def available(self) -> bool:
        return platform.system() in ("Windows", "Darwin")

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self.stop()
            return
        system = platform.system()
        proc: subprocess.Popen | None = None
        try:
            with self._lock:  # kill any prior utterance and publish the new handle atomically
                self._kill_locked()
                if system == "Windows":
                    script = (
                        "Add-Type -AssemblyName System.Speech;"
                        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                        ".Speak([Console]::In.ReadToEnd())"
                    )
                    proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-Command", script],
                        stdin=subprocess.PIPE, text=True, encoding="utf-8", creationflags=_NO_WINDOW,
                    )
                    self._proc = proc
                elif system == "Darwin":
                    proc = subprocess.Popen(["say", text])
                    self._proc = proc
            if proc is not None and system == "Windows" and proc.stdin is not None:
                proc.stdin.write(text)
                proc.stdin.close()
            if proc is not None:
                proc.wait()  # block until the utterance completes (or stop() kills it)
        except Exception as exc:
            _LOG.warning("TTS failed: %s", exc)

    def stop(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


class NullSpeechOut:
    def available(self) -> bool:
        return False

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass
