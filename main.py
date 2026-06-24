"""HELIX launcher.

Thin entry point. Voice STT pre-warming MUST happen here — before any PyQt6 import — because building
faster-whisper's native runtime after QApplication initializes crashes the process on Windows. It runs
only when the user has hands-free voice enabled, so a text-only first launch stays fast.
"""
from __future__ import annotations

import sys


def _prewarm_voice_if_enabled() -> None:
    """If hands-free voice is saved as on, load the local STT model now (before Qt). Best-effort and
    silent: any failure just leaves voice unavailable for this run. Imports here touch no Qt."""
    try:
        from helix.adapters import speech
        from helix.adapters.json_settings import JsonSettings
        from helix.config import AppPaths

        settings = JsonSettings(AppPaths.resolve().settings_file)
        if settings.get("voice_input_on", False):
            speech.prewarm()
    except Exception:
        pass


if __name__ == "__main__":
    _prewarm_voice_if_enabled()
    from helix.app.cli import main  # imported after prewarm — this pulls in PyQt6

    sys.exit(main())
