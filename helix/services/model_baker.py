"""ModelBaker — compile a hologram's model.py into a mesh and wrap it in a technical-illustration viewer.

A hologram is a PROGRAM. The coder writes `model.py` (build123d — a real B-rep CAD kernel, in Python,
millimetres, a `# --- Parameters ---` block at the top, a design-brief docstring, geometry inside
build()) and HELIX does the rest here: the static lints + safety gate, the compile through the
CadEngine port (a worker subprocess writes STL + STEP + 3MF + the preview in ONE run), the render the
vision critic looks at, and the single fixed viewer page (`index.html`). The coder never touches the
viewer — "make it 100 wide" is an edit to a named parameter in source, which is what makes verbal
design accurate. Python is the language LLMs write best, and the helix_parts library carries real
hardware footprints (Arduino/ESP32/Pi/relays), which is what makes an enclosure come out FITTING.
model.scad (the retired OpenSCAD engine) is migrated on the next edit, like the primitive engine was.

Three entry points, all called by the Forge on a worker thread and all NEVER raising — together they are
one BAKE CYCLE, and the Forge owns it:

  - prepare(workspace) — before the coder runs. Seeds the helper library (helix.scad) into the workspace,
    so a coder that lists the folder finds the ONLY library it is told about, and opens a fresh cycle for
    this workspace (the critic's one look is per cycle — see _Record.checks).
  - check(workspace)   — the pre-finalize gate for MODEL builds, after each coder pass. Lints, refreshes the
    helper library, compiles to assets/model.stl, renders assets/preview.png and (on the FIRST check of
    the cycle only) asks the critic. Returns a problem string for the Forge's one-pass repair loop, or None.
  - bake(workspace)    — after the gate passes. Reuses check's compile (the record is keyed by the
    source's sha256 plus the library, so the same text is never compiled twice), writes the exports, the
    base64 STL sidecar the viewer reads over file://, the vendored three.js, and the viewer page; and it
    closes the cycle however it ends.

The viewer is a TECHNICAL ILLUSTRATION, not a product shot: flat matcap shading with crease-edge lines on
dark slate, a millimetre grid, axes, bounding-box dimensions, a section plane, wireframe, the parameter
panel, and export links. It is self-contained on purpose — the vendored three.js r128 UMD build is
copied beside it and every piece of data is inlined or loaded via <script src> — because Chrome refuses
fetch()/XHR of local files over file:// and the same page must open as a plain file in a browser tab AND
inside HELIX's QWebEngineView. No CDN, no bloom, no image-based lighting, no tone-mapping boost: the old
glossy rig was "way too bright", and a drawing you can measure is what a designer wants to see.

Everything that is not the new engine stays as it was: a 360° ENVIRONMENT (model.json, engine
"environment") is still a Blockade panorama in a skybox viewer; an explicit Tripo REFERENCE (model.json,
engine "neural") is still a GLB in a small viewer — a likeness to look at, not the design; a hand-authored
ANIMATED index.html is left alone with the render kit beside it. A workspace from the retired primitive
engine (model.json with "parts" and no model.scad) gets a friendly page asking for a redesign — never a
crash, never a blank page.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from helix.domain import cadpy as scad  # the design-language module (aliased: same surface names)
from helix.logging_setup import get_logger
from helix.ports.cad import CadEngine
from helix.services import render_kit
from helix.services.builds import MANIFEST  # single source of truth for the manifest filename

_LOG = get_logger("model_baker")

# Optional hosted text/image-to-3D backend (the Tripo REFERENCE). Given (prompt, image_path|None) -> GLB bytes.
NeuralBackend = Callable[[str, Path | None], bytes]
# Optional hosted text-to-360°-panorama backend (environments/scenes). Given prompt -> image bytes.
SkyboxBackend = Callable[[str], bytes]
# The vision critic: (preview_png_path, brief_text) -> ONE short problem sentence, or None if it looks right.
Critic = Callable[[Path, str], str | None]

SPEC_FILE = "model.json"                 # environments, references, and the retired primitive engine
SOURCE_FILE = "model.py"                 # THE design (build123d)
LEGACY_SOURCE = "model.scad"             # the retired OpenSCAD design — migrated on the next edit
STL_REL = "assets/model.stl"             # compiled mesh — the viewer's and the printer's food
STL_JS_REL = "assets/model.stl.js"       # the same STL as `window.HELIX_STL = "<base64>"` (file:// safe)
MF_REL = "assets/model.3mf"              # slicer-friendly export, best effort
STEP_REL = "assets/model.step"           # the B-rep export — Bambu Studio / Fusion eat this natively
PREVIEW_REL = "assets/preview.png"       # what the vision critic looks at
THREE_REL = "assets/three.min.js"        # the vendored three.js r128 UMD build, copied beside the page
GLB_REL = "assets/model.glb"             # a Tripo reference mesh
PANO_REL = "assets/panorama.jpg"         # the equirectangular 360° image for an environment scene
VIEWER_FILE = "index.html"
# Stamped into every generated viewer so bake() can tell its OWN page from a hand-authored animated one.
VIEWER_SENTINEL = "<!-- HELIX-GENERATED-VIEWER -->"

DEFAULT_BG = "#10161c"                   # dark slate — a drawing board, not a showroom
DEFAULT_ACCENT = "#3fe0e0"               # HELIX accent, for the chrome only — never for the model

# The page's fallback when no vendored three.js path is handed in (a bare ModelBaker() in a test, or a
# container that forgot). Stated in the page as a comment so a stray CDN reference is never a mystery.
THREE_CDN = "https://unpkg.com/three@0.128.0/build/three.min.js"

# The Forge runs the coder at most TWICE per build (the build, then one repair pass) and calls check()
# after each; prepare() opens the cycle before the first pass and bake() closes it, whatever happens in
# between. This ceiling is the fallback for a baker driven WITHOUT the Forge (a bare baker in a test, a
# future caller that forgets prepare): a third check on the same workspace can then only be a NEW build,
# so a cycle nobody closed cannot leave "already critiqued" behind forever.
_CHECKS_PER_CYCLE = 2

# Best-effort extras are skipped when the STL compile was already slow: the 3MF export is a second full
# CGAL compile and the preview is a third, so a heavy model would otherwise wait three times as long for
# files the viewer does not need. The preview gets the larger budget because the critic needs it.
_MF_BUDGET_S = 20.0
_PREVIEW_BUDGET_S = 90.0

_NO_ENGINE_HINT = (
    "Holograms are computed by the build123d CAD kernel — free, about a minute to install — and it "
    "isn't set up in this environment yet; ask HELIX to install it."
)


class SpecError(Exception):
    """The model.json was missing, malformed, or described nothing we can build."""


@dataclass
class _Record:
    """What the baker remembers about one workspace across a bake cycle (prepare → check → bake).

    The artefact fields are keyed by `sha` (the source text + the helper library) so the same text is
    never compiled twice — they outlive the cycle on purpose; `checks` implements "the critic speaks only
    on the FIRST check of a bake cycle" — its verdict must always leave the Forge's one repair pass
    available, so a picky critic can never fail and roll back a design that compiles on that repair
    pass. prepare() zeroes it when a build begins and bake() zeroes it however the build ends."""

    sha: str = ""
    stl_ok: bool = False
    stl_seconds: float = 0.0
    preview_ok: bool = False
    mf_ok: bool = False
    checks: int = 0          # checks seen this cycle; the critic speaks only on the first


class ModelBaker:
    def __init__(
        self,
        cad: CadEngine | None = None,
        *,
        three_js: Path | None = None,
        neural_backend: NeuralBackend | None = None,
        neural_available=None,
        skybox_backend: SkyboxBackend | None = None,
        skybox_available=None,
        critic: Critic | None = None,
    ) -> None:
        # `cad` is the hologram engine behind the port (OpenSCAD CLI today). None means "no engine wired"
        # and behaves exactly like an engine that is not installed: check() passes, bake() writes the
        # install page. `three_js` is the vendored three.min.js, handed in by the container as a plain
        # Path because a service must not import ui.
        self._cad = cad
        self._three_js = Path(three_js) if three_js else None
        self._critic = critic
        # neural_backend is the hosted Tripo REFERENCE. The container wires it UNCONDITIONALLY (it raises
        # only at call time if no key), so `self._neural is not None` is NOT a real availability check —
        # `neural_available()` is (it reflects a live Tripo key). Defaults to wired==available for tests.
        self._neural = neural_backend
        self._neural_available = neural_available or (lambda: neural_backend is not None)
        # skybox_backend is the hosted environment/scene path (Blockade Labs): a whole 360° PLACE, shown
        # as a skybox. Same wired-unconditionally / availability-reflects-a-live-key pattern as neural.
        self._skybox = skybox_backend
        self._skybox_available = skybox_available or (lambda: skybox_backend is not None)
        # Per-workspace compile records. Builds of DIFFERENT holograms may run on a few worker threads at
        # once (same-name builds serialize), so the dict is guarded; the records themselves are only ever
        # touched by the one thread building that workspace.
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    # ----- engine state -----
    def engine_missing(self) -> bool:
        """True when there is nothing to compile with — no engine wired, or one that isn't installed.
        Cheap (available() spawns nothing) and never raises."""
        if self._cad is None:
            return True
        try:
            return not self._cad.available()
        except Exception:  # noqa: BLE001 — a probing hiccup reads as "missing", never as a crash
            _LOG.warning("cad.available() raised", exc_info=True)
            return True

    def _install_hint(self) -> str:
        if self._cad is None:
            return _NO_ENGINE_HINT
        try:
            return self._cad.install_hint() or _NO_ENGINE_HINT
        except Exception:  # noqa: BLE001
            return _NO_ENGINE_HINT

    def _engine_version(self) -> str:
        if self._cad is None:
            return ""
        try:
            return self._cad.version() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _record(self, workspace: Path) -> _Record:
        key = str(Path(workspace).resolve())
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = self._records[key] = _Record()
            return rec

    # ----- opening the cycle -----
    def prepare(self, workspace: Path) -> None:
        """Called by the Forge for every MODEL build — new or iterating — once the workspace exists and
        BEFORE the coder runs. Two jobs, both idempotent, and it never raises:

        1. Seed helix.scad beside where model.scad will go. The coder's prompt names it as the ONLY
           library here; before this, the file was first written by check() — AFTER the coder — so on a
           fresh hologram a coder that listed the folder found no helix.scad and could reinvent the
           helpers or skip `use <helix.scad>` altogether.
        2. Open a fresh bake cycle for this workspace, so the critic's one look lands on THIS build's
           first check. Resetting here — not only in bake() — is what keeps a cycle that never reached
           bake() (a repair pass that was cancelled, died, or escaped; an engine that was missing) from
           leaving "already critiqued" behind, where the next build of the same hologram would run with
           first=False and never hear the critic."""
        try:
            ws = Path(workspace)
            self._write_lib(ws)
            self._record(ws).checks = 0
        except Exception:  # noqa: BLE001 — a preparation hiccup must never fail a build
            _LOG.warning("prepare failed unexpectedly", exc_info=True)

    # ----- the pre-finalize gate -----
    def check(self, workspace: Path) -> str | None:
        """The Forge's pre-finalize check for MODEL builds. Returns a problem string for the repair loop,
        or None when the work passes. Never raises: a baker bug must not be able to fail a build, so an
        unexpected exception is logged and reads as a pass (bake() will then show what it can)."""
        try:
            return self._check(Path(workspace))
        except Exception:  # noqa: BLE001
            _LOG.warning("model check failed unexpectedly", exc_info=True)
            return None

    def _check(self, ws: Path) -> str | None:
        # Count this check FIRST, before any early return: the critic may only speak on the FIRST check
        # of a cycle (see _CHECKS_PER_CYCLE), whatever that first check ends up saying. A first check that
        # found no model.scad at all, a retired model.json, or a lint or compile failure still means the
        # repair pass is the LAST pass — and a critic speaking on the repaired design would roll back a
        # model that compiles. Counting lower down let exactly that happen: the early returns skipped the
        # counter, so the check after the repair was treated as check one and the critic spoke.
        rec = self._record(ws)
        if rec.checks >= _CHECKS_PER_CYCLE:
            rec.checks = 0   # a new build of this hologram (a baker driven without the Forge's prepare)
        rec.checks += 1
        first = rec.checks == 1
        src = ws / SOURCE_FILE
        if not src.is_file():
            if (ws / LEGACY_SOURCE).is_file():
                # The retired OpenSCAD engine. Asking for model.py here lets the Forge's repair pass
                # MIGRATE the design in the same build ("make it wider" on an old hologram just
                # works); if that pass fails too, the Forge rolls back and the old page keeps working.
                return (
                    "model.scad is the retired OpenSCAD format — redraw the design as model.py "
                    "(build123d, millimetres, geometry inside build()); the old model.scad is ignored."
                )
            spec = self._read_spec(ws)
            if spec is None:
                if (ws / SPEC_FILE).is_file():
                    return ("model.json is not valid JSON — write the design as model.py instead "
                            "(a hologram is a build123d program).")
                viewer = ws / VIEWER_FILE
                if viewer.is_file() and not self._is_generated_viewer(viewer):
                    return None  # a hand-authored ANIMATED page: the Forge's HTML/py gate covers it
                return "no model.py was produced"
            if str(spec.get("engine", "")).lower() in ("environment", "neural"):
                return None  # a 360° scene or a Tripo reference: nothing to compile
            # Anything else is the retired primitive format (a 'parts' list, engine 'auto'/'parametric').
            return (
                "model.json with a 'parts' list is the retired primitive format — write the design as "
                "model.py (build123d, millimetres) instead; the old model.json is ignored."
            )
        source = src.read_text(encoding="utf-8", errors="replace")
        lints = scad.inspect_source(source)
        if lints:
            return " ".join(lints)
        # The helper library lives nowhere else: prepare() seeds it before the coder runs, and it is
        # refreshed here too so `use <helix.scad>` resolves (the engine runs with cwd at the source) even
        # in an old workspace nobody prepared — and an upgraded HELIX never compiles against a stale one.
        self._write_lib(ws)
        if self.engine_missing():
            return None  # not the coder's fault; bake() shows the install page
        sha = _sha(source)
        if rec.sha != sha:
            rec.sha, rec.stl_ok, rec.preview_ok, rec.mf_ok, rec.stl_seconds = sha, False, False, False, 0.0
        stl = ws / STL_REL
        if not (rec.stl_ok and stl.is_file()):
            res = self._cad.compile_stl(src, stl)  # type: ignore[union-attr]
            if not res.ok:
                rec.stl_ok = False
                return self._compile_problem(res)
            rec.stl_ok, rec.stl_seconds = True, float(res.seconds or 0.0)
        # The preview is best effort — a render hiccup is NOT the coder's problem — and the critic, when
        # wired, gets ONE look per bake cycle. A second check (after the repair pass) never re-critiques.
        png = self._render_preview(ws, src, rec)
        if self._critic is None or not first or png is None:
            return None
        try:
            verdict = self._critic(png, self._brief_text(source))
        except Exception:  # noqa: BLE001 — a failing critic must never fail a build
            _LOG.warning("hologram critic raised", exc_info=True)
            return None
        verdict = (verdict or "").strip()
        if not verdict:
            return None
        return (
            f"Looking at the rendered preview ({PREVIEW_REL}): {verdict.rstrip('.')}. "
            f"Fix the model so it matches the brief."
        )

    def _compile_problem(self, res) -> str:
        """The repair-loop string for a failed compile: the warm sentence, then the compiler's own words
        fenced as DATA (the coder needs file:line to fix it; the user only ever hears the sentence)."""
        problem = (res.problem or "The hologram's source couldn't be compiled.").strip()
        detail = (res.detail or "").strip()
        return f"{problem} The engine said: {detail}" if detail else problem

    def _render_preview(self, ws: Path, src: Path, rec: _Record) -> Path | None:
        """assets/preview.png for THIS source, or None. Best effort: a failed render is not a problem, and
        a stale picture of an older design is removed so neither the critic nor the page can see it."""
        png = ws / PREVIEW_REL
        if rec.preview_ok and png.is_file():
            return png
        if rec.stl_seconds > _PREVIEW_BUDGET_S:
            _unlink(png)
            return None
        try:
            res = self._cad.render_png(src, png)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            _LOG.warning("preview render raised", exc_info=True)
            res = None
        rec.preview_ok = bool(res is not None and res.ok and png.is_file())
        if not rec.preview_ok:
            _unlink(png)
        return png if rec.preview_ok else None

    @staticmethod
    def _brief_text(source: str) -> str:
        """What the critic is told the model is supposed to be: the header brief plus the parameter block,
        as plain lines. The critic judges the picture against THIS, not against the raw source."""
        brief = scad.parse_brief(source)
        lines: list[str] = []
        if brief.get("title"):
            lines.append(f"Design: {brief['title']}")
        if brief.get("summary") and brief["summary"] != brief.get("title"):
            lines.append(f"Summary: {brief['summary']}")
        if brief.get("parts"):
            lines.append("Parts: " + ", ".join(brief["parts"]))
        params = scad.parse_params(source)
        if params:
            lines.append("Parameters:")
            for p in params:
                rng = ""
                if p.minimum is not None or p.maximum is not None:
                    rng = f" [{_num(p.minimum)}..{_num(p.maximum)}]"
                elif p.choices:
                    rng = " [" + ", ".join(p.choices) + "]"
                desc = f" — {p.description}" if p.description else ""
                lines.append(f"  {p.name} = {p.value}{rng}{desc}")
        return "\n".join(lines) or "(the source carries no design brief)"

    # ----- public entry point -----
    def bake(self, workspace: Path) -> None:
        """Turn the workspace's design into the exports + the viewer page, in place.

        Called by ForgeService AFTER the coder runs, the escape guard passes and check() is happy, BEFORE
        finalize — so the baked index.html is what _detect_entry finds and what gets committed. Never
        raises: every failure becomes a friendly page so the build still completes and the user sees a
        clear message."""
        ws = Path(workspace)
        try:
            self._bake(ws)
        except Exception as exc:  # noqa: BLE001 — never let a baking bug fail the whole build
            _LOG.warning("bake failed unexpectedly", exc_info=True)
            try:
                self._write_error(ws, f"Couldn't build the hologram: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            # EVERY way out of a bake closes the cycle — the install notice, a bake-time compile failure,
            # an environment or a reference, a baking bug — not only the happy path. Closing it from inside
            # _bake_scad alone left the engine-missing notice and every non-SCAD path with the count still
            # standing, so the first build after OpenSCAD landed was taken for "check 2" and never heard
            # the critic.
            self._end_cycle(ws)

    def _bake(self, ws: Path) -> None:
        src = ws / SOURCE_FILE
        spec_path = ws / SPEC_FILE
        viewer = ws / VIEWER_FILE
        if src.is_file():
            self._bake_scad(ws, src)   # THE design — model.scad always wins
            return
        if not spec_path.exists():
            # No design and no spec: this is the hand-authored ANIMATED path (the coder wrote index.html
            # itself), or a build that produced nothing. Leave a real page alone (and ship the render kit
            # it imports); otherwise explain. Our OWN leftover page (the sentinel) is not a result — a
            # stale viewer of an older design must not be shown as if the build had made it.
            if not viewer.exists() or self._is_generated_viewer(viewer):
                self._write_error(ws, "The hologram build produced no model.py and no page of its own.")
            else:
                self._write_render_kit(ws)
            return
        # A static→animated CONVERSION: the coder replaced our generated viewer with its own animated
        # index.html. Respect it — skip baking and drop the now-stale model.json, so a re-bake doesn't
        # silently overwrite the animation with an old scene.
        if viewer.exists() and not self._is_generated_viewer(viewer):
            try:
                spec_path.unlink()
            except OSError:
                pass
            self._write_render_kit(ws)
            return
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise SpecError("model.json must be a JSON object.")
            engine = str(spec.get("engine", "")).lower()
            if engine == "environment":
                self._bake_environment(ws, spec)   # a 360° scene, not a mesh
            elif engine == "neural":
                self._bake_reference(ws, spec)     # an explicit Tripo likeness
            elif self._is_legacy_spec(spec):
                self._write_legacy_page(ws, spec)  # the retired primitive engine
            else:
                raise SpecError(
                    "The hologram has no model.py — the design is a build123d program, and none was written."
                )
        except SpecError as exc:
            self._write_error(ws, str(exc))
        except json.JSONDecodeError:
            self._write_error(ws, "model.json isn't valid JSON.")

    # ----- the OpenSCAD design -----
    def _bake_scad(self, ws: Path, src: Path) -> None:
        source = src.read_text(encoding="utf-8", errors="replace")
        self._write_lib(ws)
        brief = scad.parse_brief(source)
        params = scad.parse_params(source)
        title = self._title(ws, {}, brief)
        if self.engine_missing():
            self._write_notice(
                ws, title=title, heading="The hologram engine isn't installed yet",
                message=self._install_hint(), brief=brief, source=source,
                note="The design itself is finished and saved; once the engine is installed, ask for any "
                     "change and HELIX will compile and show it.",
            )
            return
        rec = self._record(ws)
        sha = _sha(source)
        stl = ws / STL_REL
        if not (rec.sha == sha and rec.stl_ok and stl.is_file()):
            rec.sha, rec.stl_ok, rec.preview_ok, rec.mf_ok, rec.stl_seconds = sha, False, False, False, 0.0
            res = self._cad.compile_stl(src, stl)  # type: ignore[union-attr]
            if not res.ok:
                self._write_notice(
                    ws, title=title, heading="This hologram didn't compile",
                    message=res.problem or "The hologram's source couldn't be compiled.",
                    brief=brief, source=source,
                    note="Ask for a small change and HELIX will repair the design.",
                )
                return
            rec.stl_ok, rec.stl_seconds = True, float(res.seconds or 0.0)
        has_3mf = self._export_3mf(ws, src, rec)
        has_preview = self._render_preview(ws, src, rec) is not None
        has_step = (ws / STEP_REL).is_file()  # written by the engine's one-run artifact set
        stl_bytes = stl.read_bytes()
        assets = ws / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        # The sidecar is how the viewer gets the mesh over file://: a <script src> is allowed where a
        # fetch() of a local file is not. The plain .stl stays beside it for the export link.
        (ws / STL_JS_REL).write_text(
            "window.HELIX_STL=\"" + base64.b64encode(stl_bytes).decode("ascii") + "\";\n",
            encoding="utf-8",
        )
        three_src = self._copy_three(ws)
        data = {
            "title": title,
            "summary": brief.get("summary", ""),
            "parts": list(brief.get("parts") or []),
            "params": [dataclasses.asdict(p) for p in params],
            "files": {"stl": STL_REL, "mf": MF_REL if has_3mf else "", "scad": SOURCE_FILE,
                      "step": STEP_REL if has_step else "",
                      "preview": PREVIEW_REL if has_preview else ""},
            "engine": self._engine_version(),
            "source": source,
        }
        page = (
            _VIEWER_HTML
            .replace("__TITLE__", _esc(title))
            .replace("__THREE_SRC__", three_src)
            .replace("__THREE_NOTE__", "" if three_src == THREE_REL else _CDN_NOTE)
            .replace("__STL_JS__", STL_JS_REL)
            .replace("__DATA__", _json_for_script(data))   # last: the data may contain anything
        )
        (ws / VIEWER_FILE).write_text(page, encoding="utf-8")

    def _end_cycle(self, ws: Path) -> None:
        """bake() closes the cycle: the next check of this workspace is a new build's first. Never
        raises (it runs in bake()'s finally, where an exception would escape the never-raises promise)."""
        try:
            self._record(ws).checks = 0
        except Exception:  # noqa: BLE001
            _LOG.warning("could not close the bake cycle", exc_info=True)

    def _export_3mf(self, ws: Path, src: Path, rec: _Record) -> bool:
        """Best effort, by contract. Returns whether assets/model.3mf matches THIS source — a stale file
        from an older compile (the engine leaves `out` untouched on failure) is removed rather than linked."""
        out = ws / MF_REL
        if rec.mf_ok and out.is_file():
            return True
        if rec.stl_seconds > _MF_BUDGET_S:
            _unlink(out)
            return False
        try:
            res = self._cad.export_3mf(src, out)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            _LOG.warning("3MF export raised", exc_info=True)
            res = None
        rec.mf_ok = bool(res is not None and res.ok and out.is_file())
        if not rec.mf_ok:
            _unlink(out)
        return rec.mf_ok

    def _copy_three(self, ws: Path) -> str:
        """Put the vendored three.js beside the page (idempotent) and return the src the page should use.
        Falls back to the CDN only when no vendored file was handed in — and says so in the page."""
        if self._three_js is None:
            return THREE_CDN
        dst = ws / THREE_REL
        try:
            if not self._three_js.is_file():
                return THREE_CDN
            if not (dst.is_file() and dst.stat().st_size == self._three_js.stat().st_size):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(self._three_js.read_bytes())
            return THREE_REL
        except OSError:
            _LOG.warning("could not copy three.js into the workspace", exc_info=True)
            return THREE_CDN

    def _write_lib(self, ws: Path) -> None:
        try:
            lib = ws / scad.HELIX_LIB_FILE
            if not (lib.is_file() and lib.read_text(encoding="utf-8", errors="replace") == scad.HELIX_LIB):
                lib.write_text(scad.HELIX_LIB, encoding="utf-8")
        except OSError:
            _LOG.warning("could not write %s", scad.HELIX_LIB_FILE, exc_info=True)

    # ----- environment (360° scene) -----
    def _bake_environment(self, workspace: Path, spec: dict) -> None:
        """engine="environment": generate a 360° panorama for the scene and wrap it in a skybox viewer.
        There's no mesh — the whole PLACE is the image, mapped onto the inside of a sphere the user looks
        around in. A missing key or a generation failure becomes a friendly page, like any other build."""
        prompt = str(spec.get("prompt") or spec.get("title") or "").strip()
        if not prompt:
            self._write_error(workspace, "A 360° environment needs a 'prompt' describing the scene.")
            return
        if self._skybox is None or not self._skybox_available():
            self._write_error(
                workspace,
                "360° scenes need Blockade Labs — ask HELIX to connect Blockade and a secure key "
                "panel opens. (For a single object instead, ask for that object.)")
            return
        try:
            img = self._skybox(prompt)
        except Exception as exc:  # noqa: BLE001 — surface the scene generator's message, never crash
            self._write_error(workspace, f"Couldn't generate the scene: {exc}")
            return
        if not img:
            self._write_error(workspace, "The scene generator returned nothing.")
            return
        try:
            (workspace / "assets").mkdir(parents=True, exist_ok=True)
            (workspace / PANO_REL).write_bytes(img)
            try:  # a converted-from-mesh scene shouldn't keep a stale GLB around
                (workspace / GLB_REL).unlink()
            except OSError:
                pass
            self._write_skybox_viewer(workspace, spec)
        except OSError as exc:
            self._write_error(workspace, f"Couldn't save the scene: {exc}")

    def _write_skybox_viewer(self, workspace: Path, spec: dict) -> None:
        title = self._title(workspace, spec)
        accent = _hex_str(spec.get("accent"), DEFAULT_ACCENT)
        (workspace / VIEWER_FILE).write_text(
            _SKYBOX_HTML.replace("__TITLE__", _esc(title)).replace("__ACCENT__", accent)
            .replace("__PANO__", PANO_REL), encoding="utf-8")

    # ----- the Tripo reference -----
    def _bake_reference(self, workspace: Path, spec: dict) -> None:
        """engine="neural": an EXPLICIT "show me what a real X looks like" — a hosted text-to-mesh likeness
        in a small GLB viewer. It is a reference to look at, never the design, and the page says so."""
        prompt = str(spec.get("prompt") or spec.get("title") or "").strip()
        if not prompt:
            self._write_error(workspace, "A reference hologram needs a 'prompt' describing the subject.")
            return
        if self._neural is None or not self._neural_available():
            self._write_error(
                workspace,
                "Reference holograms need Tripo — ask HELIX to connect Tripo and a secure key panel "
                "opens. (To DESIGN the object instead, describe it and HELIX will model it.)")
            return
        try:
            image = spec.get("image")
            image_path = (workspace / image) if isinstance(image, str) and image else None
            glb = self._neural(prompt, image_path)
        except Exception as exc:  # noqa: BLE001 — surface the service's message, never crash
            self._write_error(workspace, f"Couldn't fetch the reference: {exc}")
            return
        if not glb:
            self._write_error(workspace, "The reference service returned nothing.")
            return
        try:
            (workspace / "assets").mkdir(parents=True, exist_ok=True)
            (workspace / GLB_REL).write_bytes(glb)
        except OSError as exc:
            self._write_error(workspace, f"Couldn't save the reference: {exc}")
            return
        title = self._title(workspace, spec)
        (workspace / VIEWER_FILE).write_text(
            _REFERENCE_HTML.replace("__TITLE__", _esc(title)).replace("__ACCENT__", DEFAULT_ACCENT)
            .replace("__BG__", DEFAULT_BG).replace("__GLB__", GLB_REL), encoding="utf-8")

    # ----- pages -----
    @staticmethod
    def _read_spec(ws: Path) -> dict | None:
        try:
            spec = json.loads((ws / SPEC_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return spec if isinstance(spec, dict) else None

    @staticmethod
    def _is_legacy_spec(spec: dict) -> bool:
        """A model.json from the retired primitive engine: a 'parts' list, or an engine name that only
        that engine knew ('parametric' / 'auto')."""
        if isinstance(spec.get("parts"), list):
            return True
        return str(spec.get("engine", "")).lower() in {"parametric", "auto"}

    def _write_legacy_page(self, ws: Path, spec: dict) -> None:
        title = self._title(ws, spec)
        self._write_notice(
            ws, title=title, heading="This hologram was made with HELIX's older engine",
            message="Say “redesign it” and HELIX will rebuild it as a parametric design you can change by "
                    "voice — “make it wider”, “add a gusset”, “two more holes”.",
            brief={"title": title, "summary": str(spec.get("prompt") or ""), "parts": []},
            source=None, note="",
        )

    def _write_notice(
        self, ws: Path, *, title: str, heading: str, message: str, brief: dict,
        source: str | None, note: str = "",
    ) -> None:
        """A friendly page in the viewer's chrome with NO dead controls: used when the engine is missing,
        when a compile fails at bake time, and for a retired-engine workspace. The source, when there is
        one, is shown in a collapsed panel — it IS the design, and seeing it is part of trusting it."""
        summary = str(brief.get("summary") or "")
        parts = [str(p) for p in (brief.get("parts") or [])]
        source_block = ""
        if source:
            source_block = (
                '<details class="src"><summary>Source — model.py</summary>'
                f'<pre>{_esc(source)}</pre>'
                f'<p class="links"><a href="{SOURCE_FILE}" download>Download model.py</a></p></details>'
            )
        parts_block = ""
        if parts:
            parts_block = "<p class=\"parts\"><span>Parts:</span> " + _esc(", ".join(parts)) + "</p>"
        (ws / VIEWER_FILE).write_text(
            _NOTICE_HTML
            .replace("__TITLE__", _esc(title))
            .replace("__HEADING__", _esc(heading))
            .replace("__MESSAGE__", _esc(message))
            .replace("__NOTE__", _esc(note))
            .replace("__SUMMARY__", _esc(summary) if summary and summary != title else "")
            .replace("__PARTS__", parts_block)
            .replace("__SOURCE__", source_block),
            encoding="utf-8",
        )

    @staticmethod
    def _is_generated_viewer(viewer: Path) -> bool:
        """True if index.html is HELIX's OWN baked viewer (vs a hand-authored animated page). Matches the
        sentinel OR a reference to a baked mesh, so viewers from before the sentinel still count — that
        avoids ever mistaking a real (older) generated viewer for hand-authored and deleting its spec."""
        try:
            html = viewer.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # unreadable → don't risk dropping the spec; just re-bake
        return VIEWER_SENTINEL in html or GLB_REL in html or STL_JS_REL in html

    def _write_render_kit(self, workspace: Path) -> None:
        """Ship the shared render kit next to a hand-authored animated index.html (which imports it)."""
        try:
            (workspace / render_kit.KIT_FILE).write_text(render_kit.HELIX3D_JS, encoding="utf-8")
        except OSError:
            pass

    def _write_error(self, workspace: Path, message: str) -> None:
        try:  # drop a stale reference mesh so the error page isn't sitting next to a now-invalid old GLB
            (workspace / GLB_REL).unlink()
        except OSError:
            pass
        (workspace / VIEWER_FILE).write_text(
            _ERROR_HTML.replace("__BG__", DEFAULT_BG).replace("__ACCENT__", DEFAULT_ACCENT)
            .replace("__MSG__", _esc(message)), encoding="utf-8")

    @staticmethod
    def _title(workspace: Path, spec: dict, brief: dict | None = None) -> str:
        """The design's name: the brief's title → model.json's title → the build's name → 'Hologram'."""
        if brief:
            t = brief.get("title")
            if isinstance(t, str) and t.strip():
                return t.strip()
        t = spec.get("title") if isinstance(spec, dict) else None
        if isinstance(t, str) and t.strip():
            return t.strip()
        try:
            man = json.loads((workspace / MANIFEST).read_text(encoding="utf-8"))
            if isinstance(man.get("name"), str) and man["name"].strip():
                return man["name"]
        except Exception:  # noqa: BLE001
            pass
        return "Hologram"


# ----- small helpers -----
def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _sha(source: str) -> str:
    # The library is part of the key: an upgraded helix.scad must recompile even unchanged source.
    return hashlib.sha256((source + "\n" + scad.HELIX_LIB).encode("utf-8")).hexdigest()


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _hex_str(value, default: str) -> str:
    if isinstance(value, str):
        v = value.strip()
        if len(v) == 7 and v.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
            return v
    return default


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _json_for_script(data: dict) -> str:
    """JSON safe to inline inside a <script> block: a '</' in the design's source or a comment in a
    parameter description must not be able to close the script element early."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


_CDN_NOTE = (
    "<!-- No vendored three.js was handed to the baker, so this page loads it from a CDN; "
    "a HELIX build ships assets/three.min.js beside the page and needs no network. -->"
)

# A single fixed viewer template, written verbatim by HELIX (never authored by the model). three.js r128
# UMD (THREE.* globals — outputEncoding era; no addons, because addons are ES modules and would drag a
# CDN back in). OpenSCAD is Z-up, so the WORLD is Z-up here (camera.up = +Z, the grid lies in XY): model
# coordinates are world coordinates, and the section plane, the axes and the dimensions read straight
# off the STL with no sign gymnastics. The data the page needs is inlined as window.HELIX_MODEL; the
# mesh arrives as window.HELIX_STL (base64) from a sidecar <script src>, because fetch() of a local file
# is refused over file://.
_VIEWER_HTML = """<!doctype html>
<!-- HELIX-GENERATED-VIEWER -->
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root { --accent: #3fe0e0; --bg: #10161c; --ink: #d5dde3; --dim: #8a98a4; --line: rgba(213,221,227,.14); }
  html, body { margin: 0; height: 100%; background: var(--bg); overflow: hidden; font-size: 13px;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; color: var(--ink); }
  #app { position: fixed; inset: 0; cursor: grab; touch-action: none; }
  #app:active { cursor: grabbing; }
  #vignette { position: fixed; inset: 0; pointer-events: none;
    background: radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.45) 100%); }
  .hud { position: fixed; background: rgba(16,22,28,.8); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 12px; backdrop-filter: blur(6px); z-index: 2; }
  #info { top: 14px; left: 14px; max-width: 360px; }
  #info h1 { margin: 0 0 2px; font-size: 14px; font-weight: 600; letter-spacing: .04em; color: var(--accent); }
  #info .sum { color: var(--dim); margin: 0 0 6px; line-height: 1.35; }
  #info .stats { font-variant-numeric: tabular-nums; line-height: 1.55; }
  #info .stats span { color: var(--dim); margin-right: 4px; }
  #info .ver { color: var(--dim); font-size: 11px; margin-top: 4px; }
  #tools { top: 14px; right: 14px; width: 262px; max-height: calc(100% - 28px); overflow: auto; }
  #tools h2 { margin: 8px 0 5px; font-size: 11px; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: var(--dim); }
  #tools h2:first-child { margin-top: 0; }
  .row { display: flex; flex-wrap: wrap; gap: 5px; }
  button, .seg b { background: transparent; color: var(--ink); border: 1px solid var(--line);
    border-radius: 7px; padding: 5px 9px; font-size: 12px; cursor: pointer; font-family: inherit; }
  button:hover, .seg b:hover { border-color: var(--accent); color: var(--accent); }
  button.on, .seg b.on { color: var(--accent); border-color: var(--accent); }
  .seg { display: inline-flex; gap: 4px; }
  #section { width: 100%; margin: 6px 0 2px; accent-color: var(--accent); }
  #sectionLabel { color: var(--dim); font-variant-numeric: tabular-nums; }
  #params { margin: 0; padding: 0; list-style: none; }
  #params li { padding: 5px 0; border-top: 1px solid var(--line); }
  #params li:first-child { border-top: none; }
  #params .n { font-weight: 600; }
  #params .v { color: var(--accent); font-variant-numeric: tabular-nums; margin-left: 6px; }
  #params .r, #params .d { color: var(--dim); font-size: 12px; display: block; }
  #params .say { color: var(--dim); font-size: 11px; font-style: italic; }
  #exports a { color: var(--accent); text-decoration: none; border: 1px solid var(--line); border-radius: 7px;
    padding: 4px 8px; font-size: 12px; }
  #exports a:hover { border-color: var(--accent); }
  details.src summary { cursor: pointer; color: var(--dim); font-size: 12px; }
  details.src pre { margin: 6px 0 0; max-height: 260px; overflow: auto; font-size: 11px; line-height: 1.4;
    color: var(--ink); background: rgba(0,0,0,.25); border-radius: 7px; padding: 8px; white-space: pre; }
  #hint { position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%); color: var(--dim);
    font-size: 11px; pointer-events: none; z-index: 2; text-shadow: 0 1px 6px rgba(0,0,0,.7); }
  #msg { position: fixed; inset: 0; display: none; align-items: center; justify-content: center; z-index: 3;
    text-align: center; padding: 24px; font-size: 15px; color: var(--dim); pointer-events: none; }
  @media (max-width: 720px) { #tools { display: none; } #info { max-width: 60%; } }
</style>
</head>
<body>
  <div id="app"></div>
  <div id="vignette"></div>
  <div id="info" class="hud">
    <h1 id="title">__TITLE__</h1>
    <p class="sum" id="summary"></p>
    <div class="stats">
      <div><span>Size</span><b id="dims">—</b></div>
      <div><span>Faces</span><b id="faces">—</b></div>
      <div><span>Grid</span><b id="gridLabel">—</b></div>
    </div>
    <div class="ver" id="engine"></div>
  </div>
  <div id="tools" class="hud">
    <h2>View</h2>
    <div class="row">
      <button id="wire">Wireframe</button>
      <button id="shade">Flat-lit</button>
      <button id="reset">Reset view</button>
    </div>
    <h2>Section plane</h2>
    <div class="seg" id="axisPick"><b data-axis="">Off</b><b data-axis="x">X</b><b data-axis="y">Y</b><b data-axis="z">Z</b></div>
    <input id="section" type="range" min="0" max="1000" value="500" disabled />
    <div id="sectionLabel">No section</div>
    <h2>Parameters</h2>
    <ul id="params"></ul>
    <h2>Export</h2>
    <div class="row" id="exports"></div>
    <h2>Source</h2>
    <details class="src"><summary>model.py</summary><pre id="source"></pre></details>
  </div>
  <div id="hint">drag to orbit · right-drag or shift-drag to pan · wheel to zoom</div>
  <div id="msg"></div>
  __THREE_NOTE__
  <script src="__THREE_SRC__"></script>
  <script src="__STL_JS__"></script>
  <script>
  window.HELIX_MODEL = __DATA__;
  </script>
  <script>
  (function () {
    "use strict";
    var D = window.HELIX_MODEL || {};
    var $ = function (id) { return document.getElementById(id); };
    var fail = function (m) { var e = $("msg"); e.textContent = m; e.style.display = "flex"; };
    var fmt = function (n) { return (Math.round(n * 10) / 10).toString(); };

    // ---- The chrome that needs no WebGL: brief, parameters, exports, source. Filled first so a page
    // whose 3D view cannot start (no WebGL, a missing sidecar) still shows the whole design. ----
    $("summary").textContent = D.summary || "";
    $("engine").textContent = D.engine ? "build123d " + D.engine : "";
    var ul = $("params");
    (D.params || []).forEach(function (p) {
      var li = document.createElement("li");
      var n = document.createElement("span"); n.className = "n"; n.textContent = p.name;
      var v = document.createElement("span"); v.className = "v"; v.textContent = p.value;
      li.appendChild(n); li.appendChild(v);
      var range = "";
      if (p.minimum !== null && p.minimum !== undefined && p.maximum !== null && p.maximum !== undefined)
        range = p.minimum + " – " + p.maximum + (p.step ? " by " + p.step : "");
      else if (p.choices && p.choices.length) range = p.choices.join(" · ");
      if (range) { var r = document.createElement("span"); r.className = "r"; r.textContent = range; li.appendChild(r); }
      if (p.description) { var d = document.createElement("span"); d.className = "d"; d.textContent = p.description; li.appendChild(d); }
      var say = document.createElement("span"); say.className = "say";
      say.textContent = "say: make " + p.name + " " + (p.kind === "bool" ? (p.value === "true" ? "false" : "true") : "<value>");
      li.appendChild(say);
      ul.appendChild(li);
    });
    if (!(D.params || []).length) { var none = document.createElement("li"); none.className = "d";
      none.textContent = "No adjustable parameters — say what to change and HELIX edits the design."; ul.appendChild(none); }
    var ex = $("exports"); var F = D.files || {};
    [["STL", F.stl], ["STEP", F.step], ["3MF", F.mf], ["PY", F.scad], ["Preview", F.preview]].forEach(function (pair) {
      if (!pair[1]) return;
      var a = document.createElement("a"); a.href = pair[1]; a.setAttribute("download", ""); a.textContent = pair[0]; ex.appendChild(a);
    });
    $("source").textContent = D.source || "";

    // ---- STL parsing. OpenSCAD 2021.01 writes ASCII STL by default (a 2 MB file is normal), so the
    // ASCII path is one regex over the vertex lines, never a per-character walk. Binary is recognised by
    // its exact length (84 + 50 * triangles) — the only reliable tell, since some writers start a binary
    // file with the word "solid" too. ----
    function bytesFromBase64(b64) {
      var bin = atob(b64), n = bin.length, out = new Uint8Array(n);
      for (var i = 0; i < n; i++) out[i] = bin.charCodeAt(i);
      return out;
    }
    function parseBinary(bytes) {
      var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      var n = dv.getUint32(80, true), pos = new Float32Array(n * 9), o = 84, k = 0;
      for (var i = 0; i < n; i++) {
        o += 12;                                   // the stored facet normal is ignored; faces are recomputed
        for (var v = 0; v < 9; v++) { pos[k++] = dv.getFloat32(o, true); o += 4; }
        o += 2;                                    // attribute byte count
      }
      return pos;
    }
    var VERTEX_RE = /vertex\\s+([-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?)\\s+([-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?)\\s+([-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?)/g;
    function parseAscii(text) {
      var arr = [], m;
      VERTEX_RE.lastIndex = 0;
      while ((m = VERTEX_RE.exec(text)) !== null) arr.push(+m[1], +m[2], +m[3]);
      arr.length -= arr.length % 9;                // a truncated trailing facet is dropped, not drawn
      return new Float32Array(arr);
    }
    function parseSTL(bytes) {
      if (bytes.byteLength >= 84) {
        var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        var n = dv.getUint32(80, true);
        if (84 + n * 50 === bytes.byteLength) return parseBinary(bytes);
      }
      return parseAscii(new TextDecoder("utf-8").decode(bytes));
    }

    // ---- A procedural matcap: a soft key from the top-left on neutral grey with a faint rim, drawn on a
    // canvas so the page needs no image asset. Lit-looking without a single light or an environment. ----
    function makeMatcap() {
      var c = document.createElement("canvas"); c.width = c.height = 256;
      var g = c.getContext("2d");
      var key = g.createRadialGradient(92, 84, 6, 128, 128, 156);
      key.addColorStop(0, "#f4f6f8"); key.addColorStop(0.3, "#bcc4cb");
      key.addColorStop(0.72, "#6f7983"); key.addColorStop(1, "#343b42");
      g.fillStyle = key; g.fillRect(0, 0, 256, 256);
      var rim = g.createRadialGradient(128, 128, 96, 128, 128, 128);
      rim.addColorStop(0, "rgba(255,255,255,0)"); rim.addColorStop(1, "rgba(205,222,232,0.38)");
      g.fillStyle = rim; g.fillRect(0, 0, 256, 256);
      return new THREE.CanvasTexture(c);
    }

    if (typeof THREE === "undefined") { fail("Couldn't start the 3D view — the viewer library didn't load."); return; }
    if (!window.HELIX_STL) { fail("Couldn't load the hologram mesh."); return; }

    try {
      var positions = parseSTL(bytesFromBase64(window.HELIX_STL));
      if (!positions.length) { fail("The compiled model is empty — nothing to show yet."); return; }
      var app = $("app");
      var renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(window.innerWidth, window.innerHeight);
      renderer.localClippingEnabled = true;          // the section plane
      app.appendChild(renderer.domElement);

      var scene = new THREE.Scene();
      scene.background = new THREE.Color(0x10161c);
      var camera = new THREE.PerspectiveCamera(40, window.innerWidth / window.innerHeight, 0.1, 10000);
      camera.up.set(0, 0, 1);                          // OpenSCAD is Z-up; so is this world

      // Geometry straight from the STL: unshared vertices → computeVertexNormals gives per-FACE normals,
      // the faceted look that reads as CAD. Crease edges above 30° are drawn as lines over it.
      var geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geom.computeVertexNormals();
      geom.computeBoundingBox();
      var bb = geom.boundingBox;
      var size = bb.getSize(new THREE.Vector3()), center = bb.getCenter(new THREE.Vector3());
      var radius = Math.max(size.length() / 2, 0.5);

      var matcapMat = new THREE.MeshMatcapMaterial({ matcap: makeMatcap(), color: 0xffffff,
        side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      var flatMat = new THREE.MeshLambertMaterial({ color: 0xb9c1c9, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      var edgeMat = new THREE.LineBasicMaterial({ color: 0xd2dbe2, transparent: true, opacity: 0.6 });
      var mats = [matcapMat, flatMat, edgeMat];
      var mesh = new THREE.Mesh(geom, matcapMat);
      var edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 30), edgeMat);
      scene.add(mesh); scene.add(edges);
      // Lights only matter to the flat-lit variant: a soft sky/ground pair and one gentle key. Low
      // intensities, no tone-mapping exposure — a drawing, not a showroom.
      scene.add(new THREE.HemisphereLight(0xe2e8ee, 0x2a3139, 0.85));
      var sun = new THREE.DirectionalLight(0xffffff, 0.55); sun.position.set(1, -1.2, 2.2); scene.add(sun);

      // Grid in millimetres, sized from the model: 10 mm minor / 100 mm major for anything up to 500 mm,
      // scaled by a decade either way beyond that, and the spacing is written in the HUD so the picture
      // can be read like a drawing. Lines snap to multiples of the major spacing so the origin sits on one.
      var extent = Math.max(size.x, size.y, size.z, 1);
      var minor = 10;
      while (extent > minor * 50) minor *= 10;
      while (extent < minor * 2 && minor > 0.1) minor /= 10;
      var major = minor * 10;
      var span = Math.max(2, Math.ceil((extent * 1.8) / major)) * major;
      var gcx = Math.round(center.x / major) * major, gcy = Math.round(center.y / major) * major;
      var gridMinor = new THREE.GridHelper(span, Math.round(span / minor), 0x2a3540, 0x1e2831);
      var gridMajor = new THREE.GridHelper(span, Math.round(span / major), 0x3b4855, 0x3b4855);
      [gridMinor, gridMajor].forEach(function (gr) {
        gr.rotation.x = Math.PI / 2;                 // GridHelper is XZ; lay it in the XY plane
        gr.position.set(gcx, gcy, bb.min.z);
        gr.material.transparent = true; gr.material.opacity = 0.9;
        scene.add(gr);
      });
      var axes = new THREE.AxesHelper(major);
      axes.position.set(0, 0, Math.min(0, bb.min.z));
      scene.add(axes);
      $("gridLabel").textContent = minor + " mm · " + major + " mm";
      $("dims").textContent = fmt(size.x) + " × " + fmt(size.y) + " × " + fmt(size.z) + " mm  (W × D × H)";
      $("faces").textContent = (positions.length / 9).toLocaleString() + " triangles";

      // ---- Orbit / pan / zoom, by hand (OrbitControls is an addon = an ES module = a CDN). Spherical
      // coordinates about a target with Z up; pan slides the target in the camera plane. ----
      var fovRad = camera.fov * Math.PI / 180;
      var view = { target: new THREE.Vector3(), az: 0, el: 0, r: 1 };
      function home() {
        view.target.copy(center);
        view.az = -Math.PI / 2 + 0.55;               // from the front-right, a little above — OpenSCAD's habit
        view.el = 0.5;
        view.r = (radius / Math.sin(fovRad / 2)) * 1.15;
        applyCamera();
      }
      function applyCamera() {
        var ce = Math.cos(view.el);
        camera.position.set(
          view.target.x + view.r * ce * Math.cos(view.az),
          view.target.y + view.r * ce * Math.sin(view.az),
          view.target.z + view.r * Math.sin(view.el));
        camera.lookAt(view.target);
        camera.near = Math.max(view.r / 500, 0.01);
        camera.far = view.r * 20 + radius * 20;
        camera.updateProjectionMatrix();
        render();
      }
      function rotate(dx, dy) {
        view.az -= dx * 0.0065;
        view.el = Math.max(-1.52, Math.min(1.52, view.el + dy * 0.0065));
        applyCamera();
      }
      function pan(dx, dy) {
        var h = renderer.domElement.clientHeight || 1;
        var perPixel = (2 * view.r * Math.tan(fovRad / 2)) / h;
        var right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
        var upv = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
        view.target.addScaledVector(right, -dx * perPixel).addScaledVector(upv, dy * perPixel);
        applyCamera();
      }
      function zoom(f) {
        view.r = Math.max(radius * 0.05, Math.min(radius * 60, view.r * f));
        applyCamera();
      }
      var el = renderer.domElement, pointers = new Map(), pinch = 0;
      el.addEventListener("contextmenu", function (e) { e.preventDefault(); });
      el.addEventListener("pointerdown", function (e) {
        try { el.setPointerCapture(e.pointerId); } catch (_) { /* a synthetic or already-released pointer */ }
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, button: e.button, shift: e.shiftKey });
        if (pointers.size === 2) { var p = Array.from(pointers.values()); pinch = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y); }
      });
      el.addEventListener("pointermove", function (e) {
        var p = pointers.get(e.pointerId); if (!p) return;
        var dx = e.clientX - p.x, dy = e.clientY - p.y; p.x = e.clientX; p.y = e.clientY;
        if (pointers.size === 2) {                   // touch pinch: zoom + two-finger pan
          var q = Array.from(pointers.values()), d = Math.hypot(q[0].x - q[1].x, q[0].y - q[1].y);
          if (pinch > 0 && d > 0) zoom(pinch / d);
          pinch = d; pan(dx / 2, dy / 2); return;
        }
        if (p.button === 2 || p.button === 1 || p.shift) pan(dx, dy); else rotate(dx, dy);
      });
      var release = function (e) { pointers.delete(e.pointerId); pinch = 0; };
      el.addEventListener("pointerup", release); el.addEventListener("pointercancel", release);
      el.addEventListener("wheel", function (e) { e.preventDefault(); zoom(Math.exp(e.deltaY * 0.0012)); }, { passive: false });

      // ---- Section plane: one THREE.Plane per axis, slid across the model's extent; the mesh is
      // double-sided so the cut shows the inside. Edges clip with it. ----
      var planes = { x: new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0),
                     y: new THREE.Plane(new THREE.Vector3(0, -1, 0), 0),
                     z: new THREE.Plane(new THREE.Vector3(0, 0, -1), 0) };
      var sectionAxis = "";
      var slider = $("section");
      function applySection() {
        var label = $("sectionLabel");
        if (!sectionAxis) {
          mats.forEach(function (m) { m.clippingPlanes = []; m.needsUpdate = true; });
          slider.disabled = true; label.textContent = "No section";
        } else {
          var lo = bb.min[sectionAxis], hi = bb.max[sectionAxis];
          var c = lo + (hi - lo) * (slider.value / 1000);
          var pl = planes[sectionAxis]; pl.constant = c;  // keeps axis ≤ c (distance = c − coord ≥ 0)
          mats.forEach(function (m) { m.clippingPlanes = [pl]; m.needsUpdate = true; });
          slider.disabled = false;
          label.textContent = "Cut at " + sectionAxis.toUpperCase() + " = " + fmt(c) + " mm";
        }
        render();
      }
      Array.prototype.forEach.call($("axisPick").querySelectorAll("b"), function (b) {
        b.classList.toggle("on", b.getAttribute("data-axis") === sectionAxis);
        b.addEventListener("click", function () {
          sectionAxis = b.getAttribute("data-axis");
          Array.prototype.forEach.call($("axisPick").querySelectorAll("b"), function (o) {
            o.classList.toggle("on", o === b); });
          applySection();
        });
      });
      slider.addEventListener("input", applySection);

      // ---- View toggles ----
      var wire = false, flat = false;
      $("wire").addEventListener("click", function () {
        wire = !wire; $("wire").classList.toggle("on", wire);
        matcapMat.wireframe = flatMat.wireframe = wire; render();
      });
      $("shade").addEventListener("click", function () {
        flat = !flat; $("shade").classList.toggle("on", flat);
        mesh.material = flat ? flatMat : matcapMat; render();
      });
      $("reset").addEventListener("click", home);
      // The canvas follows the window — checked on EVERY render, not only on a resize event, because a
      // page that loads while its window is still 0×0 (a hidden pane, a QWebEngineView before layout)
      // may never be told about the size it grew into, and would stay a 0×0 canvas with a NaN aspect.
      var fitted = { w: 0, h: 0 };
      function fit() {
        var w = window.innerWidth, h = window.innerHeight;
        if (!w || !h || (w === fitted.w && h === fitted.h)) return;
        fitted.w = w; fitted.h = h;
        camera.aspect = w / h; camera.updateProjectionMatrix();
        renderer.setSize(w, h);
      }
      window.addEventListener("resize", render);

      // Render on demand — a still drawing must not burn the GPU at 60 fps beside the orb.
      var queued = false;
      function render() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; fit(); renderer.render(scene, camera); });
      }
      home();
    } catch (err) {
      fail("Couldn't start the 3D view: " + (err && err.message ? err.message : err));
    }
  })();
  </script>
</body>
</html>
"""

# The friendly page in the viewer's chrome with no dead controls: the engine isn't installed, a compile
# failed at bake time, or the workspace is from the retired engine. The design (when there is one) is
# shown in full — it is finished and saved even when it cannot be compiled yet.
_NOTICE_HTML = """<!doctype html>
<!-- HELIX-GENERATED-VIEWER -->
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root { --accent: #3fe0e0; --bg: #10161c; --ink: #d5dde3; --dim: #8a98a4; --line: rgba(213,221,227,.14); }
  html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--ink); font-size: 14px;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; }
  body { display: flex; align-items: flex-start; justify-content: center; padding: 48px 16px; box-sizing: border-box; }
  .card { max-width: 640px; width: 100%; background: rgba(16,22,28,.8); border: 1px solid var(--line);
    border-radius: 12px; padding: 22px 24px; }
  .name { color: var(--accent); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 6px; }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 10px; }
  p { line-height: 1.5; margin: 0 0 10px; }
  .note, .sum, .parts { color: var(--dim); }
  .parts span { color: var(--ink); }
  details.src { margin-top: 14px; }
  details.src summary { cursor: pointer; color: var(--dim); font-size: 12px; }
  details.src pre { margin: 8px 0 0; max-height: 360px; overflow: auto; font-size: 11px; line-height: 1.4;
    background: rgba(0,0,0,.25); border-radius: 7px; padding: 8px; white-space: pre; }
  a { color: var(--accent); }
</style>
</head>
<body>
  <div class="card">
    <p class="name">__TITLE__</p>
    <h1>__HEADING__</h1>
    <p>__MESSAGE__</p>
    <p class="sum">__SUMMARY__</p>
    __PARTS__
    <p class="note">__NOTE__</p>
    __SOURCE__
  </div>
</body>
</html>
"""

# A fixed skybox viewer for an ENVIRONMENT: the equirectangular panorama is mapped onto the inside of a
# sphere and the camera sits at the centre, so dragging looks AROUND the scene (a place, not an object).
_SKYBOX_HTML = """<!doctype html>
<!-- HELIX-GENERATED-VIEWER -->
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root { --accent: __ACCENT__; }
  html, body { margin: 0; height: 100%; background: #05080b; overflow: hidden;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; color: #cfeff0; }
  #app { position: fixed; inset: 0; cursor: grab; }
  #app:active { cursor: grabbing; }
  #title { position: fixed; top: 14px; left: 16px; font-size: 14px; font-weight: 600;
    letter-spacing: .04em; color: var(--accent); opacity: .9; pointer-events: none;
    text-shadow: 0 1px 8px rgba(0,0,0,.7); }
  #hint { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); font-size: 12px;
    color: #9fc7c8; opacity: .7; pointer-events: none; text-shadow: 0 1px 6px rgba(0,0,0,.7); }
  #panel { position: fixed; bottom: 14px; right: 14px; display: flex; gap: 6px;
    background: rgba(8,11,15,.5); border: 1px solid rgba(63,224,224,.25); border-radius: 10px;
    padding: 6px; backdrop-filter: blur(6px); }
  #panel button { background: transparent; color: #bfe9ea; border: 1px solid rgba(63,224,224,.25);
    border-radius: 7px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  #panel button:hover, #panel button.on { border-color: var(--accent); color: var(--accent); }
  #msg { position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
    text-align: center; padding: 24px; font-size: 15px; color: #9fc7c8; }
  #corners i { position: fixed; width: 26px; height: 26px; border: 1.5px solid rgba(63,224,224,.45);
    pointer-events: none; }
  #corners .tl { top: 16px; left: 16px; border-right: none; border-bottom: none; }
  #corners .tr { top: 16px; right: 16px; border-left: none; border-bottom: none; }
  #corners .bl { bottom: 16px; left: 16px; border-right: none; border-top: none; }
  #corners .br { bottom: 16px; right: 16px; border-left: none; border-top: none; }
</style>
</head>
<body>
  <div id="app"></div>
  <div id="corners"><i class="tl"></i><i class="tr"></i><i class="bl"></i><i class="br"></i></div>
  <div id="title">__TITLE__</div>
  <div id="hint">Drag to look around</div>
  <div id="panel"><button id="spin" class="on">Auto-pan</button></div>
  <div id="msg"></div>
  <script type="importmap">
  { "imports": {
      "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
      "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  } }
  </script>
  <script type="module">
  import * as THREE from "three";
  import { OrbitControls } from "three/addons/controls/OrbitControls.js";
  const fail = (m) => { const e = document.getElementById("msg");
    e.textContent = m; e.style.display = "flex"; };
  try {
    const app = document.getElementById("app");
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    app.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1100);
    // Camera at the centre; a target a hair away so OrbitControls rotates our VIEW (look around), while
    // pan + dolly are off so we can't leave the sphere.
    camera.position.set(0, 0, 0.1);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 0, 0);
    controls.enablePan = false;
    controls.enableZoom = false;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = -0.4;             // drag-to-look feels natural (inverted from orbit)
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.35;

    const tex = new THREE.TextureLoader().load("__PANO__", undefined, undefined,
      () => fail("Couldn't load the scene image."));
    tex.colorSpace = THREE.SRGBColorSpace;
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(500, 64, 40),
      new THREE.MeshBasicMaterial({ map: tex, side: THREE.BackSide }));
    scene.add(sphere);

    const spin = document.getElementById("spin");
    spin.onclick = () => { controls.autoRotate = !controls.autoRotate;
      spin.classList.toggle("on", controls.autoRotate); };
    // Stop auto-pan the moment the user grabs it; a click on the button re-enables it.
    renderer.domElement.addEventListener("pointerdown", () => {
      controls.autoRotate = false; spin.classList.remove("on"); });

    addEventListener("resize", () => {
      camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight); });
    (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
  } catch (err) {
    fail("Couldn't start the 3D view: " + (err && err.message ? err.message : err));
  }
  </script>
</body>
</html>
"""

# The Tripo REFERENCE viewer: a hosted likeness in a plain GLB viewer, labelled as what it is. It keeps
# the module build + GLTFLoader from the CDN because the vendored r128 UMD bundle has no glTF loader, and
# a reference already needs the network (Tripo is a hosted service). A neutral room environment so PBR
# textures read, exposure 1.0 and NOTHING else — no bloom, no AO, no shadows: the glossy rig was too bright.
_REFERENCE_HTML = """<!doctype html>
<!-- HELIX-GENERATED-VIEWER -->
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root { --accent: __ACCENT__; }
  html, body { margin: 0; height: 100%; background: __BG__; overflow: hidden;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; color: #d5dde3; }
  #app { position: fixed; inset: 0; }
  #title { position: fixed; top: 14px; left: 16px; font-size: 14px; font-weight: 600;
    letter-spacing: .04em; color: var(--accent); opacity: .9; pointer-events: none;
    text-shadow: 0 1px 8px rgba(0,0,0,.6); }
  #banner { position: fixed; top: 14px; left: 50%; transform: translateX(-50%); max-width: 70%;
    background: rgba(16,22,28,.82); border: 1px solid rgba(213,221,227,.18); color: #8a98a4;
    border-radius: 10px; padding: 7px 14px; font-size: 12px; text-align: center; pointer-events: none; }
  #panel { position: fixed; bottom: 14px; right: 14px; display: flex; gap: 6px;
    background: rgba(16,22,28,.6); border: 1px solid rgba(213,221,227,.14); border-radius: 10px;
    padding: 6px; backdrop-filter: blur(6px); }
  #panel button { background: transparent; color: #d5dde3; border: 1px solid rgba(213,221,227,.14);
    border-radius: 7px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  #panel button:hover, #panel button.on { border-color: var(--accent); color: var(--accent); }
  #msg { position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
    text-align: center; padding: 24px; font-size: 15px; color: #8a98a4; }
</style>
</head>
<body>
  <div id="app"></div>
  <div id="title">__TITLE__</div>
  <div id="banner">Reference from Tripo — a likeness to look at, not the design.</div>
  <div id="panel"><button id="spin">Auto-rotate</button><button id="reset">Reset view</button></div>
  <div id="msg"></div>
  <script type="importmap">
  { "imports": {
      "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
      "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  } }
  </script>
  <script type="module">
  import * as THREE from "three";
  import { OrbitControls } from "three/addons/controls/OrbitControls.js";
  import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
  import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
  const fail = (m) => { const e = document.getElementById("msg");
    e.textContent = m; e.style.display = "flex"; };
  try {
    const app = document.getElementById("app");
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;
    app.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("__BG__");
    const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 5000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.08;
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.add(new THREE.HemisphereLight(0xe2e8ee, 0x2a3139, 0.4));
    const home = { pos: new THREE.Vector3(), target: new THREE.Vector3() };
    let model = null;
    const frame = () => {
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3()), c = box.getCenter(new THREE.Vector3());
      const radius = Math.max(size.x, size.y, size.z, 1e-3) * 0.5;
      const dist = radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 1.25;
      camera.near = radius / 100; camera.far = radius * 100; camera.updateProjectionMatrix();
      camera.position.set(c.x + dist * 0.7, c.y + dist * 0.45, c.z + dist);
      controls.target.copy(c); controls.update();
      home.pos.copy(camera.position); home.target.copy(c);
      const grid = new THREE.GridHelper(radius * 6, 24, 0x3b4855, 0x1e2831);
      grid.position.y = box.min.y; grid.material.opacity = 0.5; grid.material.transparent = true;
      scene.add(grid);
    };
    new GLTFLoader().load("__GLB__", (gltf) => { model = gltf.scene; scene.add(model); frame(); },
      undefined, () => fail("Couldn't load the reference mesh."));
    addEventListener("resize", () => {
      camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight); });
    const spinBtn = document.getElementById("spin");
    spinBtn.onclick = () => { controls.autoRotate = !controls.autoRotate;
      controls.autoRotateSpeed = 1.4; spinBtn.classList.toggle("on", controls.autoRotate); };
    document.getElementById("reset").onclick = () => {
      camera.position.copy(home.pos); controls.target.copy(home.target); controls.update(); };
    (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
  } catch (err) {
    fail("Couldn't start the 3D view: " + (err && err.message ? err.message : err));
  }
  </script>
</body>
</html>
"""

_ERROR_HTML = """<!doctype html>
<!-- HELIX-GENERATED-VIEWER -->
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Hologram</title>
<style>
  html, body { margin: 0; height: 100%; background: __BG__; color: #9fc7c8;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center; text-align: center; }
  .card { max-width: 460px; padding: 28px; }
  h1 { color: __ACCENT__; font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  p { font-size: 14px; line-height: 1.5; opacity: .85; }
</style></head>
<body><div class="card"><h1>This hologram didn't build</h1><p>__MSG__</p>
<p>Try describing it again, or ask for a small change.</p></div></body></html>
"""
