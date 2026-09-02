"""OpenSCAD, as data — the helper library a hologram may `use`, and the pure readers of its source.

A hologram is a PROGRAM: the coder writes model.scad (millimetres, a customizer parameter block at the
top, named modules per part, a design-brief header), and HELIX compiles it. That makes verbal design
accurate — "make it 100 wide" is an edit to a named parameter, not a regeneration — but it also means
HELIX has to READ the source: the viewer's parameter panel, the brief the vision critic judges against,
the compiler's complaints turned into a repair prompt, and the cheap lints that catch a broken file
before a compile is even attempted. All of that reading lives here, in the domain, with nothing but the
standard library: no subprocess, no Qt, no filesystem. The engine that actually runs the compiler is an
adapter; this module is what every layer above it shares.

HELIX_LIB is the one library a model may use. It is plain OpenSCAD 2021.01 — no BOSL2, no `roof`, no
nightly-only features — because the engine HELIX installs for the user is the stable release, and a
helper that only works on a nightly would turn "design me a bracket" into a compiler error on the very
machine HELIX set up. It is embedded as a string the way render_kit.py embeds its JS: the frozen build
has nothing extra to package, and the baker copies it beside model.scad as helix.scad.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------------------------------
# The HELIX helper library.
# ---------------------------------------------------------------------------------------------------

HELIX_LIB_FILE = "helix.scad"

# Design notes the helpers share (the coder reads them in HELIX_LIB_DOC; they are repeated here so the
# library is self-explaining when a person opens helix.scad):
#   - Every SOLID is centred on X and Y and stands on Z=0; center=true centres it vertically too. Parts
#     designed by voice are things that sit on a table or a wall — a base on the ground plane is what
#     "100 wide, 5 thick" means to a person.
#   - Every HOLE TOOL runs along Z from just below 0 to just above h with its head (countersink,
#     counterbore) at the TOP, z=h — so `difference(){ plate; hole }` on a plate standing on Z=0 with its
#     top at z=h cuts cleanly through. The overshoot (helix_eps) is why: a cutter ending exactly on a face
#     leaves CGAL a zero-thickness skin, which shows up as a hole that is "there" but not open.
#   - Curves follow $fa/$fs, never $fn, so helix_quality() controls smoothness everywhere at once and a
#     compile stays fast (a top-level $fn=200 makes a plate with twenty holes take minutes under CGAL).
#   - No top-level variables: a `use`d file's top-level assignments are not reliably visible to its own
#     modules across OpenSCAD versions, so constants are functions (helix_eps()).
HELIX_LIB = r"""// helix.scad — HELIX's helper library for parts designed by voice.
// Millimetres. Plain OpenSCAD 2021.01 (no BOSL2, nothing nightly-only).
//   use <helix.scad>;
// Solids are centred on X and Y and stand on Z=0 (center=true centres them vertically). Hole tools run
// along Z from just below 0 to just above h with their head at the top (z=h) — subtract them from a part
// whose top face is at z=h. Curves follow $fa/$fs (never a global $fn), so helix_quality() sets smoothness.

// helix_eps() — 0.01 mm: the overshoot hole tools use so a cut never leaves a zero-thickness skin.
function helix_eps() = 0.01;

// helix_quality(level) — wrap the whole model: helix_quality("normal") { ... }. "draft" is fast and
// coarse, "normal" is the default, "fine" is smooth (slower). Sets $fa/$fs for everything inside.
module helix_quality(level = "normal") {
    $fa = level == "fine" ? 2 : level == "draft" ? 12 : 6;
    $fs = level == "fine" ? 0.2 : level == "draft" ? 1.5 : 0.5;
    children();
}

// m_size(m) — the numeric size of an M-thread given as 4 or "M4" (unknown → 3).
function m_size(m) = is_num(m) ? m
    : m == "M2" ? 2 : m == "M2.5" ? 2.5 : m == "M3" ? 3 : m == "M4" ? 4 : m == "M5" ? 5
    : m == "M6" ? 6 : m == "M8" ? 8 : m == "M10" ? 10 : 3;

// m_clearance(m) — clearance hole diameter for an M-size bolt (M3 → 3.4, M4 → 4.5, M5 → 5.5, M6 → 6.6).
function m_clearance(m) = lookup(m_size(m),
    [[2, 2.4], [2.5, 2.9], [3, 3.4], [4, 4.5], [5, 5.5], [6, 6.6], [8, 9.0], [10, 11.0]]);

// m_tap(m) — tap-drill diameter for threading an M-size hole (M3 → 2.5, M4 → 3.3, M5 → 4.2, M6 → 5.0).
function m_tap(m) = lookup(m_size(m),
    [[2, 1.6], [2.5, 2.05], [3, 2.5], [4, 3.3], [5, 4.2], [6, 5.0], [8, 6.8], [10, 8.5]]);

// m_head_d(m) — socket-cap (DIN 912) head diameter for an M-size, for counterbores (M3 → 5.5, M4 → 7).
function m_head_d(m) = lookup(m_size(m),
    [[2, 3.8], [2.5, 4.5], [3, 5.5], [4, 7.0], [5, 8.5], [6, 10.0], [8, 13.0], [10, 16.0]]);

// m_csk_d(m) — flat countersunk (DIN 7991, 90°) head diameter for an M-size (M3 → 6, M4 → 8, M5 → 10).
function m_csk_d(m) = lookup(m_size(m),
    [[2, 3.8], [2.5, 4.7], [3, 6.0], [4, 8.0], [5, 10.0], [6, 12.0], [8, 16.0], [10, 20.0]]);

// m_nut_af(m) — hex nut width across flats for an M-size, for hex_pocket (M3 → 5.5, M4 → 7, M5 → 8).
function m_nut_af(m) = lookup(m_size(m),
    [[2, 4.0], [2.5, 5.0], [3, 5.5], [4, 7.0], [5, 8.0], [6, 10.0], [8, 13.0], [10, 16.0]]);

// rounded_box(size, r) — a box [x, y, z] with every edge and corner rounded by r (a hull of spheres).
module rounded_box(size = [40, 30, 20], r = 3, center = false) {
    s = is_list(size) ? size : [size, size, size];
    rr = min(r, min(s) / 2 - helix_eps());
    translate([0, 0, center ? 0 : s[2] / 2])
        if (rr <= 0) cube(s, center = true);
        else hull() for (x = [-1, 1], y = [-1, 1], z = [-1, 1])
            translate([x * (s[0] / 2 - rr), y * (s[1] / 2 - rr), z * (s[2] / 2 - rr)]) sphere(r = rr);
}

// rounded_plate(size, r) — a flat plate [x, y, thickness] with its four vertical edges rounded by r.
module rounded_plate(size = [60, 40, 4], r = 4, center = false) {
    s = is_list(size) ? size : [size, size, 3];
    rr = min(r, min(s[0], s[1]) / 2 - helix_eps());
    translate([0, 0, center ? -s[2] / 2 : 0])
        linear_extrude(height = s[2])
            if (rr <= 0) square([s[0], s[1]], center = true);
            else offset(r = rr) square([s[0] - 2 * rr, s[1] - 2 * rr], center = true);
}

// cyl(d, h) — a cylinder of diameter d and height h (centred on X/Y, standing on Z=0).
module cyl(d = 10, h = 10, center = false) {
    cylinder(d = d, h = h, center = center);
}

// tube(od, id, h) — a hollow tube or bushing: outer diameter od, bore id, height h.
module tube(od = 12, id = 8, h = 10, center = false) {
    e = helix_eps();
    difference() {
        cylinder(d = od, h = h, center = center);
        translate([0, 0, center ? 0 : -e]) cylinder(d = id, h = h + 2 * e, center = center);
    }
}

// slot(length, d, h) — a stadium slot hole along X: overall length `length`, width d, through height h.
module slot(length = 20, d = 4, h = 5) {
    e = helix_eps();
    c = max(length - d, 0) / 2;
    translate([0, 0, -e]) hull() for (x = [-c, c]) translate([x, 0, 0]) cylinder(d = d, h = h + 2 * e);
}

// countersunk_hole(d, h, head_d) — a through hole Ø d of height h with a 90° countersink Ø head_d
// at the top.
module countersunk_hole(d = 3.4, h = 10, head_d = 6.5, angle = 90) {
    e = helix_eps();
    depth = max((head_d - d) / 2 / tan(angle / 2), 0);
    translate([0, 0, -e]) cylinder(d = d, h = h + 2 * e);
    translate([0, 0, h - depth])
        cylinder(d1 = d, d2 = d + 2 * (depth + e) * tan(angle / 2), h = depth + e);
}

// counterbore_hole(d, h, cb_d, cb_depth) — a through hole Ø d of height h with a flat-bottomed counterbore
// Ø cb_d, cb_depth deep, at the top.
module counterbore_hole(d = 4.5, h = 10, cb_d = 8, cb_depth = 4) {
    e = helix_eps();
    translate([0, 0, -e]) cylinder(d = d, h = h + 2 * e);
    translate([0, 0, h - cb_depth]) cylinder(d = cb_d, h = cb_depth + e);
}

// hex_pocket(af, h) — a hexagonal nut trap, width across flats af, depth h, opening upward at z=h
// (add ~0.3 to af for a printed fit; translate it so its mouth sits on the face it opens from).
module hex_pocket(af = 7, h = 4) {
    e = helix_eps();
    cylinder(d = af / cos(30), h = h + e, $fn = 6);
}

// chamfered_cylinder(d, h, c) — a cylinder Ø d, height h, with a 45° chamfer of c on both ends.
module chamfered_cylinder(d = 10, h = 10, c = 1, center = false) {
    cc = min(c, d / 2 - helix_eps(), h / 2 - helix_eps());
    translate([0, 0, center ? -h / 2 : 0])
        if (cc <= 0) cylinder(d = d, h = h);
        else hull() {
            translate([0, 0, cc]) cylinder(d = d, h = h - 2 * cc);
            cylinder(d = d - 2 * cc, h = h);
        }
}

// rect_pattern(cols, rows, dx, dy) — repeats its children in a cols×rows grid, dx/dy apart, centred on
// the origin: rect_pattern(2, 2, 80, 40) countersunk_hole(...) puts four holes at (±40, ±20).
module rect_pattern(cols = 2, rows = 2, dx = 20, dy = 20) {
    for (i = [0 : max(cols, 1) - 1], j = [0 : max(rows, 1) - 1])
        translate([(i - (cols - 1) / 2) * dx, (j - (rows - 1) / 2) * dy, 0]) children();
}

// ring_pattern(n, d) — repeats its children n times around a circle of diameter d (a bolt circle).
module ring_pattern(n = 6, d = 40) {
    for (i = [0 : max(n, 1) - 1])
        rotate([0, 0, i * 360 / max(n, 1)]) translate([d / 2, 0, 0]) children();
}
"""

# The model-facing cheat-sheet. prompts.py interpolates this into the hologram coder's instructions, so
# it has to be COMPACT (tokens on every hologram turn) and COMPLETE (a helper that is not listed is a
# helper the coder will reinvent, badly). test_scad pins that every module/function defined in
# HELIX_LIB appears here and vice versa, so the two can never drift apart.
HELIX_LIB_DOC = """\
helix.scad — `use <helix.scad>;` — millimetres. Solids are centred on X/Y and stand on Z=0 (center=true
centres them); hole tools run from just below 0 to just above h with the head at the top, z=h; curves
follow $fa/$fs — never set a global $fn. Helpers:
helix_quality(level) { ... } — wrap the whole model; "draft" | "normal" | "fine" sets $fa/$fs inside
helix_eps() — 0.01 mm, the overshoot the hole tools use so cuts never leave a zero-thickness skin
m_size(m) — 4 or "M4" → 4
m_clearance(m) — clearance hole Ø for an M-size bolt (M3 3.4, M4 4.5, M5 5.5, M6 6.6, M8 9.0)
m_tap(m) — tap-drill Ø for threading an M-size hole (M3 2.5, M4 3.3, M5 4.2, M6 5.0)
m_head_d(m) — socket-cap head Ø for counterbores (M3 5.5, M4 7, M5 8.5, M6 10)
m_csk_d(m) — 90° countersunk head Ø (M3 6, M4 8, M5 10, M6 12)
m_nut_af(m) — hex nut width across flats for hex_pocket (M3 5.5, M4 7, M5 8, M6 10)
rounded_box(size, r) — box [x,y,z] with every edge and corner rounded by r
rounded_plate(size, r) — flat plate [x,y,thickness] with its four vertical edges rounded by r
cyl(d, h) — cylinder Ø d, height h
tube(od, id, h) — hollow tube / bushing: outer Ø od, bore Ø id, height h
slot(length, d, h) — stadium slot hole along X: overall length, width d, through height h
countersunk_hole(d, h, head_d) — through hole Ø d, height h, 90° countersink Ø head_d at the top
counterbore_hole(d, h, cb_d, cb_depth) — through hole Ø d, height h, counterbore Ø cb_d × cb_depth on top
hex_pocket(af, h) — hexagonal nut trap, across-flats af, depth h, opening upward at z=h (add ~0.3 for fit)
chamfered_cylinder(d, h, c) — cylinder Ø d, height h, 45° chamfer c on both ends
rect_pattern(cols, rows, dx, dy) { child } — repeats the child in a centred cols×rows grid, dx/dy apart
ring_pattern(n, d) { child } — repeats the child n times around a circle of diameter d (bolt circle)
"""


# ---------------------------------------------------------------------------------------------------
# Source readers.
# ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScadParam:
    """One customizer parameter: a top-level assignment the user may change by voice ("make w 100")."""

    name: str
    value: str                      # the literal as written ("80", "true", "\"M4\"")
    kind: str                       # "number" | "bool" | "string"
    minimum: float | None = None    # from // [min:max] or // [min:step:max]
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()   # from // [a, b, c]  (labels allowed: // [1:One, 2:Two])
    description: str = ""           # the comment line immediately above, if any


_ASSIGN_RE = re.compile(r"^\s*(\$?[A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", re.S)
_DEF_RE = re.compile(r"^\s*(module|function)\b")
_DIRECTIVE_RE = re.compile(r"^\s*(use|include)\s*<")
_GROUP_RE = re.compile(r"^\s*/\*\s*\[\s*([^\]]*?)\s*\]\s*\*/\s*$")
_ANNOT_RE = re.compile(r"//\s*\[(.*)\]\s*$")
_NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
# The brief header's field lines and part bullets, as parse_brief reads them. Shared with parse_params
# so the two readers agree on what a header IS: a `// Units: mm` sitting directly above the first
# parameter (the coder forgot the blank line) is a header field to both, never that parameter's
# description.
_BRIEF_FIELD_RE = re.compile(r"(?i)^(design|units?|parts?|material|quality)\s*:")
_BRIEF_PARTS_RE = re.compile(r"(?i)^parts?\s*:\s*(.*)$")
_BRIEF_BULLET_RE = re.compile(r"^[-*•]\s+(.*)$")


def _strip_comments(source: str, keep_strings: bool = True) -> str:
    """Blank out // and /* */ comments (and, unless keep_strings, string literals) while preserving line
    structure — every character outside a comment stays at the same offset, so line numbers and the
    brace/paren balance are computed on what the compiler actually parses. A `{` inside a comment or a
    `"` inside a string must never count."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        two = source[i:i + 2]
        if two == "//":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if two == "/*":
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in source[i:j]))
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(source[i:j] if keep_strings else '"' + " " * (j - i - 2) + '"')
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _classify(value: str) -> str:
    """The panel's idea of a value's type. Literal bools and strings are exact; everything else is
    "number" when it could be arithmetic (80, -2.5, w/2, max(a, b)) — vectors and anything quoted fall
    back to "string" so the panel shows them verbatim rather than offering a slider for [40, 30, 20]."""
    v = value.strip()
    if v in ("true", "false"):
        return "bool"
    if v.startswith('"') or '"' in v or v.startswith("["):
        return "string"
    return "number"


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside brackets/quotes — so a choice list like [a:Label, one] stays intact."""
    parts, depth, buf, quoted = [], 0, [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
        if ch == sep and depth == 0 and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _to_float(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _annotation(inner: str, kind: str) -> dict:
    """Decode the customizer's `// [...]` forms. [min:max] and [min:step:max] are ranges; [a, b, c] and
    [1:One, 2:Two] are choices (the VALUE before a label's colon is what goes back into the source); a
    lone [N] on a number is OpenSCAD's "slider 0..N" shorthand, on anything else a single choice."""
    items = [p.strip() for p in _split_top(inner, ",") if p.strip()]
    if not items:
        return {}
    if len(items) == 1:
        bits = [b.strip() for b in items[0].split(":")]
        nums = [_to_float(b) for b in bits]
        if len(bits) == 2 and all(x is not None for x in nums):
            return {"minimum": nums[0], "maximum": nums[1]}
        if len(bits) == 3 and all(x is not None for x in nums):
            return {"minimum": nums[0], "step": nums[1], "maximum": nums[2]}
        if len(bits) == 1 and kind == "number" and nums[0] is not None:
            return {"minimum": 0.0, "maximum": nums[0]}
        return {"choices": (bits[0],)}
    return {"choices": tuple(it.split(":", 1)[0].strip() for it in items)}


def parse_params(source: str) -> list[ScadParam]:
    """The OpenSCAD customizer convention: top-level assignments before the first module/function/
    block, with `/* [Tab] */` groups and `// [..]` range/choice annotations. Stops at the first module/
    function definition or the first top-level statement that is not an assignment — so a
    `helix_quality("normal") { ... }` or a bare `bracket();` marks the end of the panel and geometry can
    never leak into it. `$fa`-style special variables and anything under `/* [Hidden] */` are skipped,
    as the customizer skips them: "make $fs 0.2" is not a design decision a person makes by voice."""
    params: list[ScadParam] = []
    lines = source.splitlines()
    description = ""
    hidden = False
    i = 0
    in_block_comment = False
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if not line:
            description = ""      # the description must sit DIRECTLY above its parameter
            continue
        group = _GROUP_RE.match(line)
        if group:
            hidden = group.group(1).strip().lower() == "hidden"
            description = ""
            continue
        if line.startswith("/*"):
            if "*/" not in line[2:]:
                in_block_comment = True
            description = ""
            continue
        if line.startswith("//"):
            text = line[2:].strip()
            # A `// [..]` annotation on its own line is not prose, and neither is a brief-header line:
            # with no blank between the header and the first parameter, `// Units: mm` would otherwise
            # become the description the parameter panel shows for `width`.
            is_header = bool(_BRIEF_FIELD_RE.match(text) or _BRIEF_BULLET_RE.match(text))
            description = "" if (_ANNOT_RE.match(line) or is_header) else text
            continue
        if _DIRECTIVE_RE.match(line):
            description = ""
            continue
        if _DEF_RE.match(line):
            break
        m = _ASSIGN_RE.match(raw)
        if not m:
            break                 # the first real statement: geometry starts, the panel ends
        name, rest = m.group(1), m.group(2)
        # Gather the value up to the terminating ';' at depth 0 — a vector may span several lines.
        value_chars: list[str] = []
        depth, quoted, done = 0, False, False
        tail = ""
        buf = rest
        while True:
            k = 0
            while k < len(buf):
                ch = buf[k]
                if ch == '"':
                    quoted = not quoted
                elif not quoted:
                    if ch in "([{":
                        depth += 1
                    elif ch in ")]}":
                        depth -= 1
                    elif ch == ";" and depth == 0:
                        tail = buf[k + 1:]
                        done = True
                        break
                    elif ch == "/" and buf[k:k + 2] == "//":
                        break  # a comment before the ';' — the value continues on the next line
                value_chars.append(ch)
                k += 1
            if done or i >= len(lines):
                break
            value_chars.append("\n")
            buf = lines[i]
            i += 1
        if not done:
            break                 # an unterminated assignment: nothing sane follows
        value = " ".join("".join(value_chars).split())
        stripped = tail.strip()
        annotation = stripped if stripped.startswith("//") else ""
        if stripped and not annotation:
            # A second statement on the same line (`w = 80; t = 5;`): it is processed next, and any
            # annotation after it belongs to IT, not to this parameter.
            lines.insert(i, tail)
        if name.startswith("$") or hidden:
            description = ""
            continue
        kind = _classify(value)
        extra = {}
        ann = _ANNOT_RE.search(annotation)
        if ann:
            extra = _annotation(ann.group(1), kind)
        params.append(ScadParam(name=name, value=value, kind=kind, description=description, **extra))
        description = ""
    return params


def parse_brief(source: str) -> dict:
    """The design brief the coder writes at the top of model.scad, as data for the critic and the HUD:

        // Design: Pipe wall bracket — a saddle bracket for 60.3 mm pipe with two M6 mounting holes
        // Parts:
        // - base plate
        // - saddle
        // - gusset

    or the same inside one /* ... */ block, or `// Parts: base plate, saddle, gusset` on one line. The
    title is the text after `Design:` up to the first em dash / ` - ` / `. `; the rest of that line plus
    every plain comment line that follows — before OR after the Parts line, which is where the prompt
    has the coder put the key dimensions in words — is the summary, until a blank line or a `[Group]`
    customizer header. Tolerant: anything missing is "" / [] — the header is a courtesy the critic
    enjoys, not a contract the build fails on."""
    out = {"title": "", "summary": "", "parts": []}
    text_lines: list[str | None] = []
    # Flatten the header comments into plain text lines, whatever comment style was used. A code line
    # (a `use <helix.scad>;` before or after the header is common) becomes a blank — a separator — so
    # the brief is read as one contiguous block of comment text. A top-level ASSIGNMENT is remembered
    # apart from other code (None, not ""), because the comment directly above it is that parameter's
    # description by the customizer convention parse_params reads — never brief prose, even when the
    # coder skipped the blank line the prompt asks for between header and parameters.
    in_block = False
    for raw in source.splitlines()[:80]:
        s = raw.strip()
        if in_block:
            body, _, _ = s.partition("*/")
            text_lines.append(body.strip().lstrip("*").strip())
            in_block = "*/" not in s
            continue
        if s.startswith("//"):
            text_lines.append(s[2:].strip())
        elif s.startswith("/*"):
            body, closed, _ = s[2:].partition("*/")
            in_block = not closed
            text_lines.append(body.strip().lstrip("*").strip())
        elif _ASSIGN_RE.match(raw) and not _DEF_RE.match(s) and not _DIRECTIVE_RE.match(s):
            text_lines.append(None)
        else:
            text_lines.append("")
    start = None
    for idx, t in enumerate(text_lines):
        if t and re.match(r"(?i)design\s*:", t):
            start = idx
            break
    if start is None:
        return out
    first = re.sub(r"(?i)^design\s*:\s*", "", text_lines[start]).strip()
    title, remainder = first, ""
    for sep in (" — ", " – ", " - ", ": "):
        if sep in first:
            title, remainder = first.split(sep, 1)
            break
    out["title"] = title.strip().rstrip(".")
    summary_bits = [remainder.strip()] if remainder.strip() else []
    parts: list[str] = []
    rest = text_lines[start + 1:]
    for idx, t in enumerate(rest):
        if not t:
            # A blank (or a code line) ends the brief — the next comment is usually the first
            # parameter's description, which must not be read as prose — unless what follows is
            # plainly the parts list.
            nxt = next((x for x in rest[idx + 1:] if x), "")
            if _BRIEF_PARTS_RE.match(nxt) or _BRIEF_BULLET_RE.match(nxt):
                continue
            break
        pm = _BRIEF_PARTS_RE.match(t)
        if pm:
            inline = pm.group(1).strip()
            if inline:
                parts.extend(p.strip().rstrip(".") for p in re.split(r"[,;]", inline) if p.strip())
            continue
        lm = _BRIEF_BULLET_RE.match(t)
        if lm:
            parts.append(lm.group(1).strip().rstrip("."))
            continue
        if _BRIEF_FIELD_RE.match(t):
            continue   # sibling header fields the brief may carry (Units/Material/Quality); not prose
        if re.match(r"^\[.*\]$", t):
            break      # a /* [Group] */ customizer header: the parameter block has begun
        if idx + 1 < len(rest) and rest[idx + 1] is None:
            break      # the comment directly above an assignment: a parameter description, not prose
        # Plain prose — before or AFTER the Parts line. The prompt's header puts "the key dimensions in
        # words" on the line after Parts, and that line is exactly what the critic judges a preview
        # against; a reader that stopped at Parts handed it a brief with no numbers in it.
        summary_bits.append(t)
    out["summary"] = " ".join(summary_bits).strip()
    out["parts"] = [p for p in parts if p]
    if not out["summary"]:
        out["summary"] = out["title"]
    return out


# OpenSCAD's message shapes (2021.01 wording; newer builds keep the same keywords), matched in order —
# the first hit names the problem. Each entry: (regex over the whole output, warm user sentence).
_ERROR_SHAPES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"Parser error|syntax error|Can't parse file", re.I),
     "The hologram's source didn't parse — there's a small syntax slip to fix."),
    (re.compile(r"Can't open (include file|library)|unable to open file", re.I),
     "The hologram's source asks for a library that isn't installed here."),
    (re.compile(r"Ignoring unknown (module|function)", re.I),
     "The hologram's source calls a shape helper that doesn't exist, so it couldn't be built."),
    (re.compile(r"Ignoring unknown variable|not specified as parameter", re.I),
     "The hologram's source refers to a value it never defined."),
    (re.compile(r"Assertion .* failed|assert\(", re.I),
     "A size check inside the hologram's source failed for these values."),
    (re.compile(r"Recursion detected", re.I),
     "A part of the hologram's source calls itself without end."),
    (re.compile(r"CGAL error|CGAL ERROR|not closed|not 2-manifold|Unable to convert|Nef|Manifold", re.I),
     "The geometry came out impossible to solidify — usually two shapes meeting on exactly the "
     "same face."),
    (re.compile(r"top level object is empty|top-level object is empty|No top level geometry", re.I),
     "The hologram compiled but drew nothing — nothing is placed at the top level of the source."),
    (re.compile(r"Unknown color scheme|colorscheme", re.I),
     "The preview picture couldn't be drawn with that colour scheme."),
    (re.compile(r"OpenGL|Offscreen|GLEW|framebuffer", re.I),
     "The preview picture couldn't be drawn on this machine's display driver."),
)

_DETAIL_LINE_RE = re.compile(
    r"^\s*(ERROR|WARNING|DEPRECATED|TRACE|Can't parse|Can't open|Current top level|Execution aborted|"
    r"Unable to|Unknown|Assertion|CGAL|Parser error)",
    re.I,
)
# Strip directory prefixes from "in file C:/Users/x/model.scad" so the repair prompt carries the file
# NAME the coder knows and no user path — the prompt is data the model reads, not a place for paths.
_PATH_IN_MSG_RE = re.compile(r"(?<=[\s\"'])(?:[A-Za-z]:)?[\\/](?:[^\\/\s\"',]+[\\/])*([^\\/\s\"',]+\.scad)")
_DETAIL_LIMIT = 800


def friendly_error(stderr: str) -> tuple[str, str]:
    """(user_sentence, detail) from the compiler's output.

    The user sentence is warm and names nothing internal — no paths, no "CGAL", no stderr — because the
    person hearing it cannot act on any of that; HELIX's repair loop can. The detail keeps the compiler's
    actual complaint lines (file:line + its words, paths reduced to file names, trimmed to ~800 chars)
    for the coder's repair prompt, which treats it as data."""
    text = (stderr or "").replace("\r", "")
    sentence = "The hologram's source couldn't be compiled."
    for pattern, warm in _ERROR_SHAPES:
        if pattern.search(text):
            sentence = warm
            break
    kept = [ln.rstrip() for ln in text.splitlines() if _DETAIL_LINE_RE.match(ln)]
    if not kept:
        kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-12:]
    detail = "\n".join(kept)
    detail = _PATH_IN_MSG_RE.sub(r"\1", detail)
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[:_DETAIL_LIMIT - 1].rstrip() + "…"
    return sentence, detail


def _top_level_statements(source: str) -> list[str]:
    """Each top-level statement's head (the text before its block or terminating ';'), comments and
    strings blanked, so geometry can be told from definitions and assignments structurally rather than
    by guessing at lines."""
    code = _strip_comments(source, keep_strings=False)
    # `use <x>` / `include <x>` need no ';' in OpenSCAD, so without this a directive would fuse with the
    # statement after it and a real `bracket();` would be misread as part of the directive.
    code = re.sub(r"^\s*(use|include)\s*<[^>\n]*>", "", code, flags=re.M)
    stmts: list[str] = []
    depth = 0
    buf: list[str] = []
    head: str | None = None
    for ch in code:
        if ch == "{":
            if depth == 0:
                head = "".join(buf).strip()
                buf = []
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                stmts.append(head or "")
                head = None
                buf = []
        elif depth == 0:
            if ch == ";":
                stmts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
    trailing = "".join(buf).strip()
    if trailing:
        stmts.append(trailing)
    return [s for s in stmts if s]


def inspect_source(source: str) -> list[str]:
    """Cheap static lints the coder must fix before a compile is even attempted — each a short sentence
    the repair prompt can quote. A compile costs seconds and a repair pass costs a model call, so the
    failures that can be seen from the text alone are reported from the text alone. Empty list = fine."""
    problems: list[str] = []
    code = _strip_comments(source, keep_strings=False)
    for opener, closer, name in (("{", "}", "braces"), ("(", ")", "parentheses"), ("[", "]", "brackets")):
        if code.count(opener) != code.count(closer):
            problems.append(
                f"Unbalanced {name}: {code.count(opener)} '{opener}' but {code.count(closer)} '{closer}'."
            )
    for m in re.finditer(r"^\s*(use|include)\s*<([^>]+)>", source, re.M):
        lib = m.group(2).strip()
        if lib.replace("\\", "/").split("/")[-1] != HELIX_LIB_FILE:
            problems.append(
                f"Uses <{lib}>, which isn't installed — only helix.scad is available; "
                f"write the shape with plain OpenSCAD or helix.scad helpers."
            )
    stmts = _top_level_statements(source)
    geometry = [
        s for s in stmts
        if not _DEF_RE.match(s) and not _DIRECTIVE_RE.match(s) and not _ASSIGN_RE.match(s)
    ]
    # With braces unbalanced the statement split is meaningless (an unclosed module swallows the
    # `bracket();` after it), so the brace lint stands alone rather than dragging a false one behind it.
    if not geometry and code.count("{") == code.count("}"):
        problems.append(
            "No top-level geometry: the file defines modules but never instantiates one, so nothing is "
            "drawn — add a call like `bracket();` at the end (inside helix_quality(...) { } if used)."
        )
    if not re.search(r"\bmm\b|millimet", source, re.I):
        problems.append(
            "No units hint: say the units in the header comment (HELIX models are in millimetres, "
            "e.g. `// Units: mm`) so every dimension reads the same way."
        )
    fn = re.search(r"^\s*\$fn\s*=\s*(\d+)", code, re.M)
    if fn and int(fn.group(1)) > 120:
        problems.append(
            f"A top-level $fn = {fn.group(1)} makes every curve heavy and the compile very slow; "
            f"remove it and wrap the model in helix_quality(\"normal\") {{ ... }} instead."
        )
    return problems
