"""Speech ports — optional. Null implementations make voice purely additive."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SpeechIn(Protocol):
    def available(self) -> bool: ...

    def transcribe(self, wav_path: Path) -> str: ...


class SpeechOut(Protocol):
    def available(self) -> bool: ...

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...
