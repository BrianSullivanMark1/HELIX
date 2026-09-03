"""ModelBaker tests — model.py is compiled through the CadEngine port into a mesh + a technical viewer.

The engine is a FAKE implementing the port (the real build123d kernel costs seconds per compile);
one test at the bottom runs against the real kernel when it is installed and is skipped honestly
otherwise. Nothing here needs the network: the viewer is self-contained
(vendored three.js) and is only read as text."""
from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path

import pytest

from helix.domain import cadpy as scad
from helix.ports.cad import CadResult
from helix.services import model_baker as mb
from helix.services.model_baker import (
    GLB_REL,
    MF_REL,
    PANO_REL,
    PREVIEW_REL,
    SOURCE_FILE,
    SPEC_FILE,
    STL_JS_REL,
    STL_REL,
    THREE_CDN,
    THREE_REL,
    VIEWER_FILE,
    VIEWER_SENTINEL,
    ModelBaker,
)

# A design that passes every static lint: a brief docstring, a parameter block with ranges, choices
# and descriptions, the library import, and geometry inside build().
GOOD_SOURCE = (
    '"""Design: Pipe wall bracket - a saddle bracket for 2-inch pipe with two M6 mounting holes\n'
    "Parts:\n"
    "- base plate\n"
    "- saddle\n"
    "- gusset\n"
    '"""\n'
    "from helix_parts import *\n\n"
    "# --- Parameters ---\n"
    "w = 80.0       # [40..200] width of the base plate, mm\n"
    "t = 5.0        # [3..12..0.5] plate thickness\n"
    'bolt = "M6"    # [M4, M5, M6] bolt size\n'
    "gusset = True  # stiffening gusset\n"
    "# --- End Parameters ---\n\n\n"
    "def build():\n"
    "    return Box(w, 50, t)\n"
)

# Four triangles with known coordinates — the same solid as ASCII and as binary STL.
_TRIS = [
    [(0, 0, 0), (10, 0, 0), (0, 10, 0)],
    [(0, 0, 0), (0, 10, 0), (0, 0, 10)],
    [(0, 0, 0), (0, 0, 10), (10, 0, 0)],
    [(10, 0, 0), (0, 0, 10), (0, 10, 0)],
]
ASCII_STL = ("solid OpenSCAD_Model\n" + "".join(
    "  facet normal 0 0 1\n    outer loop\n"
    + "".join(f"      vertex {x} {y} {z}\n" for x, y, z in t) + "    endloop\n  endfacet\n"
    for t in _TRIS) + "endsolid OpenSCAD_Model\n").encode()


def _binary_stl() -> bytes:
    out = b"solid-looking binary header".ljust(80, b"\0") + struct.pack("<I", len(_TRIS))
    for t in _TRIS:
        out += struct.pack("<3f", 0, 0, 1)
        for v in t:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    return out


BINARY_STL = _binary_stl()


class _FakeCad:
    """A scripted CadEngine. Records every call; can be unavailable, fail a compile with a given compiler
    message, fail the render or the 3MF export, or raise — whatever a test needs the port to do."""

    def __init__(self, *, available: bool = True, stl: bytes = ASCII_STL, compile_detail: str | None = None,
                 render_ok: bool = True, mf_ok: bool = True, version: str = "2021.01",
                 compile_seconds: float = 0.5, raise_on_compile: bool = False):
        self._available = available
        self._stl = stl
        self._compile_detail = compile_detail
        self._render_ok = render_ok
        self._mf_ok = mf_ok
        self._version = version
        self._seconds = compile_seconds
        self._raise = raise_on_compile
        self.calls: list[tuple] = []

    def available(self) -> bool:
        return self._available

    def version(self):
        return self._version if self._available else None

    def compile_stl(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        self.calls.append(("compile_stl", Path(source), Path(out)))
        if self._raise:
            raise RuntimeError("engine exploded")
        if self._compile_detail is not None:
            return CadResult(False, None, "The hologram's source has a slip the engine couldn't read past.",
                             self._compile_detail, 0.1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self._stl)
        return CadResult(True, out, None, None, self._seconds)

    def export_3mf(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        self.calls.append(("export_3mf", Path(source), Path(out)))
        if not self._mf_ok:
            return CadResult(False, None, "3MF export didn't work this time.", "no 3mf", 0.1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"PK-3MF-FAKE")
        return CadResult(True, out, None, None, 0.2)

    def render_png(self, source: Path, out: Path, *, size=(1280, 960), timeout_s: float = 120.0) -> CadResult:
        self.calls.append(("render_png", Path(source), Path(out)))
        if not self._render_ok:
            return CadResult(False, None, "The preview picture couldn't be drawn.", "render failed", 0.1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG-FAKE")
        return CadResult(True, out, None, None, 0.3)

    def install(self, on_progress=None, timeout_s: float = 900.0) -> CadResult:
        self.calls.append(("install",))
        return CadResult(False, None, "not in a test", None, 0.0)

    def install_hint(self) -> str:
        return "Holograms are computed by build123d — just say “install it” and HELIX will set it up."

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


@pytest.fixture
def three_js(tmp_path: Path) -> Path:
    # A stand-in for the vendored helix/ui/assets/three.min.js — only its bytes matter to the baker.
    p = tmp_path / "vendor" / "three.min.js"
    p.parent.mkdir()
    p.write_text("/* fake three r128 */ var THREE = {REVISION: '128'};", encoding="utf-8")
    return p


def _ws(tmp_path: Path, source: str | None = GOOD_SOURCE, name: str = "Test Model") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".helixbuild.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if source is not None:
        (ws / SOURCE_FILE).write_text(source, encoding="utf-8")
    return ws


def _html(ws: Path) -> str:
    return (ws / VIEWER_FILE).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------------
# check(): the pre-finalize gate
# ----------------------------------------------------------------------------------------------------

def test_check_writes_the_library_beside_the_source_and_compiles(tmp_path):
    cad = _FakeCad()
    ws = _ws(tmp_path)
    (ws / scad.HELIX_LIB_FILE).write_text("// stale library from an older HELIX", encoding="utf-8")
    assert ModelBaker(cad).check(ws) is None
    # the library is ALWAYS refreshed — a new model must never compile against a stale helper set
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8") == scad.HELIX_LIB
    assert cad.calls[0] == ("compile_stl", ws / SOURCE_FILE, ws / STL_REL)
    assert (ws / STL_REL).read_bytes() == ASCII_STL


class _MeteredCad(_FakeCad):
    """A FakeCad whose one-run meta carries a print analysis — the P1S review path."""

    def __init__(self, warnings, **kw):
        super().__init__(**kw)
        self._warnings = list(warnings)

    def meta_for(self, source):
        return {"bbox_mm": [100, 40, 30], "volume_cm3": 12.3, "print_warnings": self._warnings}


def test_a_measured_warning_goes_to_the_repair_pass_once(tmp_path):
    # THE SELF-REVIEW PASS: any measured print problem fails the FIRST check (the coder gets one
    # shot at fixing it against real numbers)…
    cad = _MeteredCad(["OVERHANG: ≈6.0 cm² of faces steeper than 45° downward (lowest at 4.0 mm)"])
    baker = ModelBaker(cad)
    ws = _ws(tmp_path)
    baker.prepare(ws)
    problem = baker.check(ws)
    assert problem and "P1S" in problem and "OVERHANG" in problem
    # …but the SAME warning on the repair pass's check must NOT fail the build: a benign residual
    # warning stays a studio warning, never a rollback of a design that compiles.
    assert baker.check(ws) is None


def test_floating_still_blocks_the_repair_pass_too(tmp_path):
    cad = _MeteredCad(["FLOATING: a piece of 'body' starts 5.0 mm above the plate — nothing below it."])
    baker = ModelBaker(cad)
    ws = _ws(tmp_path)
    baker.prepare(ws)
    assert "mid-air" in (baker.check(ws) or "")
    assert "mid-air" in (baker.check(ws) or "")  # always wrong — the slicer would refuse it


def test_the_critic_hears_the_measurements(tmp_path):
    seen: dict = {}

    def critic(png, brief):
        seen["brief"] = brief
        return None

    baker = ModelBaker(_MeteredCad([]), critic=critic)
    ws = _ws(tmp_path)
    baker.prepare(ws)
    assert baker.check(ws) is None
    assert "Measured off the compiled model" in seen["brief"] and "100" in seen["brief"]


def test_lint_short_circuits_before_any_compile(tmp_path):
    cad = _FakeCad()
    ws = _ws(tmp_path, GOOD_SOURCE.replace("from helix_parts import *",
                                           "import os\nfrom helix_parts import *"))
    problem = ModelBaker(cad).check(ws)
    assert problem and "imports 'os'" in problem
    assert cad.calls == []  # a compile costs seconds; a text-visible defect is reported from the text


def test_compile_failure_carries_the_warm_sentence_and_the_compiler_detail(tmp_path):
    cad = _FakeCad(compile_detail="NameError in model.py, line 13: name 'wdth' is not defined")
    problem = ModelBaker(cad).check(_ws(tmp_path))
    assert problem.startswith("The hologram's source has a slip")
    assert "The engine said: NameError in model.py, line 13" in problem


def test_critic_speaks_once_per_cycle_and_its_verdict_is_the_problem(tmp_path):
    cad = _FakeCad()
    seen: list[tuple] = []

    def critic(png: Path, brief: str):
        seen.append((png, brief))
        return "the gusset the brief asks for is missing."

    baker = ModelBaker(cad, critic=critic)
    ws = _ws(tmp_path)
    first = baker.check(ws)
    assert first == ("Looking at the rendered preview (assets/preview.png): the gusset the brief asks for "
                     "is missing. Fix the model so it matches the brief.")
    assert len(seen) == 1
    png, brief = seen[0]
    assert png == ws / PREVIEW_REL and png.is_file()
    # the critic judges against the brief + the parameter block, not the raw source
    assert "Design: Pipe wall bracket" in brief and "Parts: base plate, saddle, gusset" in brief
    assert "w = 80.0 [40..200] — width of the base plate, mm" in brief and "bolt = M6 [M4, M5, M6]" in brief
    # the Forge's repair pass re-checks: a picky critic must NOT be able to fail a design that compiles
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 90"), encoding="utf-8")
    assert baker.check(ws) is None
    assert len(seen) == 1


def test_critic_is_silent_when_the_first_check_already_failed(tmp_path):
    # Check one failed a lint, so the repair pass is the LAST pass: the critic speaking on check two would
    # roll back a design that compiles. The critic only ever speaks on the first check of a cycle.
    seen = []
    baker = ModelBaker(_FakeCad(), critic=lambda png, brief: seen.append(1) or "too small")
    ws = _ws(tmp_path, GOOD_SOURCE.replace("from helix_parts import *",
                                           "import os" + chr(10) + "from helix_parts import *"))
    assert "imports 'os'" in baker.check(ws)
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert baker.check(ws) is None
    assert seen == []


def test_bake_closes_the_cycle_so_the_next_build_gets_its_critic(tmp_path, three_js):
    seen = []
    baker = ModelBaker(_FakeCad(), three_js=three_js, critic=lambda png, brief: seen.append(1) and None)
    ws = _ws(tmp_path)
    assert baker.check(ws) is None and seen == [1]
    baker.bake(ws)
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 120"), encoding="utf-8")
    assert baker.check(ws) is None
    assert seen == [1, 1]


def test_a_rolled_back_cycle_does_not_starve_the_next_build_of_its_critic(tmp_path):
    # Both checks of a build failed (the Forge rolled it back; bake() never ran). The next build of the
    # same hologram is a NEW cycle and must get its one look — that is what _CHECKS_PER_CYCLE is for.
    seen = []
    baker = ModelBaker(_FakeCad(), critic=lambda png, brief: (seen.append(1), "wrong shape")[1])
    ws = _ws(tmp_path)
    assert "wrong shape" in baker.check(ws)           # check 1: critic
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE + "\n# v2\n", encoding="utf-8")
    assert baker.check(ws) is None                    # check 2: silent
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE + "\n# v3\n", encoding="utf-8")
    assert "wrong shape" in baker.check(ws)           # next build's check 1: critic again
    assert seen == [1, 1]


def test_a_first_check_that_found_no_source_still_counts_toward_the_cycle(tmp_path):
    # Check one found a retired model.json (or nothing at all) and said so; the coder then wrote
    # model.py in the REPAIR pass. That second check is the last pass — the critic must not speak on
    # it, or a design that compiles is rolled back. The counter used to sit after these early returns,
    # so check two was taken for check one and the critic spoke.
    seen = []
    baker = ModelBaker(_FakeCad(), critic=lambda png, brief: (seen.append(1), "wrong shape")[1])
    ws = _ws(tmp_path / "retired", None)
    (ws / SPEC_FILE).write_text(json.dumps({"parts": [{"shape": "box"}]}))
    assert "retired" in baker.check(ws)
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert baker.check(ws) is None and seen == []
    ws2 = _ws(tmp_path / "empty", None)
    assert baker.check(ws2) == "no model.py was produced"
    (ws2 / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert baker.check(ws2) is None and seen == []
    ws3 = _ws(tmp_path / "badjson", None)
    (ws3 / SPEC_FILE).write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in baker.check(ws3)
    (ws3 / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert baker.check(ws3) is None and seen == []


def test_prepare_seeds_the_library_and_opens_a_fresh_cycle(tmp_path):
    # The Forge calls prepare() before the coder runs. It must (a) put helix_parts.py in the workspace — the
    # coder's prompt names it as the only library here, and a coder that lists a fresh folder must find
    # it — and (b) reset the critic's one look, so a cycle that never reached bake() (the repair pass was
    # cancelled, died, or escaped) cannot leave the next build of the same hologram running with
    # first=False forever. Idempotent, and an already-current library is not rewritten.
    seen = []
    baker = ModelBaker(_FakeCad(), critic=lambda png, brief: (seen.append(1), "wrong shape")[1])
    ws = _ws(tmp_path, None)  # the fresh workspace: manifest only, no model.py yet
    baker.prepare(ws)
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8") == scad.HELIX_LIB
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert "wrong shape" in baker.check(ws)           # check 1 of cycle one: the critic
    # ...the repair pass dies before its check; no bake() ever closes the cycle. A new build begins:
    baker.prepare(ws)
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 90"), encoding="utf-8")
    assert "wrong shape" in baker.check(ws)           # check 1 of cycle two: the critic again
    assert seen == [1, 1]
    baker.prepare(ws)                                  # idempotent: the library is left as it is
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8") == scad.HELIX_LIB
    ModelBaker(None).prepare(tmp_path / "does-not-exist" / "ws")  # never raises, engine or not


def test_every_bake_path_closes_the_cycle(tmp_path, three_js):
    # bake() closes the cycle however it ends — not only after a successful SCAD bake. Closing it from
    # inside _bake_scad alone left the engine-missing notice, a bake-time compile failure, a bake that
    # raised, and the environment/reference/legacy pages with the count still standing, so the first
    # build after the engine was installed was taken for "check 2" and never heard the critic.
    def fresh(name, **cad_kw):
        seen = []
        cad = _FakeCad(**cad_kw)
        baker = ModelBaker(cad, three_js=three_js, critic=lambda p, b: (seen.append(1), "wrong shape")[1],
                           skybox_backend=lambda p: b"jpg", skybox_available=lambda: True)
        return cad, baker, seen, _ws(tmp_path / name)

    # the engine was missing: check passes silently, bake writes the install page; then it gets installed
    cad, baker, seen, ws = fresh("missing", available=False)
    assert baker.check(ws) is None and seen == []
    baker.bake(ws)
    assert "engine isn't installed yet" in _html(ws)
    cad._available = True
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 90"), encoding="utf-8")
    assert "wrong shape" in baker.check(ws), "the first build after the install must get the critic"

    # a compile that failed at bake time
    cad, baker, seen, ws = fresh("compilefail", compile_detail="ERROR: CGAL error")
    assert "The engine said" in baker.check(ws)
    baker.bake(ws)
    assert "didn't compile" in _html(ws)
    cad._compile_detail = None
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 90"), encoding="utf-8")
    assert "wrong shape" in baker.check(ws)

    # a bake that raised
    cad, baker, seen, ws = fresh("raised", raise_on_compile=True)
    assert baker.check(ws) is None
    baker.bake(ws)
    assert "didn't build" in _html(ws)
    cad._raise = False
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 90"), encoding="utf-8")
    assert "wrong shape" in baker.check(ws)

    # an environment workspace that was then redesigned as a model
    cad, baker, seen, ws = fresh("env")
    (ws / SOURCE_FILE).unlink()
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "environment", "prompt": "a beach"}))
    assert baker.check(ws) is None
    baker.bake(ws)
    assert PANO_REL in _html(ws)
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE, encoding="utf-8")
    assert "wrong shape" in baker.check(ws)


def test_critic_that_raises_or_says_nothing_is_not_a_problem(tmp_path):
    def boom(png, brief):
        raise RuntimeError("vision down")
    assert ModelBaker(_FakeCad(), critic=boom).check(_ws(tmp_path)) is None
    assert ModelBaker(_FakeCad(), critic=lambda p, b: None).check(_ws(tmp_path / "b")) is None


def test_preview_failure_is_not_the_coders_problem_and_the_critic_is_skipped(tmp_path):
    seen = []
    baker = ModelBaker(_FakeCad(render_ok=False), critic=lambda p, b: seen.append(1) or "x")
    assert baker.check(_ws(tmp_path)) is None
    assert seen == []  # no picture → nothing to judge


def test_a_stale_preview_of_an_older_design_is_removed_not_judged(tmp_path):
    # The engine leaves `out` untouched on a failed render, so an old picture would otherwise sit there
    # looking current — and the critic (or the page) would judge the wrong design.
    seen = []
    ws = _ws(tmp_path)
    (ws / "assets").mkdir()
    (ws / PREVIEW_REL).write_bytes(b"an old picture")
    assert ModelBaker(_FakeCad(render_ok=False), critic=lambda p, b: (seen.append(1), "x")[1]).check(ws) is None
    assert not (ws / PREVIEW_REL).exists() and seen == []


def test_engine_missing_check_passes_without_compiling(tmp_path):
    cad = _FakeCad(available=False)
    baker = ModelBaker(cad)
    assert baker.engine_missing()
    assert baker.check(_ws(tmp_path)) is None
    assert cad.count("compile_stl") == 0
    assert ModelBaker(None).engine_missing()  # no engine wired at all reads the same


def test_check_without_model_py(tmp_path):
    baker = ModelBaker(_FakeCad())
    assert baker.check(_ws(tmp_path, None)) == "no model.py was produced"
    # a hand-authored ANIMATED page is a legitimate result — the Forge's html/py gate covers it
    ws = _ws(tmp_path / "anim", None)
    (ws / VIEWER_FILE).write_text("<html><body><script>/* animated three.js */</script></body></html>")
    assert baker.check(ws) is None
    # an environment or a reference has nothing to compile
    ws = _ws(tmp_path / "env", None)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "environment", "prompt": "a beach"}))
    assert baker.check(ws) is None
    # the retired primitive format asks for a model.scad so the repair pass migrates the design
    ws = _ws(tmp_path / "old", None)
    (ws / SPEC_FILE).write_text(json.dumps({"parts": [{"shape": "box"}]}))
    problem = baker.check(ws)
    assert "retired" in problem and "model.py" in problem


def test_check_never_raises(tmp_path):
    assert ModelBaker(_FakeCad(raise_on_compile=True)).check(_ws(tmp_path)) is None


# ----------------------------------------------------------------------------------------------------
# bake(): artefacts + the viewer
# ----------------------------------------------------------------------------------------------------

def test_bake_writes_every_artefact(tmp_path, three_js):
    cad = _FakeCad()
    ws = _ws(tmp_path)
    ModelBaker(cad, three_js=three_js).bake(ws)

    assert (ws / STL_REL).read_bytes() == ASCII_STL
    assert (ws / MF_REL).is_file() and (ws / PREVIEW_REL).is_file()
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8") == scad.HELIX_LIB
    # the sidecar is the SAME bytes as base64 behind window.HELIX_STL — how the page reads it over file://
    js = (ws / STL_JS_REL).read_text(encoding="utf-8")
    m = re.fullmatch(r'window\.HELIX_STL="([A-Za-z0-9+/=]+)";\s*', js)
    assert m and base64.b64decode(m.group(1)) == ASCII_STL
    # the vendored three.js is copied beside the page and the page loads THAT, not a CDN
    assert (ws / THREE_REL).read_bytes() == three_js.read_bytes()
    html = _html(ws)
    assert VIEWER_SENTINEL in html
    assert f'<script src="{THREE_REL}"></script>' in html and f'<script src="{STL_JS_REL}"></script>' in html
    for banned in ("unpkg", "importmap", "Bloom", "RoomEnvironment", "GTAO", "toneMappingExposure"):
        assert banned not in html, banned
    # title, brief, params, engine, files and the SOURCE itself are inlined as data
    data = json.loads(re.search(r"window\.HELIX_MODEL = (\{.*?\});\n", html, re.S).group(1))
    assert data["title"] == "Pipe wall bracket"
    assert "saddle bracket for 2-inch pipe" in data["summary"]
    assert data["parts"] == ["base plate", "saddle", "gusset"]
    assert data["engine"] == "2021.01"
    assert data["source"] == GOOD_SOURCE
    assert data["files"] == {"stl": STL_REL, "mf": MF_REL, "scad": SOURCE_FILE, "step": "",
                             "preview": PREVIEW_REL}
    names = {p["name"]: p for p in data["params"]}
    assert names["w"]["minimum"] == 40 and names["w"]["maximum"] == 200
    assert names["w"]["description"] == "width of the base plate, mm"
    assert names["bolt"]["choices"] == ["M4", "M5", "M6"] and names["gusset"]["kind"] == "bool"
    assert "<title>Pipe wall bracket</title>" in html


def test_bake_reuses_the_check_compile_and_never_recompiles_unchanged_source(tmp_path, three_js):
    cad = _FakeCad()
    baker = ModelBaker(cad, three_js=three_js)
    ws = _ws(tmp_path)
    assert baker.check(ws) is None
    baker.bake(ws)
    baker.bake(ws)
    assert cad.count("compile_stl") == 1
    # a changed source DOES recompile
    (ws / SOURCE_FILE).write_text(GOOD_SOURCE.replace("w = 80", "w = 100"), encoding="utf-8")
    baker.bake(ws)
    assert cad.count("compile_stl") == 2
    # ...and so does a missing artefact, even for the same text (a rollback swept assets/ away)
    (ws / STL_REL).unlink()
    baker.bake(ws)
    assert cad.count("compile_stl") == 3


def test_a_changed_helper_library_recompiles_unchanged_source(tmp_path, three_js, monkeypatch):
    # The "never recompile the same text" key is sha256(source + library): an upgraded helix.scad changes
    # what the SAME model.scad means (a helper got a new profile), so the STL must be rebuilt — and the
    # library on disk refreshed — or the viewer keeps showing a mesh compiled against the old helpers.
    cad = _FakeCad()
    baker = ModelBaker(cad, three_js=three_js)
    ws = _ws(tmp_path)
    baker.bake(ws)
    baker.bake(ws)
    assert cad.count("compile_stl") == 1
    monkeypatch.setattr(scad, "HELIX_LIB", scad.HELIX_LIB + "\n// helix.scad v-next: a new helper\n")
    baker.bake(ws)
    assert cad.count("compile_stl") == 2
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8").endswith("a new helper\n")


def test_bake_without_a_vendored_three_falls_back_to_the_cdn_and_says_so(tmp_path):
    ws = _ws(tmp_path)
    ModelBaker(_FakeCad()).bake(ws)
    html = _html(ws)
    assert f'<script src="{THREE_CDN}"></script>' in html
    assert "No vendored three.js" in html  # the comment explaining the stray CDN reference
    assert not (ws / THREE_REL).exists()


def test_three_js_copy_is_idempotent_and_refreshed_when_it_differs(tmp_path, three_js):
    ws = _ws(tmp_path)
    (ws / "assets").mkdir()
    (ws / THREE_REL).write_text("an older bundle of a different size", encoding="utf-8")
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    assert (ws / THREE_REL).read_bytes() == three_js.read_bytes()


def test_engine_missing_bake_writes_the_install_page_with_hint_and_source(tmp_path, three_js):
    cad = _FakeCad(available=False)
    ws = _ws(tmp_path)
    ModelBaker(cad, three_js=three_js).bake(ws)
    html = _html(ws)
    assert VIEWER_SENTINEL in html
    assert "engine isn't installed yet" in html
    assert cad.install_hint() in html
    assert "return Box(w, 50, t)" in html  # the design itself is shown
    assert "Pipe wall bracket" in html
    # no dead controls: no 3D chrome, no script, no export of files that do not exist
    for dead in ("<script", 'id="wire"', 'id="section"', STL_REL, "<canvas"):
        assert dead not in html, dead
    assert cad.count("compile_stl") == 0
    assert (ws / scad.HELIX_LIB_FILE).is_file()  # the workspace is complete for the day the engine lands


def test_no_engine_wired_at_all_is_the_same_friendly_page(tmp_path):
    ws = _ws(tmp_path)
    ModelBaker(None).bake(ws)
    html = _html(ws)
    assert "engine isn't installed yet" in html and "build123d" in html and "Pipe wall bracket" in html


def test_compile_failure_at_bake_is_a_friendly_page_not_a_crash(tmp_path, three_js):
    ws = _ws(tmp_path)
    ModelBaker(_FakeCad(compile_detail="ERROR: CGAL error"), three_js=three_js).bake(ws)
    html = _html(ws)
    assert "didn't compile" in html and "slip the engine couldn't read past" in html
    assert "CGAL" not in html  # the compiler's words are for the coder, never the user
    assert "<script" not in html


def test_3mf_is_best_effort_and_a_failed_export_is_not_linked(tmp_path, three_js):
    ws = _ws(tmp_path)
    (ws / "assets").mkdir()
    (ws / MF_REL).write_bytes(b"stale 3mf of an older design")
    ModelBaker(_FakeCad(mf_ok=False), three_js=three_js).bake(ws)
    assert not (ws / MF_REL).exists()
    data = json.loads(re.search(r"window\.HELIX_MODEL = (\{.*?\});\n", _html(ws), re.S).group(1))
    assert data["files"]["mf"] == ""


def test_slow_compiles_skip_the_extra_exports(tmp_path, three_js):
    # the 3MF and the preview are each another full compile; a heavy model does not pay for them twice
    cad = _FakeCad(compile_seconds=500.0)
    ws = _ws(tmp_path)
    ModelBaker(cad, three_js=three_js).bake(ws)
    assert cad.count("export_3mf") == 0 and cad.count("render_png") == 0
    assert (ws / STL_REL).is_file() and VIEWER_SENTINEL in _html(ws)


def test_bake_never_raises(tmp_path, three_js):
    ws = _ws(tmp_path)
    ModelBaker(_FakeCad(raise_on_compile=True), three_js=three_js).bake(ws)
    assert "didn't build" in _html(ws)


def test_title_prefers_the_brief_then_model_json_then_the_build_name(tmp_path, three_js):
    ws = _ws(tmp_path, name="Build Name")
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    assert "<title>Pipe wall bracket</title>" in _html(ws)
    ws2 = _ws(tmp_path / "b", GOOD_SOURCE.replace("Design: Pipe wall bracket - ", ""), name="Build Name")
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws2)
    assert "<title>Build Name</title>" in _html(ws2)
    ws3 = _ws(tmp_path / "c", None, name="Build Name")
    (ws3 / SPEC_FILE).write_text(json.dumps({"engine": "environment", "prompt": "x", "title": "Spec Title"}))
    ModelBaker(_FakeCad(), skybox_backend=lambda p: b"jpg", skybox_available=lambda: True).bake(ws3)
    assert "<title>Spec Title</title>" in _html(ws3)


# ----------------------------------------------------------------------------------------------------
# the viewer page itself
# ----------------------------------------------------------------------------------------------------

def test_viewer_is_a_technical_illustration_with_the_expected_controls(tmp_path, three_js):
    ws = _ws(tmp_path)
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    html = _html(ws)
    for id_ in ("app", "info", "tools", "dims", "faces", "gridLabel", "engine", "wire", "shade", "reset",
                "axisPick", "section", "sectionLabel", "params", "exports", "source", "summary", "msg"):
        assert f'id="{id_}"' in html, id_
    # the look: procedural matcap, crease edges at 30°, mm grid + axes, section plane, Z-up world
    assert "MeshMatcapMaterial" in html and "CanvasTexture" in html and "createRadialGradient" in html
    assert "EdgesGeometry(geom, 30)" in html
    # the crease lines must win the depth fight along the edges they trace, or they stipple: BOTH face
    # materials carry the polygon offset, so the lines stay crisp after the flat-lit toggle swaps them
    offset = "polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1"
    matcap = re.search(r"new THREE\.MeshMatcapMaterial\(\{(.*?)\}\)", html, re.S).group(1)
    lambert = re.search(r"new THREE\.MeshLambertMaterial\(\{(.*?)\}\)", html, re.S).group(1)
    assert offset in matcap and offset in lambert
    assert "mesh.material = flat ? flatMat : matcapMat" in html  # the toggle swaps between those two
    assert "GridHelper" in html and "AxesHelper" in html and "camera.up.set(0, 0, 1)" in html
    assert "localClippingEnabled = true" in html and "clippingPlanes" in html
    assert "MeshLambertMaterial" in html and "HemisphereLight" in html  # the flat-lit toggle
    assert "#10161c" in html and "#3fe0e0" in html
    # hand-written controls — no addon import (addons are ES modules = a CDN)
    assert "OrbitControls(" not in html and "OrbitControls.js" not in html
    assert "import " not in html.split("<script")[-1]
    assert "setPointerCapture" in html and '"wheel"' in html
    assert "say: make " in html
    for href in (STL_REL, MF_REL, SOURCE_FILE):
        assert href in html


def test_viewer_parses_both_ascii_and_binary_stl(tmp_path, three_js):
    # The parser lives in JS, which this suite cannot run — so pin its two branches and the file-level
    # invariants: the ASCII regex over vertex lines, the binary length tell, and a base64 round-trip of a
    # genuine STL of each kind through the sidecar.
    for kind, stl in (("ascii", ASCII_STL), ("binary", BINARY_STL)):
        ws = _ws(tmp_path / kind)
        ModelBaker(_FakeCad(stl=stl), three_js=three_js).bake(ws)
        js = (ws / STL_JS_REL).read_text(encoding="utf-8")
        b64 = re.fullmatch(r'window\.HELIX_STL="([^"]+)";\s*', js).group(1)
        assert base64.b64decode(b64) == stl
        html = _html(ws)
        assert "function parseBinary" in html and "function parseAscii" in html and "function parseSTL" in html
        assert r"/vertex\s+" in html                      # one regex over vertex lines
        assert "84 + n * 50 === bytes.byteLength" in html   # the binary tell
        assert "dv.getUint32(80, true)" in html and 'new TextDecoder("utf-8")' in html
    # the binary sample really is what the page's tell expects
    assert 84 + 4 * 50 == len(BINARY_STL)


def test_inlined_data_cannot_close_the_script_element_early(tmp_path, three_js):
    ws = _ws(tmp_path, GOOD_SOURCE + "// a comment with </script> and <!-- inside\n")
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    html = _html(ws)
    assert "<\\/script>" in html and "<\\!--" in html
    # exactly the page's own closing tags remain: 2 sidecar loads + the data block + the viewer script
    assert html.count("</script>") == 4


# ----------------------------------------------------------------------------------------------------
# the other paths, unchanged: animated, environment, reference, retired engine
# ----------------------------------------------------------------------------------------------------

def test_animated_index_html_is_left_alone_and_gets_the_render_kit(tmp_path):
    page = "<!doctype html><html><body><script type='module'>import {createStage} from './helix3d.js';</script></body></html>"
    ws = _ws(tmp_path, None)
    (ws / VIEWER_FILE).write_text(page, encoding="utf-8")
    ModelBaker(_FakeCad()).bake(ws)
    assert (ws / VIEWER_FILE).read_text(encoding="utf-8") == page
    assert "export function createStage" in (ws / "helix3d.js").read_text(encoding="utf-8")


def test_the_sentinel_tells_our_own_viewer_from_a_hand_authored_page(tmp_path, three_js):
    # With no model.py and no model.json, index.html is either the coder's own ANIMATED page (a real
    # result: check passes, bake leaves it alone) or a leftover HELIX viewer — the sentinel — which is
    # NOT a result: check reports nothing produced, and bake writes the error page rather than showing a
    # stale design as if the build had made it. With a model.json beside it, the same tell decides
    # between re-baking our viewer and honouring a static→animated conversion (which drops the spec).
    baker = ModelBaker(_FakeCad(), three_js=three_js, neural_backend=lambda p, i: b"glb")
    hand = "<!doctype html><html><body><script>/* animated three.js */</script></body></html>"
    ws = _ws(tmp_path / "hand", None)
    (ws / VIEWER_FILE).write_text(hand, encoding="utf-8")
    assert baker.check(ws) is None
    baker.bake(ws)
    assert _html(ws) == hand
    ours = _ws(tmp_path / "ours", None)
    (ours / VIEWER_FILE).write_text(hand.replace("<html>", VIEWER_SENTINEL + "<html>"), encoding="utf-8")
    assert baker.check(ours) == "no model.py was produced"
    baker.bake(ours)
    assert "didn't build" in _html(ours) and "animated three.js" not in _html(ours)
    # a generated viewer beside a model.json is re-baked (the spec stays); a hand page beside one converts
    ref = _ws(tmp_path / "ref", None)
    (ref / SPEC_FILE).write_text(json.dumps({"engine": "neural", "prompt": "a dragon"}))
    (ref / VIEWER_FILE).write_text(VIEWER_SENTINEL + "<html>old reference</html>", encoding="utf-8")
    baker.bake(ref)
    assert (ref / SPEC_FILE).exists() and "Reference from Tripo" in _html(ref)
    (ref / VIEWER_FILE).write_text(hand, encoding="utf-8")
    baker.bake(ref)
    assert not (ref / SPEC_FILE).exists() and _html(ref) == hand


def test_static_to_animated_conversion_drops_the_stale_spec(tmp_path):
    ws = _ws(tmp_path, None)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "neural", "prompt": "a dragon"}))
    hand = "<!doctype html><html><body><script>/* animated three.js */</script></body></html>"
    (ws / VIEWER_FILE).write_text(hand, encoding="utf-8")
    ModelBaker(_FakeCad(), neural_backend=lambda p, i: b"glb").bake(ws)
    assert not (ws / SPEC_FILE).exists() and (ws / VIEWER_FILE).read_text(encoding="utf-8") == hand
    assert (ws / "helix3d.js").exists()


def test_environment_bakes_a_skybox_from_the_panorama(tmp_path):
    calls = []
    baker = ModelBaker(_FakeCad(), skybox_backend=lambda prompt: (calls.append(prompt), b"FAKE-JPEG")[1],
                       skybox_available=lambda: True)
    ws = _ws(tmp_path, None, name="Backyard")
    (ws / SPEC_FILE).write_text(json.dumps({"title": "Backyard", "engine": "environment",
                                           "prompt": "a cozy backyard at dusk with a firepit"}))
    baker.bake(ws)
    assert (ws / PANO_REL).read_bytes() == b"FAKE-JPEG"
    assert calls == ["a cozy backyard at dusk with a firepit"]
    html = _html(ws)
    assert "BackSide" in html and PANO_REL in html and "Backyard" in html and VIEWER_SENTINEL in html


def test_environment_without_a_key_or_with_a_failure_is_friendly(tmp_path):
    ws = _ws(tmp_path, None)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "environment", "prompt": "a forest clearing"}))
    ModelBaker(_FakeCad(), skybox_backend=lambda p: b"x", skybox_available=lambda: False).bake(ws)
    assert "connect Blockade" in _html(ws) and not (ws / PANO_REL).exists()

    def boom(_p):
        raise RuntimeError("service down")
    ModelBaker(_FakeCad(), skybox_backend=boom, skybox_available=lambda: True).bake(ws)
    assert "Couldn't generate the scene" in _html(ws) and "service down" in _html(ws)


def test_neural_reference_is_explicit_and_labelled_as_a_reference(tmp_path):
    sentinel = b"glTF-fake-bytes-from-backend"
    calls = []

    def backend(prompt, image):
        calls.append(prompt)
        return sentinel

    ws = _ws(tmp_path, None)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "neural", "prompt": "a real pipe bracket"}))
    ModelBaker(_FakeCad(), neural_backend=backend).bake(ws)
    assert calls == ["a real pipe bracket"]
    assert (ws / GLB_REL).read_bytes() == sentinel
    html = _html(ws)
    assert "GLTFLoader" in html and GLB_REL in html and "Reference from Tripo" in html
    for banned in ("Bloom", "GTAO", "shadowMap"):
        assert banned not in html, banned


def test_neural_reference_without_a_key_is_friendly(tmp_path):
    ws = _ws(tmp_path, None)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "neural", "prompt": "a dragon"}))
    ModelBaker(_FakeCad(), neural_backend=lambda p, i: b"x", neural_available=lambda: False).bake(ws)
    assert "connect Tripo" in _html(ws) and not (ws / GLB_REL).exists()
    ModelBaker(_FakeCad()).bake(ws)  # nothing wired at all
    assert "didn't build" in _html(ws)


def test_model_py_wins_over_a_model_json_beside_it(tmp_path, three_js):
    calls = []
    ws = _ws(tmp_path)
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "neural", "prompt": "x"}))
    ModelBaker(_FakeCad(), three_js=three_js, neural_backend=lambda p, i: calls.append(p) or b"g").bake(ws)
    assert calls == [] and (ws / STL_REL).is_file() and "MeshMatcapMaterial" in _html(ws)


def test_retired_primitive_workspace_gets_the_redesign_page(tmp_path, three_js):
    ws = _ws(tmp_path, None, name="Old Bolt")
    (ws / SPEC_FILE).write_text(json.dumps({"title": "Bolt", "parts": [{"shape": "cylinder", "radius": 1}]}))
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    html = _html(ws)
    assert "older engine" in html and "redesign it" in html and "Bolt" in html and VIEWER_SENTINEL in html
    assert "<script" not in html
    # the old auto/parametric engines too, even without parts
    (ws / SPEC_FILE).write_text(json.dumps({"engine": "auto", "prompt": "a dog"}))
    ModelBaker(_FakeCad(), three_js=three_js).bake(ws)
    assert "redesign it" in _html(ws)


def test_malformed_or_empty_spec_and_nothing_at_all_are_friendly(tmp_path):
    ws = _ws(tmp_path, None)
    (ws / SPEC_FILE).write_text("{not valid json", encoding="utf-8")
    ModelBaker(_FakeCad()).bake(ws)
    assert "didn't build" in _html(ws)
    (ws / SPEC_FILE).write_text(json.dumps({"title": "nothing"}))
    ModelBaker(_FakeCad()).bake(ws)
    assert "didn't build" in _html(ws) and "model.py" in _html(ws)
    ws2 = _ws(tmp_path / "empty", None)
    ModelBaker(_FakeCad()).bake(ws2)
    assert "produced no model.py" in _html(ws2)


def test_the_baker_no_longer_needs_the_mesh_stack():
    # The STL comes from the CAD worker now; trimesh/numpy/scipy were the primitive engine's and are gone from
    # this module (materials.py with them) — importing the baker must not drag them back in.
    import sys
    src = Path(mb.__file__).read_text(encoding="utf-8")
    for name in ("trimesh", "numpy", "scipy", "materials"):
        assert not re.search(rf"^\s*(import|from)\s+.*\b{name}\b", src, re.M), name
    assert not (Path(mb.__file__).parent / "materials.py").exists()
    assert "helix.services.materials" not in sys.modules


# ----------------------------------------------------------------------------------------------------
# the real engine, when this machine has it
# ----------------------------------------------------------------------------------------------------

def _real_engine():
    from helix.adapters.build123d_cad import Build123dCad
    return Build123dCad()


@pytest.mark.skipif(not _real_engine().available(),
                    reason="build123d is not installed in this environment")
def test_real_engine_compiles_the_good_source_end_to_end(tmp_path, three_js):
    # The one live pin: GOOD_SOURCE through the REAL kernel (worker subprocess and all) — the whole
    # artifact set lands, the viewer wraps it, and the STEP export (the Bambu-native format) exists.
    cad = _real_engine()
    baker = ModelBaker(cad, three_js=three_js)
    ws = _ws(tmp_path)
    assert baker.check(ws) is None
    baker.bake(ws)
    stl = (ws / STL_REL).read_bytes()
    assert len(stl) > 100 and (stl.startswith(b"solid") or 84 + struct.unpack("<I", stl[80:84])[0] * 50 == len(stl))
    assert (ws / mb.STEP_REL).is_file(), "the one-run artifact set must include STEP"
    html = _html(ws)
    assert VIEWER_SENTINEL in html and "MeshMatcapMaterial" in html
    assert base64.b64decode(re.search(r'window\.HELIX_STL="([^"]+)"', (ws / STL_JS_REL).read_text()).group(1)) == stl
