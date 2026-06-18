from __future__ import annotations


def _prewarm_speech_before_qt() -> None:
    """Load the speech-to-text model BEFORE PyQt6 is imported anywhere in this process.

    faster-whisper/ctranslate2 segfaults the process (access violation 0xC0000005) if its native model
    is built AFTER Qt's native libraries are loaded — and even a bare `import PyQt6` is enough to trip it
    (§23). main.py is the earliest point we control, so we pre-load the STT model here, before importing
    the CLI (whose `ui` command lazily pulls in PyQt6 for the desktop app). Initializing ctranslate2
    first, then letting Qt load on top, avoids the conflict.

    If Qt is already loaded — e.g. running under a debugger whose Qt-support imports PyQt before main.py
    even runs — we skip, so the app still STARTS (it does not crash at launch). `is_ready()` is then
    False and the Xpert voice paths disable themselves; voice works on a normal (non-debugger) launch.
    Best-effort and silent: nothing here may stop the app from starting.
    """
    import sys

    if any(
        name == "PyQt6" or name.startswith("PyQt6.") or name == "PySide6" or name.startswith("PySide6.")
        for name in list(sys.modules)
    ):
        return
    try:
        from helix.ai.transcribe import is_available, prewarm

        if is_available():
            prewarm()
    except Exception:
        pass


def main() -> int:
    _prewarm_speech_before_qt()  # MUST run before the CLI import chain pulls in PyQt6 (§23)
    from helix.interfaces.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
