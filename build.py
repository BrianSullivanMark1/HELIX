"""Package HELIX into a standalone desktop app with PyInstaller.

    pip install pyinstaller
    python build.py                 # -> dist/HELIX/HELIX.exe
    python build.py --with-voice    # also bundle the optional STT/TTS stack

NEVER bundles data/ — a shipped build starts blank (no keys, no history, nobody's apps).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "HELIX"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    icon = ROOT / "assets" / "helix.ico"
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", NAME, "--onedir", "--windowed",
        "--collect-submodules", "helix",
    ]
    if icon.exists():
        args += ["--icon", str(icon)]
    if "--with-voice" in argv:
        args += ["--collect-all", "faster_whisper", "--collect-all", "edge_tts"]
    else:  # lean build — local STT off (the OS voice still works), keeps it small + fast
        args += ["--exclude-module", "faster_whisper", "--exclude-module", "edge_tts",
                 "--exclude-module", "torch"]
    args.append(str(ROOT / "main.py"))

    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode == 0:
        print(f"\nBuilt dist/{NAME}/{NAME}.exe — data/ is NOT bundled; a fresh install starts blank.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
