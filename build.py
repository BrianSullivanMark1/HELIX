"""Package HELIX into a standalone desktop app with PyInstaller.

    pip install pyinstaller
    python build.py                 # -> dist/HELIX/HELIX.exe
    python build.py --with-voice    # also bundle the optional STT/TTS stack

NEVER bundles data/ — a shipped build starts blank (no keys, no history, nobody's apps).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "HELIX"

# Standard MSVC runtime DLLs. PyQt6 bundles an OLD copy (14.26); ctranslate2/onnxruntime want a newer
# one. Windows loads Qt's copy first, then the voice stack calls into it and access-violates on launch.
# Unifying every copy to the system's newest (forward-compatible) fixes it.
_VC_RUNTIME = (
    "MSVCP140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll", "MSVCP140_atomic_wait.dll",
    "MSVCP140_codecvt_ids.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "CONCRT140.dll",
)


def _unify_vc_runtime(dist_dir: Path) -> None:
    """Replace every bundled VC++ runtime DLL with the system's newest, so PyQt6's older copy can't be
    loaded ahead of what the voice stack needs (the launch crash). No-op off Windows / if absent."""
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    src = {n.lower(): system32 / n for n in _VC_RUNTIME if (system32 / n).exists()}
    if not src or not dist_dir.exists():
        return
    count = 0
    for path in dist_dir.rglob("*.dll"):
        source = src.get(path.name.lower())
        if source is not None:
            try:
                shutil.copy2(source, path)
                count += 1
            except OSError:
                pass
    print(f"Unified {count} VC++ runtime DLL(s) to the system's newest (launch-crash fix).")


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
        # Collect faster-whisper AND its native deps (ctranslate2; onnxruntime drives the VAD) — without
        # them the frozen app imports faster_whisper but crashes loading the model.
        for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "edge_tts"):
            args += ["--collect-all", pkg]
    else:  # lean build — local STT off (the OS voice still works), keeps it small + fast
        args += ["--exclude-module", "faster_whisper", "--exclude-module", "edge_tts",
                 "--exclude-module", "torch"]
    args.append(str(ROOT / "main.py"))

    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode == 0:
        _unify_vc_runtime(ROOT / "dist" / NAME)
        print(f"\nBuilt dist/{NAME}/{NAME}.exe — data/ is NOT bundled; a fresh install starts blank.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
