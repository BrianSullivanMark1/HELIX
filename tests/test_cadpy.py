"""domain.cadpy tests — the hologram design language: parse, rewrite, lint, and the safety gate.

Pure domain: no build123d, no subprocess, no I/O. The parser pins here are LOCKSTEP pins — the coder
prompt teaches exactly these shapes, the studio's sliders drive set_params, and the critic reads
parse_brief; drift in any of them breaks verbal design silently.
"""
from __future__ import annotations

import ast

from helix.domain import cadpy
from helix.domain.cadpy import (
    HELIX_LIB,
    PARAM_END,
    PARAM_START,
    friendly_error,
    inspect_source,
    parse_brief,
    parse_params,
    set_params,
)

SRC = (
    '"""Design: Relay box - a vented enclosure for a 2-channel relay\n'
    "Parts:\n"
    "- body\n"
    "- lid\n"
    '"""\n'
    "from helix_parts import *\n\n"
    "# --- Parameters ---\n"
    "wall = 2.0        # [1.2..4] wall thickness, mm\n"
    "inner_h = 32.0    # [20..80..0.5] inner height\n"
    "vents = True      # louvre the lid\n"
    'lid_style = "snap"  # [snap, screw] how the lid attaches\n'
    "derived = wall * 2\n"
    "# --- End Parameters ---\n\n\n"
    "def build():\n"
    "    return shell_box(60, 40, inner_h, wall=wall)\n"
)


# ----- parse_params -----

def test_parse_params_reads_numbers_bools_strings_with_ranges_steps_and_choices():
    params = {p.name: p for p in parse_params(SRC)}
    assert params["wall"].kind == "number"
    assert params["wall"].minimum == 1.2 and params["wall"].maximum == 4.0
    assert params["wall"].description == "wall thickness, mm"
    assert params["inner_h"].step == 0.5
    assert params["vents"].kind == "bool" and params["vents"].value == "True"
    assert params["lid_style"].kind == "string" and params["lid_style"].value == "snap"
    assert params["lid_style"].choices == ("snap", "screw")
    assert params["lid_style"].description == "how the lid attaches"


def test_parse_params_skips_derived_expressions_and_needs_the_markers():
    names = [p.name for p in parse_params(SRC)]
    assert "derived" not in names, "an expression is a derived value, not a knob"
    assert parse_params(SRC.replace(PARAM_START, "# params")) == []
    assert parse_params("x = 1\n") == []


# ----- set_params (the sliders' Commit) -----

def test_set_params_rewrites_literals_in_place_and_keeps_annotations():
    out = set_params(SRC, {"wall": 3, "vents": False, "lid_style": "screw", "unknown": 9})
    params = {p.name: p for p in parse_params(out)}
    assert params["wall"].value == "3"
    assert params["vents"].value == "False"
    assert params["lid_style"].value == "screw"
    # the annotation (range + description) survives the rewrite — the sliders must not eat themselves
    assert params["wall"].minimum == 1.2 and params["wall"].description == "wall thickness, mm"
    assert "unknown" not in out
    # everything outside the block is untouched
    assert out.splitlines()[0] == SRC.splitlines()[0]
    assert "def build():" in out


def test_set_params_only_touches_the_block():
    tail = SRC + "\n# wall = 99 mentioned in a comment\n"
    out = set_params(tail, {"wall": 3.5})
    assert "# wall = 99 mentioned in a comment" in out
    assert {p.name: p.value for p in parse_params(out)}["wall"] == "3.5"


# ----- parse_brief -----

def test_parse_brief_reads_the_docstring_shape():
    b = parse_brief(SRC)
    assert b["title"] == "Relay box"
    assert b["summary"] == "a vented enclosure for a 2-channel relay"
    assert b["parts"] == ["body", "lid"]


def test_parse_brief_requires_the_design_marker_and_survives_syntax_errors():
    assert parse_brief('"""Just a note."""\nx = 1\n') == {"title": "", "summary": "", "parts": []}
    # a file that doesn't even parse still yields its header textually (the critic can still judge)
    broken = SRC + "\ndef build(:\n"
    assert parse_brief(broken)["title"] == "Relay box"


# ----- inspect_source: lints + the safety gate -----

def test_good_source_has_no_lints():
    assert inspect_source(SRC) == []


def test_the_import_gate_blocks_everything_but_the_allowlist():
    for bad in ("import os", "import subprocess", "from os import path", "import requests",
                "from . import x"):
        lints = inspect_source(SRC.replace("from helix_parts import *",
                                           bad + "\nfrom helix_parts import *"))
        assert lints and ("may import only" in lints[0]), (bad, lints)
    # the allowlist itself is fine
    ok = SRC.replace("from helix_parts import *", "import math\nfrom helix_parts import *")
    assert inspect_source(ok) == []


def test_forbidden_calls_and_dunders_are_linted():
    assert any("open()" in lint for lint in inspect_source(
        SRC.replace("return shell_box(60, 40, inner_h, wall=wall)",
                    'open("x")\n    return shell_box(60, 40, inner_h, wall=wall)')))
    assert any("dunder" in lint for lint in inspect_source(
        SRC.replace("return shell_box(60, 40, inner_h, wall=wall)",
                    "return build.__globals__")))


def test_top_level_geometry_missing_build_and_missing_params_are_linted():
    lints = inspect_source(SRC + "\nprint_me = shell_box(1, 1, 1)\nx = [i for i in range(3)]\n")
    assert inspect_source(SRC + "\nshell_box(1, 1, 1)\n")[0].startswith("model.py runs code at the top level")
    assert lints == []  # assignments at top level are fine — only expressions/statements are not
    assert any("build()" in lint for lint in inspect_source("x = 1\n"))
    no_params = SRC.replace(PARAM_START, "# p").replace(PARAM_END, "# q")
    assert any(PARAM_START in lint for lint in inspect_source(no_params))


def test_a_syntax_error_is_one_lint_with_its_line():
    lints = inspect_source("def build(:\n")
    assert len(lints) == 1 and "syntax error" in lints[0] and "line 1" in lints[0]


# ----- friendly_error -----

def test_friendly_error_maps_the_common_failures_to_warm_sentences():
    cases = {
        "Traceback...\nNameError: name 'wdth' is not defined": "refers to a name",
        "worker timeout after 90s ... timed out": "took too long",
        "OCP.OCP.Standard.Standard_DomainError": "impossible dimension",
        "BRep_API: command not done": "couldn't be combined",
        "ZeroDivisionError: division by zero": "works out to zero",
    }
    for raw, expect in cases.items():
        problem, detail = friendly_error(raw)
        assert expect in problem, (raw, problem)
        assert "\\" not in problem and "site-packages" not in problem
    assert friendly_error("")[0]


# ----- the library itself -----

def test_the_parts_library_parses_and_carries_the_catalog():
    tree = ast.parse(HELIX_LIB)  # the seeded file must always be valid Python
    assert tree is not None
    for key in ("arduino_uno", "esp32_devkitc", "pi_pico", "relay_2ch", "buck_lm2596"):
        assert f'"{key}"' in HELIX_LIB, key
    for helper in ("def shell_box", "def lid_for", "def standoffs_for", "def side_rails",
                   "def vent_slots", "def usb_cutout", "def cable_gland_boss", "def arrange",
                   "def lip_ring", "def csk_hole", "def strap_tab"):
        assert helper in HELIX_LIB, helper
    # the doc the prompt embeds names every catalog board the library defines
    for key in ("arduino_uno", "esp32_devkitc", "relay_1ch"):
        assert key in cadpy.HELIX_LIB_DOC


# ----- render_boards: the catalog is the single source of the BOARDS block -----

class _Spec:
    """A stand-in BoardSpec so the rendered block can be evaluated without build123d."""

    def __init__(self, name, length, width, holes=(), hole_d=3.2, height=12.0, usb="", approx=False):
        self.name, self.length, self.width, self.holes = name, length, width, holes
        self.hole_d, self.height, self.usb, self.approx = hole_d, height, usb, approx


def _eval_boards(block: str) -> dict:
    ns: dict = {"BoardSpec": _Spec}
    exec(block, ns)  # noqa: S102 — our own rendered text
    return ns["BOARDS"]


def test_render_boards_unions_the_catalog_with_the_legacy_footprints():
    from helix.domain.components import CATALOG, Component, Hole, Port

    block = cadpy.render_boards()
    assert block == cadpy.render_boards(), "deterministic text — the seeded file is compared byte-for-byte"
    assert block in HELIX_LIB, "the seeded library carries the rendered block verbatim"
    boards = _eval_boards(block)
    for key in cadpy.LEGACY_BOARDS:
        assert key in boards, key                       # every key that ever worked still works
    for key in CATALOG:
        assert key in boards, key                       # and every catalog part is a board
    for key, c in CATALOG.items():
        b = boards[key]
        assert (b.name, b.length, b.width, b.height) == (c.name, c.length, c.width, c.height)
        assert b.holes == tuple((h.x, h.y) for h in c.holes)
        assert b.approx == (c.confidence < 0.7)
    # a legacy-only key keeps its numbers; a catalog key wins over its legacy twin
    legacy_only = [k for k in cadpy.LEGACY_BOARDS if k not in CATALOG]
    for key in legacy_only:
        name, length, width, holes, hole_d, height, usb, approx = cadpy.LEGACY_BOARDS[key]
        b = boards[key]
        assert (b.name, b.length, b.width, b.holes, b.hole_d, b.height, b.usb, b.approx) == \
            (name, length, width, holes, hole_d, height, usb, approx)
    # the rendering rules, pinned on a small catalog of our own
    mini = {
        "zz_board": Component(key="zz_board", name='Board "Z"', category="mcu", length=40.0, width=20.0, height=9.0,
                              holes=(Hole(2.0, 2.0, 2.6), Hole(38.0, 18.0, 2.6)),
                              ports=(Port("jst_ph", "back", 5.0), Port("usb_c", "left", 10.0)), confidence=0.65),
    }
    small = _eval_boards(cadpy.render_boards(mini))
    z = small["zz_board"]
    assert z.holes == ((2.0, 2.0), (38.0, 18.0)) and z.hole_d == 2.6 and z.usb == "left" and z.approx is True
    assert z.name == 'Board "Z"'
    assert "arduino_uno" in small and small["arduino_uno"].holes[0] == (14.0, 2.5)


# ----- the enclosure helpers added to helix_parts -----

def _defs(source: str) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}


def _signature(fn: ast.FunctionDef) -> list[tuple[str, object]]:
    args = fn.args.args
    defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
    out = []
    for a, d in zip(args, defaults):
        if d is None:
            out.append((a.arg, ...))
        elif isinstance(d, ast.Name):
            out.append((a.arg, d.id))          # a named constant default (FIT), reported by name
        else:
            out.append((a.arg, ast.literal_eval(d)))
    return out


def test_the_enclosure_helpers_have_the_documented_signatures():
    defs = _defs(HELIX_LIB)
    want = {
        "pocket": [("l", ...), ("w", ...), ("h", ...), ("rib", 1.6), ("clear", "FIT"), ("omit", "")],
        "pocket_for": [("spec", ...), ("h", None), ("rib", 1.6), ("clear", "FIT"), ("omit", "")],
        "battery_bay": [("l", ...), ("w", ...), ("h", ...), ("rib", 1.6), ("clear", 0.6), ("lead", 8.0), ("side", "right")],
        "lens_bore": [("d", ...), ("depth", ...), ("recess_d", 0.0), ("recess_h", 1.0)],
        "grille": [("d", ...), ("hole", 1.6), ("pitch", 2.8), ("depth", 6.0)],
        "mic_hole": [("d", 1.5), ("depth", 6.0)],
        "led_window": [("d", ...), ("depth", 6.0)],
        "screen_window": [("w", ...), ("h", ...), ("r", 1.0), ("depth", 6.0)],
        "switch_slot": [("kind", ...), ("depth", 6.0)],
        "port_slot": [("w", ...), ("h", ...), ("wall", ...), ("r", 1.5)],
        "wire_notch": [("w", ...), ("depth", ...)],
        "deboss_text": [("text", ...), ("size", ...), ("depth", ...), ("font", "Arial")],
        "deboss_tag": [("text", ...), ("size", ...), ("depth", ...)],
    }
    for name, sig in want.items():
        assert name in defs, name
        got = _signature(defs[name])
        assert got == sig, (name, got)
        assert name in cadpy.HELIX_LIB_DOC, f"{name} is not on the coder's cheat-sheet"
    # the switch table in the library is the planner's table, number for number
    table = next(n for n in ast.parse(HELIX_LIB).body
                 if isinstance(n, ast.Assign) and n.targets[0].id == "SWITCH_SLOTS")
    from helix.domain import enclosure

    assert ast.literal_eval(table.value) == enclosure.SWITCH_SLOTS
    assert ast.literal_eval(table.value)["ss12d00"] == ("slot", 8.5, 3.6)
    assert ast.literal_eval(table.value)["kcd1"][1:] == (19.2, 13.5)
    assert ast.literal_eval(table.value)["push_12"][1] == 12.4 and ast.literal_eval(table.value)["ky040"][1] == 7.2
    assert ast.literal_eval(table.value)["tact_6"][1] == 6.5


def test_deboss_text_degrades_to_a_tag_and_never_raises():
    fn = _defs(HELIX_LIB)["deboss_text"]
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, "deboss_text must guard the font"
    handler_calls = [n.func.id for h in tries[0].handlers for n in ast.walk(h) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)]
    assert "deboss_tag" in handler_calls
    assert "Text(" in ast.get_source_segment(HELIX_LIB, tries[0]) or "Text(" in HELIX_LIB


def test_the_lip_ring_nests_by_the_cavity_radius():
    """The lip's corners follow the cavity it enters (r - clear), not r - wall — the four-corner
    interference the assembled intersection once measured."""
    src = HELIX_LIB.split("def lip_ring")[1].split("def lip_rebate")[0]
    assert "r_lip = max(r - clear, 0.5)" in src and "r_bore = max(r - clear - t, 0.5)" in src
    assert "inboard" in src, "only the inboard loop of the flange is chamfered"


def test_the_doc_lists_the_catalog_keys_by_category():
    from helix.domain.components import CATALOG

    for key in list(CATALOG)[:5]:
        assert key in cadpy.HELIX_LIB_DOC, key
    assert "Plate-face CUTTERS" in cadpy.HELIX_LIB_DOC and "pocket(l,w,h,rib,clear,omit)" in cadpy.HELIX_LIB_DOC
