"""HELIX launcher.

Thin entry point. Two things MUST happen here, in this order, before the app proper starts:

1. Single-instance check — so double/triple/multi-clicking the desktop icon can't spin up a second app.
   A duplicate launch just asks the already-running HELIX to come to the front and exits; it never even
   pays for the (heavy) voice pre-warm below. A self-relaunch (--relaunch, from restart/watchdog/self-
   heal) instead waits for the outgoing instance to release the lock and then takes over.
2. Voice STT pre-warm — MUST run before any PyQt6 import, because building faster-whisper's native
   runtime after QApplication initializes crashes the process on Windows. It runs only when the user has
   hands-free voice enabled, so a text-only first launch stays fast. The single-instance check above uses
   no Qt on Windows precisely so it can run ahead of this.
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

        paths = AppPaths.resolve()
        settings = JsonSettings(paths.settings_file)
        if settings.get("voice_input_on", False):
            speech.prewarm()
            try:
                # The neural speaker-recognition model (voice identity). Also pre-Qt for symmetry;
                # downloads once on the first voice-enabled launch, like whisper's weights. Failure
                # just leaves voice identity on its built-in DSP fallback.
                from helix.adapters import speaker_embed

                speaker_embed.prewarm(paths.data / "models")
            except Exception:
                pass
    except Exception:
        pass


def _run() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["watchdog"]:  # the crash-guard subprocess needs no singleton, no STT, and no Qt
        from helix.app.cli import main

        return main()

    # Single instance BEFORE the voice pre-warm and before any PyQt6 import (see module docstring).
    from helix.app.single_instance import become_primary_or_signal
    from helix.config import AppPaths

    is_relaunch = "--relaunch" in argv
    if not become_primary_or_signal(AppPaths.resolve().data, is_relaunch=is_relaunch):
        return 0  # another instance owns this data dir; we asked it to surface and now step aside

    _prewarm_voice_if_enabled()
    from helix.app.cli import main  # imported after prewarm — Qt loads lazily inside

    return main([a for a in argv if a != "--relaunch"])


if __name__ == "__main__":
    sys.exit(_run())
