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


def _build_web_face() -> Path | None:
    """Build the React face (web/ -> web/dist) so the frozen app can serve it as helix/webui.
    Needs npm at BUILD time only — the shipped app serves static files. A missing npm (or a failed
    build) falls back to any existing dist; none at all means the frozen app runs backend-only."""
    web = ROOT / "web"
    dist = web / "dist"
    if not (web / "package.json").is_file():
        return dist if (dist / "index.html").is_file() else None
    npm = shutil.which("npm")
    if npm is None:
        print("npm not found — shipping the existing web/dist as-is." if (dist / "index.html").is_file()
              else "npm not found and no web/dist — the frozen app will be backend-only.")
        return dist if (dist / "index.html").is_file() else None
    print("Building the web face (npm run build)…")
    result = subprocess.run([npm, "run", "build"], cwd=str(web))
    if result.returncode != 0:
        print("web build FAILED — shipping the existing dist if any.")
    return dist if (dist / "index.html").is_file() else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # The glowing 'Contained Star' orb icon (assets/make_orb_icon.py); falls back to the old mark.
    icon = ROOT / "assets" / "orb.ico"
    if not icon.exists():
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
    # THE WEB FACE: the built React app rides as helix/webui (webboot._web_dist's frozen location).
    web_dist = _build_web_face()
    if web_dist is not None:
        args += ["--add-data", f"{web_dist}{os.pathsep}helix/webui"]
    # The web shell's server + window + mic. fastapi/uvicorn are imported at module scope on the web
    # path; uvicorn's workers/loops resolve dynamically, so collect it in full. pywebview loads its
    # Windows backend (winforms/WebView2) dynamically — collect it whole. sounddevice ctypes-loads a
    # bundled PortAudio DLL its hook knows how to carry.
    for pkg in ("uvicorn", "webview", "sounddevice"):
        args += ["--collect-all", pkg]
    args += ["--collect-submodules", "fastapi"]
    # THE CAD KERNEL (holograms): build123d rides on OCP — a ~300 MB compiled OCCT binding whose DLLs
    # a static scan cannot follow (the runner subprocess is the only importer, and it re-invokes
    # HELIX.exe as `cadworker`). Collect the whole stack; without it every hologram in the frozen app
    # dies at compile time — the failure mode the lazy-import rule warns about.
    for pkg in ("build123d", "OCP", "ocpsvg", "ezdxf"):
        args += ["--collect-all", pkg]
    # matplotlib is an OPTIONAL dep of the CAD stack (ezdxf/build123d 2D previews) that nothing in
    # HELIX imports — and PyInstaller's matplotlib hook CRASHES the whole build on this machine (its
    # isolated import hits a numpy/matplotlib ABI mismatch). Excluding it prunes the hook entirely;
    # the cad worker's compile path (core + Mesher + STL/STEP exporters) never touches it.
    args += ["--exclude-module", "matplotlib"]
    # numpy 2.x keeps its real internals under numpy._core, and the stock hook shipped a bundle
    # missing numpy._core._exceptions — the frozen cadworker then died with "CAD engine isn't
    # available" on every hologram. Collect numpy in full; the wheels are already on disk.
    args += ["--collect-all", "numpy"]
    # scipy is BACK (build123d.topology imports scipy.optimize), and with it the old friend this
    # file's history warned about: scipy 1.16's compiled helper `scipy._cyutility`, which
    # PyInstaller 6.12's hook skips — the frozen cadworker then dies importing scipy.linalg. Name
    # it explicitly again, and collect scipy's submodules so the next helper can't hide either.
    args += ["--collect-submodules", "scipy", "--hidden-import", "scipy._cyutility"]
    # …and the REST of build123d's module-scope import surface, swept by AST once instead of being
    # discovered one runtime crash at a time (numpy → scipy → lib3mf was the trail). Its __init__
    # star-imports every submodule, so everything installed that those import must ship: lib3mf
    # ctypes-loads a DLL, fontTools/sympy/sklearn carry data or compiled pieces, the rest are small.
    # (vtkmodules is in its source too but NOT installed here — build123d guards it — so it is not
    # named: collecting a missing package fails the build.)
    for pkg in ("lib3mf", "anytree", "fontTools", "ocp_gordon", "svgpathtools", "sympy",
                "trianglesolver", "webcolors", "sklearn", "IPython"):
        args += ["--collect-all", pkg]
    # The retired primitive engine loaded these lazily at runtime (trimesh.boolean -> manifold3d;
    # extrude -> shapely / mapbox_earcut), so PyInstaller's static scan misses them — collect each in
    # full. Holograms are compiled by the OpenSCAD CLI now (a separate program, found or winget-installed
    # at runtime — nothing to bundle) and nothing in helix/ imports this stack today; it is still
    # collected while requirements.txt still lists it, so the frozen app and a pip install agree.
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
    # (scipy._cyutility used to be named here as a hidden import: the retired services/materials.py
    # imported scipy.ndimage, whose 1.16 compiled helper PyInstaller 6.12's hook skipped. That module and
    # the scipy requirement are gone, so there is nothing to pull in — and naming a package that is not
    # installed would make PyInstaller warn on every build.)
    if icon.exists():
        args += ["--icon", str(icon)]
    # VOICE IS PART OF THE PRODUCT and ships BY DEFAULT. The old polarity (--with-voice opt-in) is how
    # the 2026-09-03 rebuild silently shipped a mute HELIX: a plain `python build.py` excluded
    # faster_whisper and edge_tts, every utterance logged "No module named 'faster_whisper'", and the
    # neural TTS fell back to the OS voice. `--no-voice` is the explicit opt-out for a lean build.
    if "--no-voice" in argv:
        args += ["--exclude-module", "faster_whisper", "--exclude-module", "edge_tts"]
    else:
        import importlib.util as _ilu

        # faster-whisper AND its full runtime surface: ctranslate2 (the native engine), onnxruntime
        # (the VAD), tokenizers + huggingface_hub (model load/download), av (its audio decoder import).
        # Collect ONLY what is installed — collect-all on a missing package fails the whole build (the
        # vtkmodules lesson), and a machine without a piece should build lean-er, not not-at-all.
        for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers",
                    "huggingface_hub", "av", "edge_tts"):
            if _ilu.find_spec(pkg) is not None:
                args += ["--collect-all", pkg]
            else:
                print(f"NOTE: voice dep '{pkg}' not installed — building without it")
    # torch is excluded in EVERY profile: nothing on the voice path needs it (faster-whisper runs on
    # ctranslate2), only the neural speaker-id would reach for it and that has a documented DSP
    # fallback — while collecting it would add gigabytes.
    args += ["--exclude-module", "torch"]
    # The Bambu printer adapter imports paho.mqtt lazily (inside the tool call), so the static scan
    # never sees it — name it, or the frozen "print it" dies with ModuleNotFoundError. paho is a
    # namespace package, so collect submodules rather than collect-all.
    args += ["--collect-submodules", "paho", "--hidden-import", "paho.mqtt.client"]
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
