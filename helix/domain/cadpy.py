"""OpenSCAD's successor: the hologram design language is now PYTHON (build123d), as data + pure rules.

A hologram is still a PROGRAM the coder writes — that idea survives the engine swap untouched. What
changed is the language: `model.py` (build123d, a real B-rep CAD kernel — fillets, chamfers, STEP
export) instead of `model.scad` (a mesh kernel with a homemade dialect). LLMs write Python far more
accurately than OpenSCAD, and the parts here are what "make a case for an ESP32" needs to come out
*fitting*: a curated hardware catalog with real dimensions and an enclosure helper library.

This module is PURE domain (stdlib plus the component catalog — no build123d import, no I/O, no
subprocess):

  - `HELIX_LIB` / `HELIX_LIB_FILE`  — the helix_parts.py helper library seeded beside model.py. It
    imports build123d *at compile time in the runner subprocess*, never here. Its BOARDS block is
    RENDERED at import time by `render_boards()` from helix.domain.components.CATALOG (union the
    LEGACY_BOARDS the library carried before the catalog), so one source of truth feeds the
    parts list, the enclosure generator and `board(key)` in model.py.
  - `HELIX_LIB_DOC`                 — the model-facing cheat-sheet the coder prompt embeds.
  - `parse_params` / `set_params`   — the `# --- Parameters ---` block: read it for the studio's
    sliders; rewrite a value in place for "make it 100 wide" and the studio's Commit button.
  - `parse_brief`                   — the design header (module docstring) for the critic and HUD.
  - `inspect_source`                — cheap static lints + the SAFETY GATE: a design file may import
    only the small allowlist below, so a prompt-injected build can't smuggle os/subprocess calls
    into the compile step. (The compile still runs in a time-boxed subprocess besides.)
  - `friendly_error`                — a raw runner traceback → one warm sentence + trimmed detail.

The shapes here (`PyParam`, the (problem, detail) pair) deliberately mirror the retired scad module,
so the baker and viewer swapped engines without changing what they carry.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from helix.domain.components import CATALOG, Component

HELIX_LIB_FILE = "helix_parts.py"

PARAM_START = "# --- Parameters ---"
PARAM_END = "# --- End Parameters ---"

# The ONLY modules a design file may import (module or from-import; submodules of these are fine).
# Everything a parametric part needs, nothing that reaches the machine: no os, no sys, no
# subprocess, no pathlib, no socket, no shutil, no importlib.
ALLOWED_IMPORTS = frozenset({"helix_parts", "build123d", "math", "typing", "dataclasses"})

# Builtin calls a design file may not make — the file computes geometry, it does not do I/O or
# reflection. (The runner also execs with the import gate above already enforced by lint + the
# Forge's repair loop; this list is the second belt.)
FORBIDDEN_CALLS = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "memoryview",
})


@dataclass(frozen=True)
class PyParam:
    """One adjustable value from the `# --- Parameters ---` block.

    Field-compatible with the retired ScadParam on purpose: the baker's viewer data, the studio's
    sliders, and the critic's brief all read these fields and never needed to change.

      width = 80.0    # [40..160] outer width, mm     -> number with a range
      vents = True    # add vent slots                -> bool toggle
      lid = "snap"    # [snap, screw] lid style       -> string with choices
    """

    name: str
    value: str                     # the literal, exactly as written ("80.0", "True", '"snap"')
    kind: str = "number"           # number | bool | string
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)


_ASSIGN_RE = re.compile(
    r"""^(?P<indent>\s*)(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^#\n]+?)\s*(?:\#\s*(?P<comment>.*))?$"""
)
_RANGE_RE = re.compile(r"^\[\s*(-?\d+\.?\d*)\s*\.\.\s*(-?\d+\.?\d*)(?:\s*\.\.\s*(-?\d+\.?\d*))?\s*\]")
_CHOICE_RE = re.compile(r"^\[([^\]]+)\]")
_NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_STRING_RE = re.compile(r"""^(['"])(.*)\1$""")


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _classify(value: str) -> str:
    v = value.strip()
    if v in ("True", "False"):
        return "bool"
    if _NUMBER_RE.match(v):
        return "number"
    if _STRING_RE.match(v):
        return "string"
    return "expr"  # a computed value (width/2) — real, but not user-adjustable in the panel


def _param_block(source: str) -> tuple[int, int, list[str]] | None:
    """(start_line_idx, end_line_idx, lines) of the parameter block, or None. Indices bound the
    lines BETWEEN the markers (exclusive)."""
    lines = source.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if start is None and s == PARAM_START:
            start = i
        elif start is not None and s == PARAM_END:
            end = i
            break
    if start is None or end is None:
        return None
    return start + 1, end, lines


def parse_params(source: str) -> list[PyParam]:
    """Read the adjustable parameters between the block markers. Literals only — an assignment whose
    value is an expression is skipped (it is derived, not a knob). The comment after a value is its
    annotation: a leading `[min..max]` or `[min..max..step]` gives a number its range, a leading
    `[a, b, c]` gives a string its choices, and the rest is the human description."""
    block = _param_block(source)
    if block is None:
        return []
    lo, hi, lines = block
    params: list[PyParam] = []
    for raw in lines[lo:hi]:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = _ASSIGN_RE.match(raw)
        if not m:
            continue
        name, value = m.group("name"), m.group("value").strip()
        kind = _classify(value)
        if kind == "expr":
            continue
        comment = (m.group("comment") or "").strip()
        minimum = maximum = step = None
        choices: tuple[str, ...] = ()
        desc = comment
        if kind == "number":
            r = _RANGE_RE.match(comment)
            if r:
                minimum, maximum = _to_float(r.group(1)), _to_float(r.group(2))
                step = _to_float(r.group(3)) if r.group(3) else None
                desc = comment[r.end():].strip()
        elif kind == "string":
            c = _CHOICE_RE.match(comment)
            if c:
                choices = tuple(x.strip().strip("'\"") for x in c.group(1).split(",") if x.strip())
                desc = comment[c.end():].strip()
        if kind == "string":
            value_clean = _STRING_RE.match(value).group(2)  # bare text for display/choices matching
        else:
            value_clean = value
        params.append(PyParam(
            name=name, value=value_clean, kind=kind, description=desc.lstrip("-—: ").strip(),
            minimum=minimum, maximum=maximum, step=step, choices=choices,
        ))
    return params


def set_params(source: str, values: dict[str, object]) -> str:
    """Rewrite parameter literals in place — the studio's Commit button and 'make it 100 wide'.

    Only names inside the block are touched; annotations and layout survive byte-for-byte. A bool
    becomes True/False, a string is re-quoted, a number keeps int-ness when it has no fraction.
    Unknown names are ignored (the caller validates against parse_params first)."""
    block = _param_block(source)
    if block is None or not values:
        return source
    lo, hi, lines = block
    for i in range(lo, hi):
        m = _ASSIGN_RE.match(lines[i])
        if not m or m.group("name") not in values:
            continue
        new = values[m.group("name")]
        if isinstance(new, bool):
            lit = "True" if new else "False"
        elif isinstance(new, (int, float)):
            f = float(new)
            lit = str(int(f)) if f.is_integer() and abs(f) < 1e15 else repr(round(f, 6))
        else:
            lit = '"' + str(new).replace("\\", "\\\\").replace('"', '\\"') + '"'
        comment = m.group("comment")
        rebuilt = f"{m.group('indent')}{m.group('name')} = {lit}"
        if comment is not None:
            # Keep the annotation column roughly where it was so the block stays readable.
            pad = max(1, len(lines[i].split("#", 1)[0]) - len(rebuilt))
            rebuilt += " " * pad + "# " + comment
        lines[i] = rebuilt
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


_BULLET_RE = re.compile(r"^[-*•]\s+(.*)$")


def parse_brief(source: str) -> dict:
    """The design header, as data for the critic and the HUD.

    The coder writes it as the module docstring:

        \"\"\"Design: ESP32 relay enclosure — vented box with a snap lid for a 2-channel relay.
        Parts:
        - body with standoffs
        - snap-fit lid
        \"\"\"

    Returns {"title", "summary", "parts"}; anything missing is ""/[] — a courtesy, not a contract."""
    out = {"title": "", "summary": "", "parts": []}
    try:
        doc = ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        # A file that doesn't parse still often has the header intact — read it textually.
        m = re.search(r'["\']{3}(.*?)["\']{3}', source, re.S)
        doc = m.group(1) if m else ""
    lines = [ln.strip() for ln in doc.strip().splitlines()]
    if not lines:
        return out
    first = lines[0]
    m = re.match(r"(?i)^design\s*:\s*(.*)$", first)
    if not m:
        return out  # the taught shape opens with "Design:" — anything else is not a brief
    head = m.group(1).strip()
    title, summary = head, ""
    for sep in (" — ", " – ", " - ", ": "):
        if sep in head:
            title, summary = head.split(sep, 1)
            break
    out["title"] = title.strip().rstrip(".")
    bits = [summary.strip()] if summary.strip() else []
    in_parts = False
    for ln in lines[1:]:
        if re.match(r"(?i)^parts?\s*:\s*$", ln):
            in_parts = True
            continue
        pm = re.match(r"(?i)^parts?\s*:\s*(.*)$", ln)
        if pm:
            out["parts"] = [p.strip() for p in pm.group(1).split(",") if p.strip()]
            in_parts = False
            continue
        b = _BULLET_RE.match(ln)
        if in_parts and b:
            out["parts"].append(b.group(1).strip())
        elif ln:
            bits.append(ln)
            in_parts = False
    out["summary"] = " ".join(bits).strip()
    return out


def inspect_source(source: str) -> list[str]:
    """Cheap static lints BEFORE a compile is attempted — each is one plain sentence the repair
    prompt can hand the coder. Doubles as the safety gate: only the allowlisted imports, no
    forbidden builtin calls, and no top-level geometry (parameter overrides land between exec and
    build(), so work outside build() would silently ignore them)."""
    lints: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"model.py has a syntax error at line {exc.lineno}: {exc.msg}."]
    has_build = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "build":
                has_build = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the docstring
        lints.append(
            f"model.py runs code at the top level (line {node.lineno}) — keep all geometry inside "
            f"build(), so parameter changes take effect."
        )
        break
    if not has_build:
        lints.append("model.py must define a build() function that returns the part (or a dict of "
                     "named parts).")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    lints.append(f"model.py imports '{root}' — a design may import only "
                                 f"{', '.join(sorted(ALLOWED_IMPORTS))}.")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in ALLOWED_IMPORTS:
                lints.append(f"model.py imports from '{node.module or '.'}' — a design may import "
                             f"only {', '.join(sorted(ALLOWED_IMPORTS))}.")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                lints.append(f"model.py calls {fn.id}() — a design computes geometry and may not "
                             f"do I/O or reflection.")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            lints.append("model.py touches a dunder attribute — a design has no business with "
                         "Python internals.")
    if not parse_params(source) and PARAM_START not in source:
        lints.append(f"model.py has no '{PARAM_START}' block — put the adjustable dimensions there "
                     f"so the design stays tunable by voice and by slider.")
    # De-dup while keeping order (an import inside a loop repeats otherwise).
    seen: set[str] = set()
    out = []
    for lint in lints:
        if lint not in seen:
            seen.add(lint)
            out.append(lint)
    return out[:4]


_DETAIL_LIMIT = 900


def friendly_error(err_text: str) -> tuple[str, str]:
    """A raw runner failure (traceback or engine message) → (one warm user sentence, trimmed detail
    for the coder's repair prompt). The sentence never contains paths or Python jargon."""
    text = (err_text or "").strip()
    detail = text[-_DETAIL_LIMIT:] if len(text) > _DETAIL_LIMIT else text
    low = text.lower()
    if not text:
        return "The design couldn't be compiled just now.", ""
    if "timed out" in low or "timeout" in low:
        return ("The design took too long to compute — it's probably heavier than it needs to be.",
                detail)
    if "syntaxerror" in low:
        return "There's a typo in the design's source.", detail
    if "nameerror" in low:
        return "The design refers to a name it never defined.", detail
    if "not_done" in low or "brep" in low or ("boolean" in low and "failed" in low):
        return ("Two shapes in the design couldn't be combined — they probably don't touch, or "
                "touch only on an edge.", detail)
    if "domainerror" in low or "constructionerror" in low or "rangeerror" in low:
        return ("A shape in the design has an impossible dimension — zero or negative where it "
                "can't be.", detail)
    if "zerodivision" in low:
        return "A dimension in the design works out to zero where it can't be.", detail
    if "build() returned" in low or "did not return" in low:
        return "The design's build() didn't hand back a part.", detail
    if "memory" in low:
        return "The design ran out of room to compute — too much detail at once.", detail
    last = text.splitlines()[-1].strip()
    if ":" in last:
        last = last.split(":", 1)[1].strip()
    last = re.sub(r"(?:[A-Za-z]:)?[\\/](?:[^\\/\s'\",]+[\\/])*([^\\/\s'\",]+\.py)", r"\1", last)
    sentence = (last[:140] + "…") if len(last) > 140 else last
    return (f"The design hit a snag while computing: {sentence}" if sentence
            else "The design hit a snag while computing."), detail


# ---------------------------------------------------------------------------------------------
# helix_parts — the library seeded beside every model.py. It is written to the WORKSPACE (so the
# coder can read it and the runner can import it with cwd there); it imports build123d only when
# the runner executes it. Everything is millimetres. Enclosure semantics are INNER dimensions —
# "will my board fit" is the question the user is actually asking.
# ---------------------------------------------------------------------------------------------

_LIB_HEAD = '''"""helix_parts — HELIX's enclosure & hardware library for holograms (build123d inside).

Everything is millimetres. Enclosure sizes are INNER (cavity) dimensions. `from helix_parts import *`
re-exports all of build123d, the board catalog, and the helpers below — model.py needs no other import.
"""
import math
from dataclasses import dataclass, field

from build123d import *  # noqa: F401,F403 — the design language rides on build123d's names
from build123d import (
    Align, Axis, Box, BuildPart, Compound, Cone, Cylinder, Location, Part, Plane, Pos, Rot, Text,
    chamfer, extrude, fillet, mirror,
)

MM = 1.0


# ----- the hardware catalog -----------------------------------------------------------------

@dataclass(frozen=True)
class BoardSpec:
    """A real circuit board: outline, mounting holes (x, y from the board's bottom-left corner,
    board lying flat, connectors noted), and clearances. `approx` marks community-measured parts —
    leave a little extra room around those."""

    name: str
    length: float                  # X
    width: float                   # Y
    holes: tuple = ()              # ((x, y), ...) from bottom-left corner
    hole_d: float = 3.2            # mounting hole diameter
    height: float = 12.0           # tallest component, board included
    usb: str = ""                  # side the main connector leaves: "left"/"right"/"front"/"back"
    approx: bool = False


'''

# The footprints the library carried before the component catalog existed. Any key the catalog
# does not (yet) define is rendered from here, so every key that ever worked keeps working;
# a key the catalog defines is rendered from the catalog — the single source of truth.
# (name, length, width, holes, hole_d, height, usb side, approx)
LEGACY_BOARDS: dict[str, tuple] = {
    "arduino_uno": ("Arduino Uno R3", 68.6, 53.4,
                    ((14.0, 2.5), (66.0, 7.6), (66.0, 35.5), (15.3, 50.6)), 3.2, 15.0, "left", False),
    "arduino_mega": ("Arduino Mega 2560", 101.6, 53.4,
                     ((14.0, 2.5), (66.0, 7.6), (66.0, 35.5), (15.3, 50.6), (96.5, 2.5), (90.2, 50.6)),
                     3.2, 15.0, "left", False),
    "arduino_nano": ("Arduino Nano", 43.2, 17.8,
                     ((1.3, 1.3), (41.9, 1.3), (1.3, 16.5), (41.9, 16.5)), 1.8, 8.0, "left", False),
    "pi_pico": ("Raspberry Pi Pico", 51.0, 21.0,
                ((2.0, 4.8), (2.0, 16.2), (49.0, 4.8), (49.0, 16.2)), 2.1, 5.0, "left", False),
    "pi_4": ("Raspberry Pi 4B", 85.0, 56.0,
             ((3.5, 3.5), (3.5, 52.5), (61.5, 3.5), (61.5, 52.5)), 2.7, 20.0, "right", False),
    "pi_zero": ("Raspberry Pi Zero 2 W", 65.0, 30.0,
                ((3.5, 3.5), (3.5, 26.5), (61.5, 3.5), (61.5, 26.5)), 2.7, 8.0, "front", False),
    # ESP32 DevKitC has NO mounting holes — clamp it with side_rails() instead of standoffs.
    "esp32_devkitc": ("ESP32 DevKitC V4", 48.2, 25.4, (), 0.0, 12.0, "left", False),
    # Community-measured modules — approx: verify against the part in hand, leave 0.5 mm slack.
    "esp8266_nodemcu": ("NodeMCU ESP8266 (Amica)", 48.6, 25.9,
                        ((2.2, 2.2), (46.4, 2.2), (2.2, 23.7), (46.4, 23.7)), 2.5, 12.0, "left", True),
    "wemos_d1_mini": ("Wemos D1 Mini", 34.2, 25.6, (), 0.0, 8.0, "left", True),
    "relay_1ch": ("Relay module, 1 channel", 50.0, 26.0,
                  ((2.75, 2.75), (47.25, 2.75), (2.75, 23.25), (47.25, 23.25)), 3.1, 19.0, "", True),
    "relay_2ch": ("Relay module, 2 channel", 50.5, 38.5,
                  ((2.75, 2.75), (47.75, 2.75), (2.75, 35.75), (47.75, 35.75)), 3.1, 19.0, "", True),
    "relay_4ch": ("Relay module, 4 channel", 75.0, 55.0,
                  ((2.75, 2.75), (72.25, 2.75), (2.75, 52.25), (72.25, 52.25)), 3.1, 19.0, "", True),
    "buck_lm2596": ("LM2596 buck converter", 43.2, 21.3, ((3.0, 6.0), (40.2, 15.3)), 3.1, 14.0, "", True),
    "breadboard_half": ("Half-size breadboard", 83.0, 55.0, (), 0.0, 10.0, "", True),
}

# Port kinds that leave the board and want a wall opening — the first of these on a component is
# the BoardSpec's `usb` side. Internal connectors (JST, headers) are not.
_EXTERNAL_PORT_KINDS = ("usb_c", "micro_usb", "usb_a", "barrel_5_5", "sd", "hdmi", "audio_3_5",
                        "antenna")


def _board_line(key: str, name: str, length: float, width: float, holes, hole_d: float,
                height: float, usb: str, approx: bool) -> str:
    holes_txt = "(" + ", ".join(f"({float(x)!r}, {float(y)!r})" for x, y in holes) + ("," if len(holes) == 1 else "") + ")"
    tail = ", approx=True" if approx else ""
    return (f'    "{key}": BoardSpec({name!r}, {float(length)!r}, {float(width)!r}, {holes_txt}, '
            f'{float(hole_d)!r}, {float(height)!r}, {usb!r}{tail}),')


def render_boards(catalog: dict[str, Component] | None = None) -> str:
    """The `BOARDS` block of helix_parts.py, rendered from the component catalog (every entry,
    keys sorted) UNION the legacy footprints for keys the catalog lacks. Deterministic text: the
    seeded library is compared byte-for-byte with what a workspace already holds. A catalog hole
    becomes (x, y); `hole_d` is the first hole's diameter; the `usb` side is the first external
    port's side; `approx` mirrors the catalog's confidence < 0.7."""
    cat = CATALOG if catalog is None else catalog
    lines = ["BOARDS: dict[str, BoardSpec] = {",
             "    # From HELIX's component catalog (helix/domain/components.py) — the single source of truth."]
    for key in sorted(cat):
        c = cat[key]
        usb = ""
        for p in c.ports:
            if p.kind in _EXTERNAL_PORT_KINDS:
                usb = p.side
                break
        hole_d = float(c.holes[0].d) if c.holes else 0.0
        lines.append(_board_line(key, c.name, c.length, c.width, [(h.x, h.y) for h in c.holes], hole_d,
                                 c.height, usb, c.confidence < 0.7))
    legacy = [k for k in LEGACY_BOARDS if k not in cat]
    if legacy:
        lines.append("    # Footprints the catalog does not carry yet (kept so every known key still works).")
        for key in legacy:
            lines.append(_board_line(key, *LEGACY_BOARDS[key]))
    lines.append("}")
    return "\n".join(lines) + "\n"


_LIB_TAIL = '''
# Heat-set insert pilot holes (diameter to print) and the screw that goes with them.
INSERT_M2 = 3.2
INSERT_M2_5 = 3.6
INSERT_M3 = 4.0
INSERT_M4 = 5.6
# Self-tapping pilot holes straight into printed bosses.
PILOT_M2 = 1.6
PILOT_M2_5 = 2.05
PILOT_M3 = 2.5

FIT = 0.30            # printed clearance for a part that must slide over another (per side)
SNAP_CLEAR = 0.15     # printed clearance for a snap lip


def board(key: str) -> BoardSpec:
    """Look a board up by its catalog key (see BOARDS)."""
    return BOARDS[key]


# ----- primitives ---------------------------------------------------------------------------

def rbox(length: float, width: float, height: float, r: float = 2.0) -> Part:
    """A box with vertical edges rounded — the basic enclosure silhouette. Sits on Z=0, centered
    in X/Y."""
    r = max(0.0, min(r, length / 2 - 0.01, width / 2 - 0.01))
    p = Box(length, width, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if r > 0.05:
        p = fillet(p.edges().filter_by(Axis.Z), r)
    return p


def shell_box(inner_l: float, inner_w: float, inner_h: float, wall: float = 2.0,
              r: float = 3.0, floor: float | None = None) -> Part:
    """An open-top enclosure BODY around an inner cavity of the given size. Outer size grows by the
    wall; the floor defaults to the wall thickness. Sits on Z=0, centered in X/Y."""
    floor_t = wall if floor is None else floor
    outer = rbox(inner_l + 2 * wall, inner_w + 2 * wall, inner_h + floor_t, r + wall)
    cavity = Pos(0, 0, floor_t) * rbox(inner_l, inner_w, inner_h + 1, r)
    return outer - cavity


def lid_for(inner_l: float, inner_w: float, wall: float = 2.0, r: float = 3.0,
            top: float = 2.0, lip_h: float = 3.0) -> Part:
    """A friction-fit lid for shell_box with the SAME inner size and wall: a flat top the size of
    the body's outer face, plus an inner lip that seats into the cavity. Print as-is (top down on
    the plate is how it's built here — flat side on Z=0)."""
    plate = rbox(inner_l + 2 * wall, inner_w + 2 * wall, top, r + wall)
    lip_l = inner_l - 2 * SNAP_CLEAR
    lip_w = inner_w - 2 * SNAP_CLEAR
    lip = Pos(0, 0, top) * (
        rbox(lip_l, lip_w, lip_h, r) - rbox(lip_l - 2 * wall, lip_w - 2 * wall, lip_h + 1, max(r - wall, 0.5))
    )
    return plate + lip


def standoff(height: float, hole_d: float = INSERT_M3, od: float | None = None) -> Part:
    """One mounting boss: a cylinder with a pilot hole (heat-set insert by default). Sits on Z=0."""
    outer_d = od if od is not None else hole_d + 3.2
    post = Cylinder(outer_d / 2, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    bore = Cylinder(hole_d / 2, height + 0.1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return post - bore


def standoffs_for(spec: BoardSpec, height: float = 5.0, hole_d: float | None = None) -> Part:
    """Bosses for every mounting hole of a board, positioned with the board CENTERED on the origin
    (so they drop straight into a shell_box of matching inner size). Add to the body: floor level is
    Z=0 inside the cavity — translate up by the floor if you built the body yourself."""
    if not spec.holes:
        raise ValueError(f"{spec.name} has no mounting holes — clamp it with side_rails() instead")
    if hole_d is not None:
        d = hole_d
    elif spec.hole_d >= 2.8:
        d = PILOT_M3
    elif spec.hole_d >= 2.3:
        d = PILOT_M2_5
    else:
        d = PILOT_M2  # a 2.1–2.2 mm board hole is M2 (Pi camera, Pico); a 1.8 mm hole is M1.6, treated as M2
    posts = [
        Pos(x - spec.length / 2, y - spec.width / 2, 0) * standoff(height, d)
        for (x, y) in spec.holes
    ]
    out = posts[0]
    for p in posts[1:]:
        out += p
    return out


def side_rails(spec: BoardSpec, height: float = 5.0, grip: float = 1.2,
               slot: float | None = None) -> Part:
    """Two rails that clamp a hole-less board (ESP32 DevKitC) by its long edges: each rail carries a
    slot the board's edge slides into. Centered like standoffs_for. `slot` defaults to a snug fit
    over a 1.6 mm PCB."""
    slot_h = slot if slot is not None else 1.9
    rail_w = grip + 2.0
    length = spec.length + 1.0
    y = spec.width / 2 + rail_w / 2 - grip

    def rail(sign: float) -> Part:
        body = Pos(0, sign * y, 0) * Box(length, rail_w, height + slot_h + 1.6,
                                         align=(Align.CENTER, Align.CENTER, Align.MIN))
        cut = Pos(0, sign * (spec.width / 2 - grip / 2), height) * Box(
            length + 1, grip + 0.4, slot_h, align=(Align.CENTER, Align.CENTER, Align.MIN))
        return body - cut

    return rail(1.0) + rail(-1.0)


def vent_slots(span: float, depth: float, rows: int = 3, slot_w: float = 1.6,
               pitch: float | None = None) -> Part:
    """A block of louvre slots to SUBTRACT from a wall or lid: `span` across, `depth` through.
    Returns solid slots centered at the origin lying in the XY plane, `rows` deep in Y — position
    with Pos/Rot onto the face you're cutting."""
    n = max(1, int(span // (slot_w * 3)))
    p = pitch if pitch is not None else slot_w * 2.5
    slots = None
    for i in range(n):
        x = (i - (n - 1) / 2) * (span / n)
        for j in range(max(1, rows)):
            y = (j - (max(1, rows) - 1) / 2) * p
            s = Pos(x, y, 0) * Box(slot_w, slot_w, depth + 1, align=(Align.CENTER, Align.CENTER, Align.CENTER))
            slots = s if slots is None else slots + s
    return slots


def usb_cutout(wall: float, kind: str = "usb_c") -> Part:
    """A connector opening to SUBTRACT from a wall: a rounded slot sized generously for the plug's
    overmold. Returns the cutter centered at origin, running through `wall` in Y — Pos/Rot it onto
    the wall. Kinds: usb_c, micro_usb, usb_a, barrel_5_5, rj45."""
    sizes = {
        "usb_c": (10.0, 4.0, 1.5), "micro_usb": (9.0, 4.5, 1.2), "usb_a": (14.0, 7.0, 1.0),
        "barrel_5_5": (8.5, 8.5, 4.0), "rj45": (16.5, 14.0, 1.0),
    }
    w, h, r = sizes.get(kind, sizes["usb_c"])
    cutter = Box(w, wall + 2, h, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    r = min(r, h / 2 - 0.1, w / 2 - 0.1)
    if r > 0.1:
        cutter = fillet(cutter.edges().filter_by(Axis.Y), r)
    return cutter


def cable_gland_boss(wall: float, thread_d: float = 12.5) -> Part:
    """The hole (and a reinforcing boss) for a PG7-style cable gland: subtract the returned
    `hole`, add the `boss`, via the tuple (boss, hole). Centered at origin through Y."""
    boss = Rot(90, 0, 0) * Cylinder(thread_d / 2 + 2.5, wall + 2,
                                    align=(Align.CENTER, Align.CENTER, Align.CENTER))
    hole = Rot(90, 0, 0) * Cylinder(thread_d / 2, wall + 8,
                                    align=(Align.CENTER, Align.CENTER, Align.CENTER))
    return (boss, hole)


def screw_boss(height: float, insert_d: float = INSERT_M3) -> Part:
    """A corner screw boss for a screw-down lid (heat-set insert size by default). Alias of
    standoff with a chunkier wall."""
    return standoff(height, insert_d, od=insert_d + 4.4)


def lip_ring(inner_l: float, inner_w: float, wall: float = 2.0, r: float = 3.0,
             lip_h: float = 4.0, lip_t: float | None = None, clear: float = SNAP_CLEAR) -> Part:
    """THE JOINT between two shell halves: an L-profile seating lip ADDed to one half's rim that
    inserts into the other half's cavity. Two shell_box halves that merely meet rim-to-rim fall
    apart — every two-half enclosure needs this (plus screws when it is worn or handled).

    Give it the SAME inner_l/inner_w/wall/r as both halves. The L-profile matters: a BASE FLANGE
    overlaps the rim wall top (so the ring prints ON the rim, never floating over the cavity), and
    the inset lip rises from it with the mating clearance; the flange's inboard underside is
    chamfered 45° so it self-supports. Which half gets it: whichever RECEIVING half satisfies
    lip_h <= (receiver depth - receiver wall - 0.5) — usually the lip goes on the deeper half,
    seating into the shallower lid. Position it at the lip half's rim plane, where for a shell_box
    half of height `depth` the rim plane is simply z = depth:

        shell = shell + Pos(0, 0, depth - 0.01) * lip_ring(inner_l, inner_w, wall, r)
    """
    t = lip_t if lip_t is not None else max(1.2, wall * 0.6)
    base_h = 2.0
    over = max(0.8, wall - 1.0)                    # how far the flange overlaps the rim wall top
    ol, ow = inner_l - 2 * clear, inner_w - 2 * clear
    # Corner radii follow the CAVITY they nest into: `r` is the cavity's radius, so the lip's
    # outline is that radius less the clearance and its bore that less the lip thickness. (Cutting
    # them from r - wall, as the first version did, left the lip's corners ~0.6 mm proud of the
    # receiving cavity at every corner — an assembled boolean intersection showed four slivers.)
    r_lip = max(r - clear, 0.5)
    r_bore = max(r - clear - t, 0.5)
    # Base flange: from the rim-wall overlap down to the lip's inner bore — SITS ON THE RIM.
    base = rbox(inner_l + 2 * over, inner_w + 2 * over, base_h, r + over)
    base = base - (Pos(0, 0, -1) * rbox(ol - 2 * t, ow - 2 * t, base_h + 2, r_bore))
    # The rising lip that seats into the other cavity.
    lip = rbox(ol, ow, base_h + lip_h, r_lip)
    lip = lip - (Pos(0, 0, -1) * rbox(ol - 2 * t, ow - 2 * t, base_h + lip_h + 2, r_bore))
    ring = base + lip
    # Chamfer the bottom edges so the flange underside self-supports at ~45°. The size must cover
    # the FULL inboard overhang (t + clear — the part hanging over the cavity void), capped by the
    # base height and never allowed to fail-and-vanish (a swallowed failure here once shipped a
    # 5.8 cm² floating underside; an undersized chamfer shipped a 2.6 cm² ring).
    # A single try/except here once swallowed a failing chamfer and shipped the flat underside it
    # exists to prevent — so this is a RETRY LADDER: shrink until the kernel accepts one. The last
    # rungs are small enough to succeed on any corner combination this ring can produce.
    # Only the INBOARD loop (the bore's bottom edges, over the cavity void) is chamfered: the
    # outboard loop sits on the rim wall and needs no chamfer — and chamfering both loops made
    # them collide on a 2.35 mm flange, so the ladder fell to 0.9 mm and left a 0.4 mm-wide
    # flat over the void all the way round (1.8 cm² on a 120 × 80 shell, most of the runner's
    # overhang budget). One loop, the full overhang: the residue is a 0.05 mm sliver.
    want = max(0.3, min(t + clear - 0.05, base_h - 0.05))
    hx, hy = (ol - 2 * t) / 2 + 0.05, (ow - 2 * t) / 2 + 0.05
    for cham in (want, want * 0.7, want * 0.5, 0.45, 0.3):
        try:
            bottom = ring.edges().group_by(Axis.Z)[0]
            inboard = [e for e in bottom if abs(e.center().X) <= hx and abs(e.center().Y) <= hy]
            ring = chamfer(inboard or bottom, cham)
            break
        except Exception:  # noqa: BLE001 — try the next rung
            continue
    return ring


def lip_rebate(inner_l: float, inner_w: float, wall: float = 2.0, r: float = 3.0,
               clear: float = SNAP_CLEAR) -> Part:
    """The RECEIVING half's counterpart to lip_ring: a rebate (rabbet) CUTTER that widens the
    cavity mouth so the lip's base flange nests INSIDE the rim and the two rims still meet flush
    outside it. Without this, the flange lands ON the receiving rim and holds the halves ~2 mm
    apart (measured by assembled boolean intersection — that is how this helper was born).
    Subtract at the receiving half's rim, matching dims:

        lid = lid - (Pos(0, 0, depth - 2.2) * lip_rebate(inner_l, inner_w, wall, r))
    """
    over = max(0.8, wall - 1.0)
    depth_cut = 2.0 + clear + 0.05                 # the flange's base_h plus seating clearance
    ol, ow = inner_l + 2 * (over + clear), inner_w + 2 * (over + clear)
    ring = rbox(ol, ow, depth_cut + 2, r + over + clear)
    bore = Pos(0, 0, -1) * rbox(inner_l - 2, inner_w - 2, depth_cut + 4, max(r - 1.0, 0.5))
    return ring - bore


def csk_hole(screw_d: float = 3.4, head_d: float = 6.3, depth: float = 12.0) -> Part:
    """A countersunk through-hole CUTTER for a COUNTERSUNK (DIN 965) screw entering through the
    printed plate face: subtract it at the hole position, cone opening toward Z=0 (the outside
    face), head flush. THE PAIRING (defaults match screw_boss's INSERT_M3 default): M3 → clearance
    3.4, csk head 6.3. Other sizes: M2 → csk_hole(2.4, 4.4); M2.5 → csk_hole(2.9, 5.5). Specify
    countersunk-head screws in the assembly note — a pan head stands proud of the face."""
    shank = Pos(0, 0, -1) * Cylinder(screw_d / 2, depth + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    csk_h = (head_d - screw_d) / 2  # a 90-degree countersink
    cone = Pos(0, 0, -0.01) * Cone(head_d / 2, screw_d / 2, csk_h + 0.01,
                                   align=(Align.CENTER, Align.CENTER, Align.MIN))
    return shank + cone


def strap_tab(slot_w: float, slot_h: float = 6.0, thickness: float = 3.0,
              margin: float = 4.0) -> Part:
    """A PRINTABLE strap/band anchor: a flat tab with the slot cut through it, lying in the XY
    plane on Z=0 — thread the elastic through the slot. Use this instead of a ring protruding off
    a face (a protruding ring floats the part on its rim and the slicer refuses it). OVERLAP the
    tab 5+ mm INTO the body (never merely touching the edge face — that is the coincident-face
    union the boolean rule forbids) and keep the slot fully outside the wall."""
    tab = rbox(slot_w + 2 * margin, slot_h + 2 * margin, thickness, r=margin * 0.8)
    slot = Pos(0, 0, -1) * rbox(slot_w, slot_h, thickness + 2, r=min(2.0, slot_h / 2 - 0.1))
    return tab - slot


def arrange(*parts: Part, gap: float = 8.0) -> Compound:
    """Lay parts side by side along X (each sitting on Z=0) — the print-plate layout for a
    multi-part design. Use as the final return: `return arrange(body, lid)` — or return the dict
    {"body": body, "lid": lid} and HELIX arranges them itself."""
    placed = []
    x = 0.0
    for p in parts:
        bb = p.bounding_box()
        placed.append(Pos(x - bb.min.X, 0, -bb.min.Z) * p)
        x += bb.size.X + gap
    total = sum((p.bounding_box().size.X for p in placed), 0.0)
    return Compound(children=[Pos(-total / 2 + 0, 0, 0) * p for p in placed])


# ----- pockets, bays, and the cutters an enclosure around real parts needs ------------------
# Conventions: ADDED features sit on Z=0 (place them with Pos(x, y, floor - 0.01)). CUTTERS span
# from 1 mm BELOW Z=0 up to `depth` (so Pos(x, y, 0) at a plate face cuts clean through when
# depth = plate + 1) — except wire_notch, which starts AT Z=0 so the floor under it survives.

_C3 = (Align.CENTER, Align.CENTER, Align.CENTER)
_CM = (Align.CENTER, Align.CENTER, Align.MIN)
_SIDE = {"left": (-1, 0), "right": (1, 0), "front": (0, -1), "back": (0, 1)}


def pocket(l: float, w: float, h: float, rib: float = 1.6, clear: float = FIT, omit: str = "") -> Part:
    """A rib-walled POCKET a part drops into — ADD it to a floor. `l` × `w` is the PART; the
    inside is the part + 2*clear, the ribs are `rib` thick outside that and `h` tall, with no
    floor of its own (the enclosure's floor is the floor). Centered, sits on Z=0. `omit` drops one
    rib ("left"/"right"/"front"/"back" = -x/+x/-y/+y) so the pocket can back onto a wall or open
    toward a connector; the corner stubs stay."""
    il, iw = l + 2 * clear, w + 2 * clear
    # every cut runs from 1 below Z=0 to 1 above the rib top — a centred cutter of height h + 2
    # would leave a solid cap over any pocket taller than 2 mm (measured: 42 cm² of ceilings)
    ring = Box(il + 2 * rib, iw + 2 * rib, h, align=_CM) - Pos(0, 0, -1) * Box(il, iw, h + 2, align=_CM)
    dx, dy = _SIDE.get(omit, (0, 0))
    if dx:
        ring = ring - Pos(dx * (il / 2 + rib / 2), 0, -1) * Box(rib + 1, iw, h + 2, align=_CM)
    elif dy:
        ring = ring - Pos(0, dy * (iw / 2 + rib / 2), -1) * Box(il, rib + 1, h + 2, align=_CM)
    return ring


def pocket_for(spec, h: float | None = None, rib: float = 1.6, clear: float = FIT, omit: str = "") -> Part:
    """pocket() sized from the catalog: a board key, a BoardSpec, or a (length, width[, height])
    tuple. The rib height defaults to the part's height minus 1, kept between 3 and 6 mm; approx
    parts get 0.5 mm more room per side."""
    if isinstance(spec, str):
        spec = BOARDS[spec]
    if isinstance(spec, BoardSpec):
        l, w, ph, extra = spec.length, spec.width, spec.height, (0.5 if spec.approx else 0.0)
    else:
        dims = tuple(spec)
        l, w = float(dims[0]), float(dims[1])
        ph, extra = (float(dims[2]) if len(dims) > 2 else 6.0), 0.0
    height = h if h is not None else max(3.0, min(6.0, ph - 1.0))
    return pocket(l, w, height, rib, clear + extra, omit)


def battery_bay(l: float, w: float, h: float, rib: float = 1.6, clear: float = 0.6,
                lead: float = 8.0, side: str = "right") -> Part:
    """A pocket for a pouch cell / battery holder with a `lead`-wide gap in the `side` rib for the
    leads (left/right/front/back = -x/+x/-y/+y). Centered, sits on Z=0; ADD it to a floor."""
    bay = pocket(l, w, h, rib, clear)
    il, iw = l + 2 * clear, w + 2 * clear
    dx, dy = _SIDE.get(side, (1, 0))
    if dx:
        gap = Pos(dx * (il / 2 + rib / 2), 0, -1) * Box(rib + 1, min(lead, iw - 2), h + 2, align=_CM)
    else:
        gap = Pos(0, dy * (iw / 2 + rib / 2), -1) * Box(min(lead, il - 2), rib + 1, h + 2, align=_CM)
    return bay - gap


def lens_bore(d: float, depth: float, recess_d: float = 0.0, recess_h: float = 1.0) -> Part:
    """CUTTER for a lens looking out through a plate face: a Ø d bore from 1 mm below Z=0 up to
    `depth` (plate + 1 goes through), plus an optional shallow recess Ø recess_d, recess_h deep, at
    the outside face so the lens sits back from the surface. Subtract at Pos(x, y, 0)."""
    bore = Pos(0, 0, -1) * Cylinder(d / 2, depth + 1, align=_CM)
    if recess_d > d:
        bore = bore + Pos(0, 0, -1) * Cylinder(recess_d / 2, recess_h + 1, align=_CM)
    return bore


def grille(d: float, hole: float = 1.6, pitch: float = 2.8, depth: float = 6.0) -> Part:
    """CUTTER: a hex-packed field of Ø hole holes filling a Ø d circle — a speaker grille through a
    plate face. One shape to subtract, spanning 1 mm below Z=0 up to `depth`."""
    r_max = d / 2 - hole / 2 - 0.2
    row = pitch * math.sqrt(3) / 2
    n = int(d / min(row, pitch)) + 2
    holes = []
    for j in range(-n, n + 1):
        y = j * row
        xoff = pitch / 2 if j % 2 else 0.0
        for i in range(-n, n + 1):
            x = i * pitch + xoff
            if x * x + y * y <= r_max * r_max:
                holes.append(Pos(x, y, -1) * Cylinder(hole / 2, depth + 1, align=_CM))
    if not holes:
        return Pos(0, 0, -1) * Cylinder(max(hole, min(d, 2.0)) / 2, depth + 1, align=_CM)
    return Compound(children=holes)


def mic_hole(d: float = 1.5, depth: float = 6.0) -> Part:
    """CUTTER: one small hole for a MEMS microphone's port, 1 mm below Z=0 up to `depth`."""
    return Pos(0, 0, -1) * Cylinder(d / 2, depth + 1, align=_CM)


def led_window(d: float, depth: float = 6.0) -> Part:
    """CUTTER: a round window for an LED (or any round hole), 1 mm below Z=0 up to `depth`."""
    return Pos(0, 0, -1) * Cylinder(d / 2, depth + 1, align=_CM)


def screen_window(w: float, h: float, r: float = 1.0, depth: float = 6.0) -> Part:
    """CUTTER: a rounded rectangular window (a display's active area + clearance), centered,
    1 mm below Z=0 up to `depth`."""
    win = Pos(0, 0, -1) * Box(w, h, depth + 1, align=_CM)
    r = min(r, w / 2 - 0.1, h / 2 - 0.1)
    if r > 0.05:
        win = fillet(win.edges().filter_by(Axis.Z), r)
    return win


# Switch cutouts, mm: kind -> (shape, along, tall). The long side runs along X — Rot it onto a wall.
SWITCH_SLOTS = {
    "ss12d00": ("slot", 8.5, 3.6),      # SS12D00 slide switch — the actuator's travel slot
    "kcd1": ("rect", 19.2, 13.5),       # KCD1 rocker — the panel cutout it snaps into
    "tact_6": ("round", 6.5, 6.5),      # 6x6 tactile — a plunger / keycap hole
    "push_12": ("round", 12.4, 12.4),   # 12 mm latching push button — its threaded bushing
    "ky040": ("round", 7.2, 7.2),       # KY-040 rotary encoder — the shaft's bushing
}


def switch_slot(kind: str, depth: float = 6.0) -> Part:
    """CUTTER for a switch's actuator or bushing through a plate or wall (see SWITCH_SLOTS; the
    long side along X — Rot(90, 0, 0) it onto a wall). 1 mm below Z=0 up to `depth`."""
    shape, w, h = SWITCH_SLOTS[kind]
    if shape == "round":
        return Pos(0, 0, -1) * Cylinder(w / 2, depth + 1, align=_CM)
    return screen_window(w, h, (h / 2 - 0.05) if shape == "slot" else 1.0, depth)


def port_slot(w: float, h: float, wall: float, r: float = 1.5) -> Part:
    """CUTTER: a rounded opening `w` wide × `h` tall running through a wall `wall` thick in Y
    (overshooting 1 mm each side), centered at the origin — Pos/Rot it onto the wall exactly like
    usb_cutout. usb_cutout knows the plug kinds; this one takes any size."""
    cutter = Box(w, wall + 2, h, align=_C3)
    r = min(r, h / 2 - 0.1, w / 2 - 0.1)
    if r > 0.1:
        cutter = fillet(cutter.edges().filter_by(Axis.Y), r)
    return cutter


def wire_notch(w: float, depth: float) -> Part:
    """CUTTER through a pocket rib so a wire drops into the trench: `w` wide along X, crossing 4 mm
    in Y (any rib up to 3 thick), from Z=0 — the floor under it survives — up `depth` + 1. Position
    it at the rib's centreline at floor height: Pos(x, y, floor) * wire_notch(4, pocket_h);
    Rot(0, 0, 90) for a rib that runs along Y."""
    return Box(w, 4.0, depth + 1, align=_CM)


def deboss_tag(text: str, size: float, depth: float) -> Part:
    """The fallback for deboss_text when no font can be loaded: a shallow rectangular tag as big as
    the text would be, so a label still marks the pocket and the build never fails on a font."""
    n = max(1, len(text))
    return Box(0.62 * size * n, 0.78 * size, depth + 0.2, align=_CM)


def deboss_text(text: str, size: float, depth: float, font: str = "Arial") -> Part:
    """CUTTER for a debossed label: the text extruded from Z=0 up depth + 0.2, centered — subtract
    at Pos(x, y, face_z - depth). Inner-face labels read as written; a label on the PLATE face reads
    from outside only when mirrored: mirror(deboss_text(...), about=Plane.YZ). Degrades to
    deboss_tag() if the font fails, never an error."""
    try:
        solid = extrude(Text(text, font_size=size, font=font), amount=depth + 0.2)
        if solid is None or not solid.solids():
            raise ValueError("no glyphs")
        return solid
    except Exception:  # noqa: BLE001 — a missing font must never fail a build
        return deboss_tag(text, size, depth)
'''

HELIX_LIB = _LIB_HEAD + render_boards(CATALOG) + _LIB_TAIL


def _board_key_lines(catalog: dict[str, Component] | None = None, width: int = 96) -> str:
    """The catalog's keys grouped by category, wrapped, for the coder's cheat-sheet."""
    cat = CATALOG if catalog is None else catalog
    groups: dict[str, list[str]] = {}
    for key in sorted(cat):
        groups.setdefault(cat[key].category, []).append(key)
    legacy = [k for k in LEGACY_BOARDS if k not in cat]
    if legacy:
        groups["legacy"] = legacy
    out: list[str] = []
    for cat_name, keys in groups.items():
        line = f"  {cat_name}: "
        for key in keys:
            if len(line) + len(key) + 2 > width:
                out.append(line.rstrip())
                line = "    " + key + ", "
            else:
                line += key + ", "
        out.append(line.rstrip().rstrip(","))
    return "\n".join(out)


_DOC_HEAD = """\
helix_parts cheat-sheet (the ONLY library; `from helix_parts import *` gives you build123d too):

BOARDS — real footprints, mm, rendered from HELIX's component catalog: board(key) ->
  BoardSpec(.name .length .width .holes .hole_d .height .usb .approx). approx=True: leave ~0.5 mm
  slack. Hole-less boards (esp32_devkitc, wemos_d1_mini…): side_rails, never standoffs. Keys:
"""

_DOC_TAIL = """
Fits: FIT=0.30 slide clearance, SNAP_CLEAR=0.15; inserts INSERT_M3=4.0 (M2 3.2, M2.5 3.6, M4 5.6),
  pilots PILOT_M3=2.5, PILOT_M2_5=2.05, PILOT_M2=1.6 (standoffs_for picks by hole size).
Helpers (all sit on Z=0, centered X/Y; enclosure sizes are INNER/cavity mm):
  rbox(l,w,h,r)                       rounded box
  shell_box(l,w,h,wall,r,floor)      open-top body around an inner cavity
  lid_for(l,w,wall,r,top,lip_h)      friction lid matching that body (same l,w,wall)
  standoff(h,hole_d,od)              one boss; standoffs_for(board(k),h) bosses for every hole,
                                     board centered — Pos(0,0,floor)* to sit on the cavity floor
  side_rails(board(k),h)             edge clamp for hole-less boards (ESP32 DevKitC)
  pocket(l,w,h,rib,clear,omit)       rib-walled pocket a PART l×w drops into (inside = part + 2*clear,
                                     no floor of its own) — ADD at Pos(x,y,floor-0.01); omit="left"/
                                     "right"/"front"/"back" drops one rib (against a wall)
  pocket_for(key|BoardSpec|(l,w,h),h)  pocket sized from the catalog (approx parts get +0.5)
  battery_bay(l,w,h,rib,clear,lead,side)  pocket with a lead gap in one rib
  vent_slots(span,depth,rows)        louvres to SUBTRACT (Rot/Pos onto the face)
  usb_cutout(wall,kind)              subtract; kinds usb_c, micro_usb, usb_a, barrel_5_5, rj45
  port_slot(w,h,wall,r)              any-size wall opening through `wall` in Y (Pos/Rot like usb_cutout)
  cable_gland_boss(wall,thread_d)    -> (boss, hole): add boss, subtract hole
  screw_boss(h,insert_d)             lid screw boss / screw TOWER (full-height for two-half shells)
  lip_ring(l,w,wall,r,lip_h,clear)   THE joint between two shell halves — an L-profile that SITS ON
                                     one rim and seats into the other cavity (same l/w/wall/r);
                                     lip_h <= receiver depth - receiver wall - 0.5. ALWAYS pair with
                                     lip_rebate() cut into the RECEIVING rim, or the flange holds
                                     the halves apart:
  lip_rebate(l,w,wall,r,clear)       subtract at the receiving rim: Pos(0,0,depth-2.2)*lip_rebate(...)
  csk_hole(screw_d,head_d,depth)     countersunk (DIN 965) through-hole cutter, cone toward the
                                     plate face. PAIRS: M3 defaults ↔ screw_boss INSERT_M3 default;
                                     M2 = csk_hole(2.4, 4.4); M2.5 = csk_hole(2.9, 5.5)
  strap_tab(slot_w,slot_h,t,margin)  flat slotted band anchor ON the plate — never a protruding ring
  arrange(*parts,gap)                print-plate layout, or return {"body": b, "lid": l}
Plate-face CUTTERS (subtract at Pos(x,y,0); each spans 1 mm below Z=0 up to `depth` — use
  depth = plate + 1 to go through; on a face-down front shell the plate face IS Z=0):
  lens_bore(d,depth,recess_d,recess_h)  lens hole + shallow recess (the lens sits back from the face)
  grille(d,hole=1.6,pitch=2.8,depth) hex field of holes filling Ø d — a speaker grille
  mic_hole(d=1.5,depth)  led_window(d,depth)  screen_window(w,h,r,depth)
  switch_slot(kind,depth)            ss12d00 8.5×3.6 | kcd1 19.2×13.5 | tact_6 Ø6.5 | push_12 Ø12.4 |
                                     ky040 Ø7.2 (long side along X; Rot(90,0,0) onto a wall)
  wire_notch(w,depth)                notch through a pocket rib, floor kept: Pos(x,y,floor)*;
                                     Rot(0,0,90) for a rib running along Y
  deboss_text(text,size,depth)       label cutter from Z=0 up depth+0.2: Pos(x,y,face_z-depth)*;
                                     mirror(..., about=Plane.YZ) on a plate face so it reads from
                                     outside; degrades to deboss_tag (a plain recess) if no font loads
build123d in 6 lines (algebra mode): parts combine with + - &; move with Pos(x,y,z)*p and
  Rot(x,y,z)*p; primitives Box(l,w,h,align=...), Cylinder(r,h); round with
  fillet(p.edges().filter_by(Axis.Z), r) and chamfer(p.edges(), c); sketch+extrude for profiles:
  extrude(Plane.XY * RectangleRounded(w,h,r), amount). Booleans need real overlap — never touch
  shapes only on a face/edge; sink one 0.01 into the other.
"""

HELIX_LIB_DOC = _DOC_HEAD + _board_key_lines(CATALOG) + "\n" + _DOC_TAIL
