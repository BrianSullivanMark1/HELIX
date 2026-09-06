"""Speech ports — optional. Null implementations make voice purely additive."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SpeechIn(Protocol):
    def available(self) -> bool: ...

    def ready(self) -> bool: ...  # True once the model is loaded (pre-warmed before Qt)

    def transcribe(self, wav_path: Path) -> str: ...


class SpeechOut(Protocol):
    def available(self) -> bool: ...

    # allow_fallback=False means "this voice or nothing" — used for progress narration so a transient
    # neural-TTS blip is skipped rather than spoken in a different (OS) voice mid-stream.
    def speak(self, text: str, allow_fallback: bool = True) -> None: ...

    def stop(self) -> None: ...

    # Optional: sleep-talk — the same voice quieter and slower, once, with no fallback. A backend
    # without it is simply not whispered through (the voice loop checks with getattr).
    # def murmur(self, text: str) -> None: ...
