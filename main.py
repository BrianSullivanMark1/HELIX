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

STT_PREWARM_ERROR = "stt_prewarm_error"  # settings key: why voice couldn't load, "" when it's fine


def _record_stt_prewarm(paths, settings, reason: str) -> None:
    """Leave a findable trace of a failed STT pre-warm — and clear it the moment one succeeds.

    Without this the failure is invisible: prewarm swallows its own exception, the UI reads "model not
    loaded" as "just needs a restart", and restarting re-runs the identical load, so the user is handed
    a Restart button that provably cannot help and helix.log says nothing at all. setup_logging is
    idempotent and is called again by the container seconds later, so bringing it forward here (only on
    the failure path — the happy path must stay as fast as it is) just means the line actually lands."""
    if reason:
        from helix.logging_setup import get_logger, setup_logging

        setup_logging(paths.log_file)
        get_logger("speech").warning("voice pre-warm failed — STT is unavailable this run: %s", reason)
        settings.set(STT_PREWARM_ERROR, reason)
    elif settings.get(STT_PREWARM_ERROR, ""):
        settings.set(STT_PREWARM_ERROR, "")  # whatever was wrong has been fixed; don't keep saying so


def _prewarm_voice_if_enabled() -> None:
    """If hands-free voice is saved as on, load the local STT model now (before Qt). Best-effort: a
    failure leaves voice unavailable for this run, but it is RECORDED rather than swallowed, so a
    permanently-dead mic can be diagnosed. Imports here touch no Qt."""
    try:
        from helix.adapters import speech
        from helix.adapters.json_settings import JsonSettings
        from helix.config import AppPaths

        paths = AppPaths.resolve()
        settings = JsonSettings(paths.settings_file)
        if not settings.get("voice_input_on", False):
            # Voice is off, so no pre-warm was ATTEMPTED — and a record that outlives its run is worse
            # than none. The console reads a non-empty record as "restarting provably cannot help" and
            # hides the Restart button; left standing after the user switched voice off and back on, it
            # would suppress the one offer that now works — the mirror image of the loop it exists to
            # end. The record only ever describes this run, so a run that did not try clears it.
            _record_stt_prewarm(paths, settings, "")
            return
        ok = speech.prewarm()
        _record_stt_prewarm(
            paths, settings,
            "" if ok else (speech.last_prewarm_error() or "the speech model could not be loaded"),
        )
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


def _ensure_streams() -> None:
    """A --windowed (GUI-subsystem) frozen app launched from Explorer/the taskbar has sys.stdout and
    sys.stderr set to None — and the FIRST library that touches one (uvicorn's log config calls
    sys.stdout.isatty()) kills the launch before a window ever appears. Give the app real, silent
    streams so every print()/isatty() in any dependency is harmless. Launched from a terminal the
    streams exist and this is a no-op; child workers (cadworker/watchdog) get pipes from their
    spawner and are no-ops too."""
    import io
    import os

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except OSError:
                setattr(sys, name, io.StringIO())


def _run() -> int:
    _ensure_streams()  # BEFORE anything that might print or probe a stream (see docstring)
    argv = sys.argv[1:]
    if argv[:1] == ["watchdog"]:  # the crash-guard subprocess needs no singleton, no STT, and no Qt
        from helix.app.cli import main

        return main()
    if argv[:1] == ["cadworker"]:  # the hologram compile worker: no singleton, no STT, no Qt —
        from helix.cad import runner  # spawned by the Build123dCad adapter, never run by hand

        return runner.main(argv[1:])

    # Single instance BEFORE the voice pre-warm and before any PyQt6 import (see module docstring).
    from helix.app.single_instance import become_primary_or_signal
    from helix.config import AppPaths

    is_relaunch = "--relaunch" in argv
    if not become_primary_or_signal(AppPaths.resolve().data, is_relaunch=is_relaunch):
        # Another instance owns this data dir. In the browser-tab world a repeat icon click means
        # "show me HELIX" — open a tab on the running backend before stepping aside (a no-op for
        # the qt/headless modes, whose surfacing is the activation ping above).
        if not any(a in ("qt", "watchdog", "cadworker", "--headless") for a in argv):
            from helix.app.cli import open_running_face  # no Qt on this path

            open_running_face()
        return 0

    _prewarm_voice_if_enabled()
    from helix.app.cli import main  # imported after prewarm — Qt loads lazily inside

    return main([a for a in argv if a != "--relaunch"])


if __name__ == "__main__":
    sys.exit(_run())
