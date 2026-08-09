"""Package HELIX into a standalone desktop app with PyInstaller.

    pip install pyinstaller
    python build.py                 # -> dist/HELIX/HELIX.exe
    python build.py --with-voice    # also bundle the optional STT/TTS stack

NEVER bundles data/ — a shipped build starts blank (no keys, no history, nobody's apps).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "HELIX"


def _force_rmtree(path: Path) -> None:
    """Remove dist/<name> even when it holds read-only files — built apps keep a git repo, and git marks
    loose objects read-only, which PyInstaller's own --clean cannot delete on Windows (it crashes). We
    clear the read-only bit and retry. (Data under dist/<name>/data is NOT bundled; back it up first.)"""
    if not path.exists():
        return

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)

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
    # Bundle static UI assets (the Orbitron display font) next to the package so the frozen app loads
    # them via the same package-relative path it uses in dev.
    assets = ROOT / "helix" / "ui" / "assets"
    if assets.exists():
        args += ["--add-data", f"{assets}{os.pathsep}helix/ui/assets"]
    # The 3D baker loads these lazily at runtime (trimesh.boolean -> manifold3d; extrude -> shapely /
    # mapbox_earcut), so PyInstaller's static scan misses them — collect each in full.
    for pkg in ("trimesh", "shapely", "manifold3d", "mapbox_earcut"):
        args += ["--collect-all", pkg]
    # Knowledge document ingestion: pypdf is pure-python; python-docx (imported as `docx`) ships a default
    # template as DATA that a static scan misses — collect both in full so PDF/Word ingestion works frozen.
    for pkg in ("pypdf", "docx"):
        args += ["--collect-all", pkg]
    # Scanned-PDF OCR (services/ocr.py): pypdfium2_raw ctypes-loads pdfium.dll from a computed path — a
    # static scan can't see the DLL, so collect both halves in full. The winsdk WinRT modules are
    # imported lazily inside functions; name them explicitly so a hook regression can't drop them.
    for pkg in ("pypdfium2", "pypdfium2_raw"):
        args += ["--collect-all", pkg]
    for mod in ("winsdk.windows.media.ocr", "winsdk.windows.globalization",
                "winsdk.windows.graphics.imaging", "winsdk.windows.security.cryptography"):
        args += ["--hidden-import", mod]
    # The subscription brain imports the Agent SDK lazily (inside functions), so the static scan misses
    # it — pull in the CODE (submodules), not the data. --collect-all would sweep the SDK's bundled
    # _bundled/claude.exe (~248 MB) into every build; HELIX drives the DESKTOP APP's claude.exe via
    # cli_path, so that copy is dead weight. mcp ships JSON schema data the SDK loads, so collect it in
    # full.
    args += ["--collect-submodules", "claude_agent_sdk"]
    args += ["--collect-all", "mcp"]
    # anthropic is now imported lazily too (inside AnthropicChat._client_for_current_key — importing it
    # at module scope cost ~1.55s of startup for every launch, including subscription-rail launches that
    # never build an API client). Same reasoning as the Agent SDK above: name it explicitly rather than
    # trust the static scan to follow an import inside a method. Code only — it ships no bulky data.
    args += ["--collect-submodules", "anthropic"]
    # scipy 1.16 added a compiled helper (scipy._cyutility) that scipy.linalg/ndimage import at startup;
    # PyInstaller 6.12's bundled scipy hook predates it and skips it, so the frozen app dies importing
    # materials.py (gaussian_filter -> scipy.ndimage -> scipy.linalg -> _cyutility). Pull it in explicitly.
    args += ["--hidden-import", "scipy._cyutility"]
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

    # LIVE user data (keys, history, built apps) may still sit at dist/<name>/data from an older install
    # (new installs keep data in %LOCALAPPDATA%). Move it aside BEFORE the rmtree and restore it after —
    # a rebuild must never wipe the user's data, even if the build itself fails.
    live_data = ROOT / "dist" / NAME / "data"
    keep = ROOT / "dist" / f".{NAME}-data-keep"
    if live_data.is_dir():
        _force_rmtree(keep)  # a stale keep from an interrupted run — the fresher live data wins
        live_data.rename(keep)
        print(f"Preserved live user data: {live_data} -> {keep}")

    # Pre-clean dist/<name> ourselves (read-only-aware) so PyInstaller doesn't choke on built apps' .git.
    _force_rmtree(ROOT / "dist" / NAME)

    print("Running:", " ".join(args))
    try:
        result = subprocess.run(args, cwd=str(ROOT))
    finally:
        if keep.is_dir():  # restore even when the build failed — the data must never be stranded
            live_data.parent.mkdir(parents=True, exist_ok=True)
            keep.rename(live_data)
            print(f"Restored live user data: {keep} -> {live_data}")
    if result.returncode == 0:
        _unify_vc_runtime(ROOT / "dist" / NAME)
        print(f"\nBuilt dist/{NAME}/{NAME}.exe — data/ is NOT bundled; a fresh install starts blank.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
