"""The hologram engine contract — the helix.scad library, the pure source readers, and the OpenSCAD CLI
adapter — pinned against fakes. The real binary is absent on most machines (Brian's included), so every
adapter branch runs against a scripted subprocess.Popen; the handful of tests that need a real OpenSCAD
are skipped honestly and will run the day it is installed."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from helix.adapters import openscad_cli
from helix.adapters.openscad_cli import DARK_COLORSCHEME, WINGET_ARGS, OpenScadCli
from helix.domain import scad
from helix.domain.scad import (
    HELIX_LIB,
    HELIX_LIB_DOC,
    HELIX_LIB_FILE,
    ScadParam,
    friendly_error,
    inspect_source,
    parse_brief,
    parse_params,
)
from helix.ports.cad import CadResult

# ═══════════════════════════════ the library ═══════════════════════════════


def _lib_definitions() -> set[str]:
    return set(re.findall(r"^\s*(?:module|function)\s+([A-Za-z_]\w*)\s*\(", HELIX_LIB, re.M))


def _doc_names() -> set[str]:
    names = set()
    for line in HELIX_LIB_DOC.splitlines():      # prose lines never start with `name(`
        m = re.match(r"^([A-Za-z_]\w*)\s*\(", line)
        if m:
            names.add(m.group(1))
    return names


def test_library_defines_every_documented_helper_and_documents_every_definition():
    """The cheat-sheet is what the coder sees; a helper missing from it gets reinvented, and a documented
    helper that doesn't exist becomes an 'unknown module' compile error. The two can never drift."""
    defined, documented = _lib_definitions(), _doc_names()
    assert defined, "no module/function definitions found in HELIX_LIB"
    assert defined == documented, (
        f"undocumented: {sorted(defined - documented)}; documented-but-missing: "
        f"{sorted(documented - defined)}"
    )


def test_library_has_the_helpers_the_contract_promises():
    want = {
        "rounded_box", "rounded_plate", "cyl", "tube", "slot", "countersunk_hole", "counterbore_hole",
        "hex_pocket", "chamfered_cylinder", "rect_pattern", "m_clearance", "m_tap", "helix_quality",
    }
    assert want <= _lib_definitions()


def test_library_is_balanced_and_plain():
    code = scad._strip_comments(HELIX_LIB, keep_strings=False)
    assert code.count("{") == code.count("}")
    assert code.count("(") == code.count(")")
    assert code.count("[") == code.count("]")
    assert not re.search(r"^\s*\$fn\s*=", code, re.M), "a top-level $fn would slow every compile"
    assert not re.search(r"^\s*(include|use)\s*<", HELIX_LIB, re.M), "helix.scad must stand alone"
    # No top-level variables: a `use`d file's top-level assignments are not reliably visible to its own
    # modules across OpenSCAD versions, so constants must be functions.
    assert not re.search(r"^[A-Za-z_]\w*\s*=", code, re.M), "top-level variable in helix.scad"
    assert HELIX_LIB_FILE == "helix.scad"


def test_library_helpers_each_carry_a_one_line_comment():
    lines = HELIX_LIB.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(module|function)\s+\w+\s*\(", line):
            assert lines[i - 1].lstrip().startswith("//"), f"no comment above: {line.strip()}"


def test_metric_tables_match_the_contract():
    """M2 2.4/1.6, M2.5 2.9/2.05, M3 3.4/2.5, M4 4.5/3.3, M5 5.5/4.2, M6 6.6/5.0, M8 9.0/6.8,
    M10 11.0/8.5 — clearance/tap, as the contract lists them."""
    table = r"function %s\(m\) = lookup\(m_size\(m\),\s*(\[\[.*?\]\])"
    clearance = re.search(table % "m_clearance", HELIX_LIB, re.S)
    tap = re.search(table % "m_tap", HELIX_LIB, re.S)
    assert clearance and tap
    assert clearance.group(1).replace(" ", "") == (
        "[[2,2.4],[2.5,2.9],[3,3.4],[4,4.5],[5,5.5],[6,6.6],[8,9.0],[10,11.0]]")
    assert tap.group(1).replace(" ", "") == (
        "[[2,1.6],[2.5,2.05],[3,2.5],[4,3.3],[5,4.2],[6,5.0],[8,6.8],[10,8.5]]")


def test_doc_is_compact_and_teaches_the_conventions():
    assert len(HELIX_LIB_DOC) < 3000, "the cheat-sheet rides on every hologram turn — keep it short"
    assert "use <helix.scad>" in HELIX_LIB_DOC
    assert "Z=0" in HELIX_LIB_DOC and "millimetres" in HELIX_LIB_DOC


# ═══════════════════════════════ parse_params ═══════════════════════════════

_PARAMS_SRC = """\
// Design: Wall bracket — for a 2-inch pipe
// Units: mm
use <helix.scad>;

/* [Dimensions] */
// Width of the base plate
w = 80; // [40:200]
// Plate thickness
t = 5; // [2:0.5:12]
n = 4; // [1:1:12]
style = "round"; // [round, square]
lid = true;
label = "HELIX";
mode = 1; // [1:One, 2:Two]
h = w/2;
size = [10,
        20, 30];
cap = 10; // [50]
$fa = 6;
/* [Hidden] */
secret = 3;
/* [More] */
shown = 9;

helix_quality("normal") {
    bracket();
}
leak = 1;
module bracket() { rounded_plate([w, 40, t], 4); }
"""


def test_parse_params_reads_the_customizer_block():
    by_name = {p.name: p for p in parse_params(_PARAMS_SRC)}
    assert list(by_name) == ["w", "t", "n", "style", "lid", "label", "mode", "h", "size", "cap", "shown"]
    assert by_name["w"] == ScadParam("w", "80", "number", 40.0, 200.0, None, (), "Width of the base plate")
    assert by_name["t"] == ScadParam("t", "5", "number", 2.0, 12.0, 0.5, (), "Plate thickness")
    assert by_name["n"] == ScadParam("n", "4", "number", 1.0, 12.0, 1.0, (), "")
    assert by_name["style"] == ScadParam("style", '"round"', "string", None, None, None,
                                         ("round", "square"), "")
    assert by_name["lid"].kind == "bool" and by_name["lid"].value == "true"
    assert by_name["label"].kind == "string" and by_name["label"].value == '"HELIX"'
    assert by_name["mode"].choices == ("1", "2")          # labelled choices keep the VALUE
    assert by_name["h"] == ScadParam("h", "w/2", "number")  # an expression, kept as written
    assert by_name["size"].value == "[10, 20, 30]" and by_name["size"].kind == "string"
    assert (by_name["cap"].minimum, by_name["cap"].maximum) == (0.0, 50.0)   # [N] = slider 0..N


def test_parse_params_skips_special_variables_and_hidden_groups_and_stops_at_geometry():
    names = [p.name for p in parse_params(_PARAMS_SRC)]
    assert "$fa" not in names          # "make $fa 2" is not a design decision
    assert "secret" not in names       # /* [Hidden] */ is the customizer's own hide switch
    assert "shown" in names            # a later group un-hides
    assert "leak" not in names         # after the first statement the panel is closed


@pytest.mark.parametrize("stopper", [
    'helix_quality("normal");',
    "translate([0, 0, 5]) cube(10);",
    "bracket();",
    "difference() {",
    "module bracket() {",
    "function f(x) = x * 2;",
    "for (i = [0:3]) cube(i);",
    "if (w > 10) cube(1);",
])
def test_parse_params_stops_at_the_first_non_assignment(stopper):
    src = f"w = 80;\n{stopper}\nafter = 1;\n"
    assert [p.name for p in parse_params(src)] == ["w"]


def test_parse_params_description_must_sit_directly_above():
    src = "// Not this one\n\n// Width\nw = 80;\n// orphan\n\nt = 3;\n"
    ps = parse_params(src)
    assert ps[0].description == "Width"
    assert ps[1].description == ""


def test_parse_params_two_assignments_on_one_line_keep_their_own_annotations():
    ps = parse_params("w = 80; t = 5; // [1:10]\nz = 1;\n")
    assert [p.name for p in ps] == ["w", "t", "z"]
    assert (ps[0].minimum, ps[0].maximum) == (None, None)   # the range belongs to t, not w
    assert (ps[1].minimum, ps[1].maximum) == (1.0, 10.0)
    assert [p.name for p in parse_params("$fa = 6; w = 80;")] == ["w"]   # a skipped $var hides nothing


def test_parse_params_on_empty_or_comment_only_source():
    assert parse_params("") == []
    assert parse_params("// just a note\n/* block */\n") == []


def test_parse_params_never_reads_a_brief_header_line_as_a_description():
    # The coder is told to leave a blank line after the header; when it forgets, the header's last line
    # sat DIRECTLY above the first parameter and became its description — the parameter panel showed
    # "Units: mm" beside `w`. Header fields and part bullets are never a description, blank or no blank.
    src = "// Design: X — y\n// Units: mm\nw = 80;\n"
    assert parse_params(src)[0].description == ""
    src = ("// Design: X\n// Parts:\n// - base\n// - lid\nw = 80;\n// Material: PLA\nt = 3;\n"
           "// Quality: fine\nq = 1;\n")
    assert [p.description for p in parse_params(src)] == ["", "", ""]
    # ...and a real description directly under a header line still reads
    src = "// Design: X\n// Units: mm\n// Width\nw = 80;\n"
    assert parse_params(src)[0].description == "Width"


def test_parse_params_negative_and_float_ranges():
    ps = parse_params("x = -5; // [-10:10]\ny = 0.5; // [0:0.1:1]\n")
    assert (ps[0].minimum, ps[0].maximum) == (-10.0, 10.0)
    assert (ps[1].minimum, ps[1].step, ps[1].maximum) == (0.0, 0.1, 1.0)


# ═══════════════════════════════ parse_brief ═══════════════════════════════

def test_parse_brief_line_comment_form():
    src = (
        "// Design: Pipe wall bracket — a saddle bracket for 60.3 mm pipe with two M6 mounting holes\n"
        "// Units: mm\n"
        "// Parts:\n"
        "// - base plate\n"
        "// - saddle\n"
        "// - gusset\n"
        "use <helix.scad>;\n"
        "// Width\nw = 80;\n"
    )
    assert parse_brief(src) == {
        "title": "Pipe wall bracket",
        "summary": "a saddle bracket for 60.3 mm pipe with two M6 mounting holes",
        "parts": ["base plate", "saddle", "gusset"],
    }


def test_parse_brief_block_comment_and_inline_parts():
    src = (
        "/* Design: Pipe wall bracket\n"
        "   A wall-mounted saddle bracket for 2-inch pipe.\n"
        "   Parts: base plate, saddle, gusset */\n"
        "use <helix.scad>;\n"
    )
    b = parse_brief(src)
    assert b["title"] == "Pipe wall bracket"
    assert b["summary"] == "A wall-mounted saddle bracket for 2-inch pipe."
    assert b["parts"] == ["base plate", "saddle", "gusset"]


def test_parse_brief_tolerates_a_use_line_first_and_missing_pieces():
    b = parse_brief("use <helix.scad>;\n// Design: Thing\n// Holds stuff.\n// More.\n\nw = 1;\n")
    assert b == {"title": "Thing", "summary": "Holds stuff. More.", "parts": []}
    assert parse_brief("w = 1;\ncube(w);\n") == {"title": "", "summary": "", "parts": []}
    assert parse_brief("") == {"title": "", "summary": "", "parts": []}
    only = parse_brief("// Design: Spacer\nw = 1;\n")
    assert only["title"] == "Spacer" and only["summary"] == "Spacer" and only["parts"] == []


def test_parse_brief_does_not_swallow_the_first_parameter_description():
    src = "// Design: Bracket\n// A bracket.\n\n// Width of the plate\nw = 80;\n"
    assert parse_brief(src)["summary"] == "A bracket."
    # ...even with no blank line: the comment directly above an assignment is that parameter's
    # description by the customizer convention parse_params reads, never brief prose
    src = "// Design: Bracket\n// A bracket.\n// Width of the plate\nw = 80;\n"
    assert parse_brief(src)["summary"] == "A bracket."


def test_parse_brief_keeps_the_key_dimensions_line_that_follows_parts():
    # EXACTLY the header shape build_3d_model_prompt teaches (brief, then ONE blank line, then the
    # customizer block): the line AFTER Parts is "the key dimensions in words" — the numbers the
    # critic judges a preview against. The reader used to stop at Parts and hand the critic a brief
    # with no numbers in it.
    src = (
        "// Design: Pipe wall bracket — a saddle bracket for 2-inch pipe that mounts to a wall\n"
        "// Units: mm\n"
        "// Parts: base plate, saddle, gusset\n"
        "// 80 x 40 base, 5 thick; saddle for 60.3 mm pipe; two M6 holes at 60 centres\n"
        "\n"
        "// overall width of the base plate\n"
        "width = 80;          // [40:200]\n"
        "use <helix.scad>;\n"
    )
    b = parse_brief(src)
    assert b["title"] == "Pipe wall bracket"
    assert b["parts"] == ["base plate", "saddle", "gusset"]
    assert b["summary"] == (
        "a saddle bracket for 2-inch pipe that mounts to a wall "
        "80 x 40 base, 5 thick; saddle for 60.3 mm pipe; two M6 holes at 60 centres"
    )
    assert "overall width" not in b["summary"]
    # the bullet form of Parts, followed by prose, reads the same way — and a sibling field between
    # them (Material) is still not prose
    src = (
        "// Design: Box — a lidded box\n// Parts:\n// - body\n// - lid\n// Material: PLA\n"
        "// 100 x 60 x 40 outside, 2 mm walls\n\n// wall\nwall = 2;\n"
    )
    b = parse_brief(src)
    assert b["parts"] == ["body", "lid"]
    assert b["summary"] == "a lidded box 100 x 60 x 40 outside, 2 mm walls"
    # a /* [Group] */ header still ends the brief, blank line or not
    src = "// Design: Box — a box\n// Parts: body\n// 10 x 10\n/* [Dimensions] */\n// w\nw = 1;\n"
    assert parse_brief(src)["summary"] == "a box 10 x 10"


# ═══════════════════════════════ friendly_error ═══════════════════════════════

# Real OpenSCAD 2021.01 message shapes, and what the user should hear for each.
_ERROR_CORPUS = [
    ('ERROR: Parser error in file "C:/Users/brian/HELIX/work/model.scad", line 12: syntax error\n'
     "Can't parse file 'model.scad'!\n",
     "syntax slip", "line 12"),
    ("WARNING: Ignoring unknown module 'rounded_bocks' in file model.scad, line 14\n"
     "WARNING: Current top level object is empty.\n",
     "shape helper that doesn't exist", "rounded_bocks"),
    ("WARNING: Ignoring unknown function 'm_clearence' in file model.scad, line 9\n",
     "shape helper that doesn't exist", "m_clearence"),
    ("WARNING: Ignoring unknown variable 'widht' in file model.scad, line 20\n"
     "ERROR: CGAL error in CGAL_Nef_polyhedron3(): CGAL ERROR: assertion violation!\n",
     "value it never defined", "widht"),
    ("Compiling design (CSG Products normalization)...\n"
     "Rendering Polygon Mesh using CGAL...\n"
     "ERROR: CGAL error in CGAL_Nef_polyhedron3(): CGAL ERROR: assertion violation! "
     "Expr: e->incident_sface() != SFace_const_handle() File: Nef_3/SNC_FM_decorator.h Line: 427\n",
     "impossible to solidify", "CGAL error"),
    ("ERROR: The given mesh is not closed! Unable to convert to CGAL_Nef_Polyhedron.\n",
     "impossible to solidify", "not closed"),
    ("Compiling design (CSG Tree generation)...\n"
     "WARNING: Current top level object is empty.\n"
     "Current top level object is empty.\n",
     "drew nothing", "top level object is empty"),
    ("WARNING: Can't open include file 'BOSL2/std.scad'.\n"
     "WARNING: Ignoring unknown module 'cuboid' in file model.scad, line 8\n",
     "library that isn't installed", "BOSL2/std.scad"),
    ("WARNING: Can't open library 'BOSL2/std.scad'.\n",
     "library that isn't installed", "BOSL2/std.scad"),
    ("ERROR: Assertion 'w > 0' failed: \"width must be positive\" in file model.scad, line 5\n",
     "size check", "line 5"),
    # a newer (Manifold-backend) build's wording for the same impossible-geometry class
    ("WARNING: PolySet -> Manifold conversion failed: NotManifold\n",
     "impossible to solidify", "Manifold"),
]


@pytest.mark.parametrize("stderr,expect_sentence,expect_detail", _ERROR_CORPUS)
def test_friendly_error_recognises_real_openscad_messages(stderr, expect_sentence, expect_detail):
    sentence, detail = friendly_error(stderr)
    assert expect_sentence in sentence
    assert expect_detail in detail
    # The user's sentence names nothing internal — no paths, no compiler vocabulary.
    assert "C:/" not in sentence and ".scad" not in sentence and "CGAL" not in sentence
    # The coder's detail carries the compiler's words but never a user path.
    assert "C:/Users" not in detail and "brian" not in detail


def test_friendly_error_trims_detail_and_survives_empty_output():
    noisy = "\n".join(f"WARNING: Ignoring unknown variable 'v{i}' in file model.scad, line {i}"
                      for i in range(200))
    sentence, detail = friendly_error(noisy)
    assert len(detail) <= 800 and detail.endswith("…")
    sentence, detail = friendly_error("")
    assert sentence and detail == ""
    sentence, detail = friendly_error("something odd happened\nand again")
    assert detail == "something odd happened\nand again"   # unrecognised → the tail is the detail


# ═══════════════════════════════ inspect_source ═══════════════════════════════

_GOOD = """\
// Design: Spacer — a 10 mm spacer
// Units: mm
use <helix.scad>;
d = 10;
helix_quality("normal") {
    tube(d, 4, 6);
}
"""


def test_inspect_source_passes_a_sound_file():
    assert inspect_source(_GOOD) == []


def test_inspect_source_flags_no_top_level_geometry():
    src = "// Units: mm\nmodule bracket() { cube(10); }\nmodule other() { sphere(2); }\n"
    problems = inspect_source(src)
    assert len(problems) == 1 and "No top-level geometry" in problems[0]
    # …and a `use <helix.scad>` line (no semicolon) must not hide a real instantiation after it.
    assert inspect_source("// mm\nuse <helix.scad>\nmodule b() { cube(1); }\nb();\n") == []
    assert inspect_source("// mm\nuse <helix.scad>\nb();\nmodule b() { cube(1); }\n") == []


def test_inspect_source_flags_foreign_libraries_only():
    src = "// Units: mm\ninclude <BOSL2/std.scad>\ncuboid([10, 10, 10]);\n"
    problems = inspect_source(src)
    assert any("BOSL2/std.scad" in p and "helix.scad" in p for p in problems)
    assert not any("BOSL2" in p for p in inspect_source(_GOOD))


def test_inspect_source_flags_unbalanced_braces_without_piling_on():
    problems = inspect_source("// mm\nmodule a() { cube(1);\na();\n")
    assert problems == ["Unbalanced braces: 1 '{' but 0 '}'."]
    problems = inspect_source("// mm\ncube((1);\n")
    assert any("parentheses" in p for p in problems)
    # braces inside comments and strings do not count
    assert inspect_source('// mm { (\nx = "}";\ncube(1);\n') == []


def test_inspect_source_flags_missing_units_hint_and_heavy_fn():
    problems = inspect_source("cube(10);\n")
    assert any("units" in p.lower() for p in problems)
    problems = inspect_source("// mm\n$fn = 200;\ncube(10);\n")
    assert any("$fn = 200" in p for p in problems)
    assert inspect_source("// mm\n$fn = 48;\ncube(10);\n") == []


# ═══════════════════════════════ the adapter, against a fake Popen ═══════════════════════════════

class _FakeProc:
    """One scripted child. `script(argv, kwargs)` may write files (a compile that 'succeeds')."""

    def __init__(self, argv, kwargs, *, lines=(), returncode=0, timeout_once=False, script=None):
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4242
        self._lines = list(lines)
        self._rc = returncode
        self._timeout_once = timeout_once
        self.returncode = None
        self.killed = False
        if script is not None:
            script(argv, kwargs)
        self.stdout = iter(self._lines)

    def wait(self, timeout=None):
        if self._timeout_once:
            self._timeout_once = False
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _Spawner:
    """A stand-in for subprocess.Popen: records every spawn, answers from a per-program script."""

    def __init__(self):
        self.spawns: list[_FakeProc] = []
        self.plans: dict[str, dict] = {}   # basename of argv[0] -> kwargs for _FakeProc

    def plan(self, program: str, **kw):
        self.plans[program] = kw

    def __call__(self, argv, **kwargs):
        name = Path(argv[0]).name.lower()
        plan = self.plans.get(name, {})
        proc = _FakeProc(argv, kwargs, **plan)
        self.spawns.append(proc)
        return proc

    def for_program(self, program: str) -> list[_FakeProc]:
        return [s for s in self.spawns if Path(s.argv[0]).name.lower() == program]


@pytest.fixture
def spawner(monkeypatch):
    sp = _Spawner()
    monkeypatch.setattr(openscad_cli.subprocess, "Popen", sp)
    return sp


@pytest.fixture
def fake_exe(tmp_path, monkeypatch):
    """A binary that 'exists': discovery finds it in a candidate dir, nothing on PATH or in settings."""
    d = tmp_path / "OpenSCAD"
    d.mkdir()
    exe = d / ("openscad.com" if openscad_cli._IS_WINDOWS else "openscad")
    exe.write_text("x")
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [d])
    return exe


def _writes(out_bytes: bytes):
    """A script that makes the child 'produce' its -o target."""
    def _script(argv, kwargs):
        i = argv.index("-o")
        Path(argv[i + 1]).write_bytes(out_bytes)
    return _script


def _model(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    src = ws / "model.scad"
    src.write_text("// mm\ncube(10);\n", encoding="utf-8")
    return src


# ----- discovery -----

def test_discovery_prefers_the_settings_override_then_path_then_known_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    over = tmp_path / "custom" / "openscad.com"
    over.parent.mkdir()
    over.write_text("x")
    on_path = tmp_path / "bin" / "openscad.com"
    on_path.parent.mkdir()
    on_path.write_text("x")
    known = tmp_path / "Program Files" / "OpenSCAD"
    known.mkdir(parents=True)
    (known / "openscad.com").write_text("x")
    monkeypatch.setattr(openscad_cli.shutil, "which",
                        lambda name: str(on_path) if name == "openscad.com" else None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [known])

    assert OpenScadCli(path_override=lambda: str(over))._discover() == str(over)
    assert OpenScadCli(path_override=lambda: "")._discover() == str(on_path)
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    assert OpenScadCli()._discover() == str(known / "openscad.com")
    # a directory override is searched, and a bad override falls through instead of breaking discovery
    assert OpenScadCli(path_override=lambda: str(over.parent))._discover() == str(over)
    # Explorer's "Copy as path" wraps the path in quotes; a pasted override still resolves
    assert OpenScadCli(path_override=lambda: f'"{over}"')._discover() == str(over)
    assert OpenScadCli(path_override=lambda: str(tmp_path / "nope.exe"))._discover() == str(
        known / "openscad.com")


def test_discovery_prefers_com_over_exe_on_windows(tmp_path, monkeypatch):
    """openscad.exe is the GUI subsystem build and swallows stdout; a compile error through it looks
    like a silent success. Whatever route finds the .exe, the sibling .com wins."""
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    d = tmp_path / "OpenSCAD"
    d.mkdir()
    exe, com = d / "openscad.exe", d / "openscad.com"
    exe.write_text("x")
    com.write_text("x")
    # PATH only knows the .exe
    monkeypatch.setattr(openscad_cli.shutil, "which",
                        lambda name: str(exe) if name == "openscad.exe" else None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [])
    assert OpenScadCli()._discover() == str(com)
    # the override names the .exe
    assert OpenScadCli(path_override=lambda: str(exe))._discover() == str(com)
    # a known dir is searched .com first
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [d])
    assert OpenScadCli()._discover() == str(com)
    # …and with no .com around, the .exe is still better than nothing
    com.unlink()
    assert OpenScadCli()._discover() == str(exe)


def test_candidate_dirs_cover_the_install_locations(monkeypatch, tmp_path):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "PF86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Home"))
    pkg = (tmp_path / "Local" / "Microsoft" / "WinGet" / "Packages"
           / "OpenSCAD.OpenSCAD_Microsoft.Winget.Source_8wekyb3d8bbwe")
    pkg.mkdir(parents=True)
    dirs = {str(p) for p in openscad_cli._candidate_dirs()}
    for want in (
        tmp_path / "PF" / "OpenSCAD", tmp_path / "PF86" / "OpenSCAD",
        tmp_path / "Local" / "Programs" / "OpenSCAD",
        tmp_path / "Home" / "scoop" / "shims",
        tmp_path / "Local" / "Microsoft" / "WinGet" / "Links", pkg,
    ):
        assert str(want) in dirs


def test_nothing_found_means_unavailable_and_no_process_is_ever_spawned(tmp_path, spawner, monkeypatch):
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [tmp_path / "nowhere"])
    cli = OpenScadCli()
    assert cli.available() is False
    assert cli.version() is None
    res = cli.compile_stl(_model(tmp_path), tmp_path / "out.stl")
    assert res.ok is False and res.output is None
    assert "isn't installed" in res.problem and "install it" in res.problem
    assert spawner.spawns == []


def test_a_miss_is_never_cached_so_an_engine_installed_by_hand_is_found(tmp_path, spawner, monkeypatch):
    # Every user-facing sentence promises "install it and HELIX will find it". A user who installs
    # OpenSCAD themselves mid-session (or lets a timed-out winget finish) must not be told it is still
    # missing until a restart — and nothing RUNS while the engine is absent, so no failed run would ever
    # drop a cached miss. A hit is still cached (the path is asked for on every pre-flight).
    d = tmp_path / "OpenSCAD"
    d.mkdir()
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [d])
    cli = OpenScadCli()
    assert cli.available() is False
    assert cli.available() is False
    exe = d / ("openscad.com" if openscad_cli._IS_WINDOWS else "openscad")
    exe.write_text("x")                                # the user installed it by hand
    assert cli.available() is True                     # found — no install(), no restart
    assert spawner.spawns == []
    probes = []
    real = cli._discover
    monkeypatch.setattr(cli, "_discover", lambda: (probes.append(1), real())[1])
    cli.available()
    cli.available()
    assert probes == []                                # the hit is cached
    # the other way round, via which(): a miss then a PATH that knows the binary
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [tmp_path / "nowhere"])
    cli2 = OpenScadCli()
    assert cli2.available() is False
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: str(exe) if name == exe.name else None)
    assert cli2.available() is True


# ----- version -----

def test_version_is_parsed_from_either_stream(fake_exe, spawner):
    # some builds print the banner on stderr; the runner merges both, so the fake needs no distinction
    spawner.plan(fake_exe.name, lines=["OpenSCAD version 2021.01\n"], returncode=0)
    cli = OpenScadCli()
    assert cli.version() == "2021.01"
    argv = spawner.spawns[0].argv
    assert argv == [str(fake_exe), "--version"]
    assert cli.version() == "2021.01" and len(spawner.spawns) == 1   # cached


def test_version_is_none_when_the_binary_will_not_run(fake_exe, spawner):
    spawner.plan(fake_exe.name, lines=["something unrelated\n"], returncode=1)
    assert OpenScadCli().version() is None


# ----- the runner and compiles -----

def test_compile_stl_argv_cwd_env_and_flags(fake_exe, spawner, tmp_path):
    """The one runner: no console window, stdin closed, both streams captured, cwd at the model so
    `use <helix.scad>` resolves and the compiler names `model.scad` (no user path), OPENSCADPATH set."""
    spawner.plan(fake_exe.name, lines=["Compiling design...\n"], returncode=0, script=_writes(b"solid x"))
    src = _model(tmp_path)
    out = tmp_path / "ws" / "assets" / "model.stl"
    libs = tmp_path / "libs"
    res = OpenScadCli(libraries_dir=libs).compile_stl(src, out, timeout_s=33)
    assert res.ok is True and res.output == out and out.read_bytes() == b"solid x"
    assert res.problem is None and res.detail is None and res.seconds >= 0
    proc = spawner.spawns[0]
    assert proc.argv[0] == str(fake_exe)
    assert proc.argv[1] == "-o" and proc.argv[3] == "model.scad" and len(proc.argv) == 4
    tmp_target = Path(proc.argv[2])
    assert tmp_target.parent == out.parent and tmp_target.suffix == ".stl" and tmp_target != out
    assert not tmp_target.exists()                      # moved into place, not left behind
    kw = proc.kwargs
    assert Path(kw["cwd"]) == src.parent
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["stdout"] is subprocess.PIPE and kw["stderr"] is subprocess.STDOUT
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kw["env"]["OPENSCADPATH"].split(os.pathsep)[0] == str(libs)
    assert kw["text"] is True and kw["errors"] == "replace"


def test_export_3mf_goes_through_the_same_runner(fake_exe, spawner, tmp_path):
    spawner.plan(fake_exe.name, returncode=0, script=_writes(b"PK3mf"))
    out = tmp_path / "ws" / "assets" / "model.3mf"
    res = OpenScadCli().export_3mf(_model(tmp_path), out)
    assert res.ok and out.read_bytes() == b"PK3mf"
    assert Path(spawner.spawns[0].argv[2]).suffix == ".3mf"   # the engine picks the format by suffix


def test_nonzero_exit_is_a_friendly_failure_that_keeps_the_last_good_file(fake_exe, spawner, tmp_path):
    spawner.plan(fake_exe.name, returncode=1, lines=[
        'ERROR: Parser error in file "model.scad", line 12: syntax error\n',
        "Can't parse file 'model.scad'!\n",
    ])
    src = _model(tmp_path)
    out = tmp_path / "ws" / "assets" / "model.stl"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"previous good mesh")
    res = OpenScadCli().compile_stl(src, out)
    assert res.ok is False and res.output is None
    assert "syntax slip" in res.problem and "model.scad" not in res.problem
    assert "line 12" in res.detail
    assert out.read_bytes() == b"previous good mesh"     # the viewer still has something to show
    assert not list(out.parent.glob("*.helixtmp*"))        # no partial file left behind


def test_missing_or_empty_output_is_a_failure_even_on_exit_zero(fake_exe, spawner, tmp_path):
    src = _model(tmp_path)
    out = tmp_path / "ws" / "assets" / "model.stl"
    spawner.plan(fake_exe.name, returncode=0, lines=["WARNING: Current top level object is empty.\n"])
    res = OpenScadCli().compile_stl(src, out)
    assert res.ok is False and "drew nothing" in res.problem and not out.exists()
    spawner.plan(fake_exe.name, returncode=0, script=_writes(b""))
    res = OpenScadCli().compile_stl(src, out)
    assert res.ok is False and not out.exists()
    assert res.detail   # the coder gets SOMETHING to act on even when the compiler said nothing


def test_timeout_kills_the_tree_and_says_took_too_long(fake_exe, spawner, tmp_path, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    spawner.plan(fake_exe.name, timeout_once=True, lines=["Rendering Polygon Mesh using CGAL...\n"])
    spawner.plan("taskkill", returncode=0)
    res = OpenScadCli().compile_stl(_model(tmp_path), tmp_path / "out.stl", timeout_s=180)
    assert res.ok is False
    assert "took too long" in res.problem and "180" in res.problem
    assert "timed out" in res.detail
    kills = spawner.for_program("taskkill")
    assert kills and kills[0].argv == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert kills[0].kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert spawner.spawns[0].killed     # belt and braces after taskkill


def test_timeout_off_windows_uses_a_plain_kill(fake_exe, spawner, tmp_path, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", False)
    (fake_exe.parent / "openscad").write_text("x")     # the POSIX name, now that the platform is faked
    spawner.plan("openscad", timeout_once=True)
    res = OpenScadCli().compile_stl(_model(tmp_path), tmp_path / "out.stl", timeout_s=5)
    assert res.ok is False and "took too long" in res.problem
    assert spawner.spawns[0].killed and not spawner.for_program("taskkill")


def test_a_binary_that_will_not_start_is_reported_and_reprobed(fake_exe, spawner, tmp_path, monkeypatch):
    def _boom(argv, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified", argv[0])
    monkeypatch.setattr(openscad_cli.subprocess, "Popen", _boom)
    cli = OpenScadCli()
    assert cli.available() is True
    res = cli.compile_stl(_model(tmp_path), tmp_path / "out.stl")
    assert res.ok is False and "couldn't be started" in res.problem
    # the cache was dropped: the next availability check re-discovers (and finds the binary gone)
    fake_exe.unlink()
    assert cli.available() is False


def test_failed_run_drops_the_cached_path(fake_exe, spawner, tmp_path, monkeypatch):
    spawner.plan(fake_exe.name, returncode=1, lines=["ERROR: something\n"])
    cli = OpenScadCli()
    probes = []
    real = cli._discover
    monkeypatch.setattr(cli, "_discover", lambda: (probes.append(1), real())[1])
    cli.available()
    cli.available()
    assert len(probes) == 1                       # cached
    cli.compile_stl(_model(tmp_path), tmp_path / "out.stl")
    cli.available()
    assert len(probes) == 2                       # re-probed after the failed run


def test_missing_source_is_a_friendly_failure_without_a_spawn(fake_exe, spawner, tmp_path):
    res = OpenScadCli().compile_stl(tmp_path / "absent.scad", tmp_path / "out.stl")
    assert res.ok is False and "model.scad" in res.problem and spawner.spawns == []


# ----- render -----

def test_render_png_argv_uses_a_dark_scheme_and_the_viewall_camera(fake_exe, spawner, tmp_path):
    spawner.plan(fake_exe.name, returncode=0, script=_writes(b"\x89PNG"))
    out = tmp_path / "ws" / "assets" / "preview.png"
    res = OpenScadCli().render_png(_model(tmp_path), out, size=(640, 480))
    assert res.ok and out.read_bytes() == b"\x89PNG"
    argv = spawner.spawns[0].argv
    assert argv[1:6] == ["--autocenter", "--viewall", "--imgsize=640,480",
                         f"--colorscheme={DARK_COLORSCHEME}", "--projection=p"]
    assert argv[6] == "-o" and Path(argv[7]).suffix == ".png" and argv[8] == "model.scad"


def test_render_png_falls_back_to_the_default_scheme_when_the_build_rejects_ours(
    fake_exe, spawner, tmp_path, monkeypatch,
):
    calls = []

    def _script(argv, kwargs):
        calls.append(argv)
        if any(a.startswith("--colorscheme") for a in argv):
            return                                   # first try: nothing written
        _writes(b"\x89PNG")(argv, kwargs)

    def _popen(argv, **kwargs):   # per-call behaviour: reject the scheme once, then succeed
        if any(a.startswith("--colorscheme") for a in argv):
            proc = _FakeProc(argv, kwargs, returncode=1,
                             lines=[f"Unknown color scheme '{DARK_COLORSCHEME}'. Valid schemes:\n"],
                             script=_script)
        else:
            proc = _FakeProc(argv, kwargs, returncode=0, script=_script)
        spawner.spawns.append(proc)
        return proc

    monkeypatch.setattr(openscad_cli.subprocess, "Popen", _popen)
    out = tmp_path / "ws" / "assets" / "preview.png"
    res = OpenScadCli().render_png(_model(tmp_path), out)
    assert res.ok and out.exists()
    assert len(calls) == 2 and not any(a.startswith("--colorscheme") for a in calls[1])


# ----- install -----

def test_install_runs_winget_with_the_exact_flags_narrates_and_reprobes(tmp_path, spawner, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    target = tmp_path / "Program Files" / "OpenSCAD"
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [target])
    monkeypatch.setattr(openscad_cli.shutil, "which",
                        lambda name: r"C:\WindowsApps\winget.exe" if name == "winget" else None)

    def _installs(argv, kwargs):
        target.mkdir(parents=True, exist_ok=True)
        (target / "openscad.com").write_text("x")
        (target / "openscad.exe").write_text("x")

    spawner.plan("winget.exe", returncode=0, script=_installs, lines=[
        "Found OpenSCAD [OpenSCAD.OpenSCAD] Version 2021.01\n",
        "Downloading https://files.openscad.org/OpenSCAD-2021.01-x86-64-Installer.exe\r"
        "  ██████████████▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒  10.0 MB / 30.0 MB\n",
        "  -\r  \\\r  |\r  /\n",
        "Successfully installed\n",
    ])
    heard: list[str] = []
    cli = OpenScadCli()
    assert cli.available() is False
    res = cli.install(on_progress=heard.append, timeout_s=900)
    assert res.ok is True and res.output == target / "openscad.com"
    proc = spawner.for_program("winget.exe")[0]
    assert proc.argv == [r"C:\WindowsApps\winget.exe", "install", "--id", "OpenSCAD.OpenSCAD", "-e",
                         "--accept-source-agreements", "--accept-package-agreements"]
    assert tuple(proc.argv[1:]) == WINGET_ARGS
    assert proc.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert proc.kwargs["stdin"] is subprocess.DEVNULL
    assert heard[0].startswith("Installing the hologram engine")
    assert "Found OpenSCAD [OpenSCAD.OpenSCAD] Version 2021.01" in heard
    assert "Successfully installed" in heard
    assert not any("█" in h or h.strip() in ("-", "\\", "|", "/") for h in heard)   # bars/spinners dropped
    assert cli.available() is True and cli._resolve() == str(target / "openscad.com")


def test_install_is_ok_only_if_the_binary_is_then_found(tmp_path, spawner, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [tmp_path / "nowhere"])
    monkeypatch.setattr(openscad_cli.shutil, "which",
                        lambda name: "winget" if name == "winget" else None)
    spawner.plan("winget", returncode=0, lines=["Successfully installed\n"])   # lies
    res = OpenScadCli().install()
    assert res.ok is False and res.output is None
    assert "openscad.org" in res.problem
    # exit code is advisory the other way too: "already installed" (non-zero) + binary present → ok
    d = tmp_path / "nowhere"
    d.mkdir()
    (d / ("openscad.com" if openscad_cli._IS_WINDOWS else "openscad")).write_text("x")
    spawner.plan("winget", returncode=-1978335189, lines=["Found an existing package already installed.\n"])
    assert OpenScadCli().install().ok is True


def test_install_without_winget_or_off_windows_says_how_instead(tmp_path, spawner, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(openscad_cli.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    res = OpenScadCli().install()
    assert res.ok is False and "winget" in res.problem and spawner.spawns == []
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", False)
    res = OpenScadCli().install()
    assert res.ok is False and "brew" in res.problem and spawner.spawns == []


def test_install_timeout_and_launch_failure_are_friendly(tmp_path, spawner, monkeypatch):
    monkeypatch.setattr(openscad_cli, "_IS_WINDOWS", True)
    monkeypatch.setattr(openscad_cli, "_candidate_dirs", lambda: [tmp_path / "nowhere"])
    monkeypatch.setattr(openscad_cli.shutil, "which",
                        lambda name: "winget" if name == "winget" else None)
    spawner.plan("winget", timeout_once=True)
    spawner.plan("taskkill", returncode=0)
    res = OpenScadCli().install(timeout_s=900)
    assert res.ok is False and "took too long" in res.problem
    assert spawner.for_program("taskkill")      # the installer tree is torn down, not orphaned

    def _boom(argv, **kwargs):
        raise OSError("no")
    monkeypatch.setattr(openscad_cli.subprocess, "Popen", _boom)
    res = OpenScadCli().install()
    assert res.ok is False and "couldn't be started" in res.problem


def test_install_hint_is_one_warm_sentence():
    hint = OpenScadCli().install_hint()
    assert "OpenSCAD" in hint and "free" in hint
    assert hint.count(". ") == 0 and hint.endswith(".")
    if openscad_cli._IS_WINDOWS:
        assert "install it" in hint


def test_progress_text_keeps_words_and_drops_bars():
    assert openscad_cli._progress_text("  -\r  \\\r  |\r  /") == ""
    assert openscad_cli._progress_text("  ██████████▒▒▒▒▒  10.0 MB / 30.0 MB") == ""
    assert openscad_cli._progress_text("Downloading x\r  ████  1 MB / 2 MB") == ""
    assert openscad_cli._progress_text("Found OpenSCAD [OpenSCAD.OpenSCAD] Version 2021.01") == (
        "Found OpenSCAD [OpenSCAD.OpenSCAD] Version 2021.01")
    assert openscad_cli._progress_text("--accept-source-agreements ok") == "--accept-source-agreements ok"


def test_the_adapter_satisfies_the_port_surface():
    """Other units code against the contract's names; the adapter must expose exactly them."""
    for name in ("available", "version", "compile_stl", "export_3mf", "render_png", "install",
                 "install_hint"):
        assert callable(getattr(OpenScadCli, name))
    r = CadResult(ok=True, output=Path("x"), problem=None, detail=None, seconds=0.1)
    assert r.ok and r.output == Path("x")


# ═══════════════════════════════ the real engine, when it is here ═══════════════════════════════

_REAL = OpenScadCli()
needs_openscad = pytest.mark.skipif(
    not _REAL.available(), reason="OpenSCAD is not installed on this machine (install it to run these)",
)

_EVERY_HELPER = """\
// Design: Helper exerciser — one of everything in helix.scad
// Units: mm
use <helix.scad>;
w = 60; // [40:100]
helix_quality("draft") {
    difference() {
        union() {
            rounded_plate([w, 40, 5], 4);
            translate([0, 0, 5]) rounded_box([20, 20, 10], 2);
            translate([25, 0, 5]) chamfered_cylinder(8, 6, 1);
            translate([-25, 0, 5]) tube(8, 4, 6);
            translate([0, 15, 5]) cyl(4, 3);
        }
        rect_pattern(2, 2, 44, 26) countersunk_hole(m_clearance(4), 5, m_csk_d(4));
        translate([0, -14, 0]) slot(14, m_clearance(3), 5);
        ring_pattern(4, 14) counterbore_hole(m_tap(3), 5, m_head_d(3), 2);
        translate([0, 0, 15 - 3]) hex_pocket(m_nut_af(4) + 0.3, 3);
    }
    echo(sizes = [m_size("M4"), m_clearance("M4"), m_tap(4)]);
}
"""


@needs_openscad
def test_real_engine_reports_a_version():
    assert _REAL.version()


@needs_openscad
def test_real_engine_compiles_a_model_using_every_helper(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / HELIX_LIB_FILE).write_text(HELIX_LIB, encoding="utf-8")
    (ws / "model.scad").write_text(_EVERY_HELPER, encoding="utf-8")
    res = _REAL.compile_stl(ws / "model.scad", ws / "assets" / "model.stl")
    assert res.ok, (res.problem, res.detail)
    assert (ws / "assets" / "model.stl").stat().st_size > 1000
    png = _REAL.render_png(ws / "model.scad", ws / "assets" / "preview.png", size=(320, 240))
    assert png.ok, (png.problem, png.detail)
    assert (ws / "assets" / "preview.png").stat().st_size > 0


@needs_openscad
def test_real_engine_turns_a_syntax_slip_into_a_friendly_problem(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "model.scad").write_text("// mm\ncube(10;\n", encoding="utf-8")
    res = _REAL.compile_stl(ws / "model.scad", ws / "out.stl")
    assert res.ok is False and "syntax slip" in res.problem and "line" in res.detail
