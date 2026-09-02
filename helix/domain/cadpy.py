"""OpenSCAD's successor: the hologram design language is now PYTHON (build123d), as data + pure rules.

A hologram is still a PROGRAM the coder writes — that idea survives the engine swap untouched. What
changed is the language: `model.py` (build123d, a real B-rep CAD kernel — fillets, chamfers, STEP
export) instead of `model.scad` (a mesh kernel with a homemade dialect). LLMs write Python far more
accurately than OpenSCAD, and the parts here are what "make a case for an ESP32" needs to come out
*fitting*: a curated hardware catalog with real dimensions and an enclosure helper library.

This module is PURE domain (stdlib only — no build123d import, no I/O, no subprocess):

  - `HELIX_LIB` / `HELIX_LIB_FILE`  — the helix_parts.py helper library seeded beside model.py. It
    imports build123d *at compile time in the runner subprocess*, never here.
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

HELIX_LIB = '''"""helix_parts — HELIX's enclosure & hardware library for holograms (build123d inside).

Everything is millimetres. Enclosure sizes are INNER (cavity) dimensions. `from helix_parts import *`
re-exports all of build123d, the board catalog, and the helpers below — model.py needs no other import.
"""
from dataclasses import dataclass, field

from build123d import *  # noqa: F401,F403 — the design language rides on build123d's names
from build123d import (
    Align, Axis, Box, BuildPart, Compound, Cylinder, Location, Part, Plane, Pos, Rot,
    chamfer, extrude, fillet,
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


BOARDS: dict[str, BoardSpec] = {
    # Canonical footprints (official drawings)
    "arduino_uno": BoardSpec("Arduino Uno R3", 68.6, 53.4,
                             ((14.0, 2.5), (66.0, 7.6), (66.0, 35.5), (15.3, 50.6)), 3.2, 15, "left"),
    "arduino_mega": BoardSpec("Arduino Mega 2560", 101.6, 53.4,
                              ((14.0, 2.5), (66.0, 7.6), (66.0, 35.5), (15.3, 50.6),
                               (96.5, 2.5), (90.2, 50.6)), 3.2, 15, "left"),
    "arduino_nano": BoardSpec("Arduino Nano", 43.2, 17.8,
                              ((1.3, 1.3), (41.9, 1.3), (1.3, 16.5), (41.9, 16.5)), 1.8, 8, "left"),
    "pi_pico": BoardSpec("Raspberry Pi Pico", 51.0, 21.0,
                         ((2.0, 4.8), (2.0, 16.2), (49.0, 4.8), (49.0, 16.2)), 2.1, 5, "left"),
    "pi_4": BoardSpec("Raspberry Pi 4B", 85.0, 56.0,
                      ((3.5, 3.5), (3.5, 52.5), (61.5, 3.5), (61.5, 52.5)), 2.7, 20, "right"),
    "pi_zero": BoardSpec("Raspberry Pi Zero 2 W", 65.0, 30.0,
                         ((3.5, 3.5), (3.5, 26.5), (61.5, 3.5), (61.5, 26.5)), 2.7, 8, "front"),
    # ESP32 DevKitC has NO mounting holes — clamp it with side_rails() instead of standoffs.
    "esp32_devkitc": BoardSpec("ESP32 DevKitC V4", 48.2, 25.4, (), 0.0, 12, "left"),
    # Community-measured modules — approx: verify against the part in hand, leave 0.5 mm slack.
    "esp8266_nodemcu": BoardSpec("NodeMCU ESP8266 (Amica)", 48.6, 25.9,
                                 ((2.2, 2.2), (46.4, 2.2), (2.2, 23.7), (46.4, 23.7)), 2.5, 12,
                                 "left", approx=True),
    "wemos_d1_mini": BoardSpec("Wemos D1 Mini", 34.2, 25.6, (), 0.0, 8, "left", approx=True),
    "relay_1ch": BoardSpec("Relay module, 1 channel", 50.0, 26.0,
                           ((2.75, 2.75), (47.25, 2.75), (2.75, 23.25), (47.25, 23.25)), 3.1, 19,
                           "", approx=True),
    "relay_2ch": BoardSpec("Relay module, 2 channel", 50.5, 38.5,
                           ((2.75, 2.75), (47.75, 2.75), (2.75, 35.75), (47.75, 35.75)), 3.1, 19,
                           "", approx=True),
    "relay_4ch": BoardSpec("Relay module, 4 channel", 75.0, 55.0,
                           ((2.75, 2.75), (72.25, 2.75), (2.75, 52.25), (72.25, 52.25)), 3.1, 19,
                           "", approx=True),
    "buck_lm2596": BoardSpec("LM2596 buck converter", 43.2, 21.3,
                             ((3.0, 6.0), (40.2, 15.3)), 3.1, 14, "", approx=True),
    "breadboard_half": BoardSpec("Half-size breadboard", 83.0, 55.0, (), 0.0, 10, "", approx=True),
}

# Heat-set insert pilot holes (diameter to print) and the screw that goes with them.
INSERT_M2 = 3.2
INSERT_M2_5 = 3.6
INSERT_M3 = 4.0
INSERT_M4 = 5.6
# Self-tapping pilot holes straight into printed bosses.
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
    d = hole_d if hole_d is not None else (PILOT_M3 if spec.hole_d >= 2.8 else PILOT_M2_5)
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
'''


HELIX_LIB_DOC = """\
helix_parts cheat-sheet (the ONLY library; `from helix_parts import *` gives you build123d too):

BOARDS — real footprints, mm: arduino_uno, arduino_mega, arduino_nano, pi_pico, pi_4, pi_zero,
  esp32_devkitc (NO holes — use side_rails), esp8266_nodemcu, wemos_d1_mini, relay_1ch/2ch/4ch,
  buck_lm2596, breadboard_half. board(key) -> BoardSpec(.length .width .holes .hole_d .height .usb
  .approx). approx=True boards: leave ~0.5 mm slack.
Fits: FIT=0.30 slide clearance, SNAP_CLEAR=0.15; inserts INSERT_M3=4.0 (M2 3.2, M2.5 3.6, M4 5.6),
  pilots PILOT_M3=2.5.
Helpers (all sit on Z=0, centered X/Y; enclosure sizes are INNER/cavity mm):
  rbox(l,w,h,r)                       rounded box
  shell_box(l,w,h,wall,r,floor)      open-top body around an inner cavity
  lid_for(l,w,wall,r,top,lip_h)      friction lid matching that body (same l,w,wall)
  standoff(h,hole_d,od)              one boss; standoffs_for(board(k),h) bosses for every hole,
                                     board centered — Pos(0,0,floor)* to sit on the cavity floor
  side_rails(board(k),h)             edge clamp for hole-less boards (ESP32 DevKitC)
  vent_slots(span,depth,rows)        louvres to SUBTRACT (Rot/Pos onto the face)
  usb_cutout(wall,kind)              subtract; kinds usb_c, micro_usb, usb_a, barrel_5_5, rj45
  cable_gland_boss(wall,thread_d)    -> (boss, hole): add boss, subtract hole
  screw_boss(h,insert_d)             lid screw boss
  arrange(*parts,gap)                print-plate layout, or return {"body": b, "lid": l}
build123d in 6 lines (algebra mode): parts combine with + - &; move with Pos(x,y,z)*p and
  Rot(x,y,z)*p; primitives Box(l,w,h,align=...), Cylinder(r,h); round with
  fillet(p.edges().filter_by(Axis.Z), r) and chamfer(p.edges(), c); sketch+extrude for profiles:
  extrude(Plane.XY * RectangleRounded(w,h,r), amount). Booleans need real overlap — never touch
  shapes only on a face/edge; sink one 0.01 into the other.
"""
