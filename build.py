#!/usr/bin/env python
"""Build a standalone Windows HELIX.exe with PyInstaller — for the living-room tablet (§41).

    python build.py               # windowed dashboard build  -> dist\\HELIX\\HELIX.exe
    python build.py --with-voice  # also bundle the Xpert voice stack (faster-whisper / edge-tts)
    python build.py --console     # keep a console window (to debug a first build)
    python build.py --dry-run     # print the PyInstaller command, build nothing

Build ON Windows (PyInstaller makes a same-OS binary), then copy the whole dist\\HELIX\\ folder to the
tablet. On first launch HELIX creates data\\ NEXT TO the .exe; copy your existing
data\\helix_settings.json (Alpaca + Claude keys) and data\\helix.db into dist\\HELIX\\data\\ to carry
over your account + history. Requires: pip install pyinstaller."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "HELIX"
ENTRY = ROOT / "main.py"
# Heavy, native-dep voice packages (Xpert STT/TTS). They're imported LAZILY (helix/ai/transcribe.py,
# speech.py), so the app runs fine without them — excluded by default for a small, reliable build.
VOICE_PACKAGES = ("faster_whisper", "edge_tts")


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _obsolete_pathlib_backport() -> bool:
    """The PyPI `pathlib` backport (obsolete since Python 3.4) shadows the stdlib and makes PyInstaller
    abort the build. Detect it so the failure is self-explanatory instead of a cryptic traceback."""
    import importlib.metadata as md
    try:
        md.version("pathlib")
        return True
    except md.PackageNotFoundError:
        return False


def build_command(*, console: bool, with_voice: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", NAME,
        "--noconfirm",            # overwrite a previous dist without prompting
        "--clean",                # fresh build cache
        "--onedir",               # a folder (robust for big Qt apps; faster start than --onefile)
        "--console" if console else "--windowed",
        "--paths", str(ROOT),     # so `import helix...` resolves during analysis
    ]
    if with_voice:
        for pkg in VOICE_PACKAGES:
            if _installed(pkg):
                cmd += ["--collect-all", pkg]   # pull their native libs + data files
    else:
        for pkg in VOICE_PACKAGES:
            cmd += ["--exclude-module", pkg]    # voice off: dashboard-only, small + reliable
    cmd.append(str(ENTRY))
    return cmd


def main() -> int:
    flags = set(sys.argv[1:])
    cmd = build_command(console="--console" in flags, with_voice="--with-voice" in flags)
    print("PyInstaller command:\n  " + " ".join(cmd) + "\n")
    if "--dry-run" in flags:
        return 0
    if not _installed("PyInstaller"):
        print("PyInstaller is not installed. Run:  pip install pyinstaller")
        return 1
    if _obsolete_pathlib_backport():
        print("The obsolete 'pathlib' backport is installed and breaks PyInstaller.\n"
              "Fix it once with:  pip uninstall -y pathlib")
        return 1
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code != 0:
        print(f"\nBuild failed (exit {code}). Try `python build.py --console` to see the error"
              + (", or drop --with-voice." if "--with-voice" in flags else "."))
        return code
    exe = ROOT / "dist" / NAME / f"{NAME}.exe"
    print(f"\nBuilt: {exe}")
    print(r"Copy the whole dist\HELIX\ folder to the tablet, then copy your data\helix_settings.json")
    print(r"(keys) + data\helix.db into dist\HELIX\data\ to carry over your account + history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
