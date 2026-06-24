"""Speech adapters — optional. Voice is purely additive; null fallbacks keep text-only fully working.

SpeechIn  : local, private STT via faster-whisper (audio never leaves the machine).
SpeechOut : the OS voice (Windows System.Speech / macOS `say`) — local, no extra dependency.
Both degrade to a null implementation when unavailable, so nothing here is ever a hard requirement.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("speech")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----- speech-in (STT) -----
class WhisperSpeechIn:
    """Local STT via faster-whisper. The model loads lazily on first use."""

    def __init__(self, model_size: str = "base", device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device
        self._model = None
        self._broken = False

    def available(self) -> bool:
        if self._broken:
            return False
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def transcribe(self, wav_path: Path) -> str:
        try:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self._model_size, device=self._device, compute_type="int8")
            segments, _info = self._model.transcribe(str(wav_path))
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            _LOG.warning("transcription failed: %s", exc)
            self._broken = True
            return ""


class NullSpeechIn:
    def available(self) -> bool:
        return False

    def transcribe(self, wav_path: Path) -> str:
        return ""


# ----- speech-out (TTS) -----
class OsSpeechOut:
    """The built-in OS voice. Local, no network, no extra dependency. Text is piped via stdin."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def available(self) -> bool:
        return platform.system() in ("Windows", "Darwin")

    def speak(self, text: str) -> None:
        self.stop()
        text = (text or "").strip()
        if not text:
            return
        system = platform.system()
        try:
            if system == "Windows":
                script = (
                    "Add-Type -AssemblyName System.Speech;"
                    "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                    ".Speak([Console]::In.ReadToEnd())"
                )
                self._proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", script],
                    stdin=subprocess.PIPE, text=True, encoding="utf-8", creationflags=_NO_WINDOW,
                )
                self._proc.stdin.write(text)  # type: ignore[union-attr]
                self._proc.stdin.close()  # type: ignore[union-attr]
            elif system == "Darwin":
                self._proc = subprocess.Popen(["say", text])
        except Exception as exc:
            _LOG.warning("TTS failed: %s", exc)

    def stop(self) -> None:
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
