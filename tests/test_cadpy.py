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
