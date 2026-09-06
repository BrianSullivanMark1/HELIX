"""Loaded meshes — STL files the user brings INTO a hologram, as data + pure rules.

A hologram is normally a program HELIX writes (model.py on build123d). But a great deal of what
people print was designed by someone else and published as STL: InMoov's humanoid (hundreds of
parts), a Thingiverse bracket, a release archive. This module is the pure half of loading those
files into a hologram of their own — the maker brain (services/maker.py) copies the files into the
workspace's `parts/` folder, and the design it writes beside them is an ORDINARY model.py that
calls `mesh("<file>.stl", scale)` from helix_parts for each part, so everything downstream — the
compile worker, the studio's sliders, the print sheet, a later "add a stand for it" edit by the
coder — works unchanged.

Here, without build123d and without I/O:

  - `safe_part_name` / `unique_names` — what a loaded file is called inside parts/ (a plain ASCII
    file name; never a path, so `mesh()` can refuse everything else);
  - `pack_plates`             — the print-plate layout the compile worker uses when a design's parts
    overflow one row: rows within a 244 mm plate, plates side by side along X;
  - `model_source`            — the model.py text: the brief, one `scale` parameter, the parts table,
    and a build() that loads every part.

Contract notes: the Bambu Lab P1S bed is 256 mm each way; parts stay 6 mm inside it (purge line,
brim, edge adhesion), so a plate is 244 mm square. `pack_plates` is also what the runner uses for
an authored design with many parts — the numbers agree with helix/cad/runner.py by construction
(the runner imports them from here).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

BED_MM = 256.0                          # the Bambu Lab P1S build volume, every axis
BED_MARGIN_MM = 6.0                     # parts stay this far inside the bed's edges
PLATE_MM = BED_MM - 2 * BED_MARGIN_MM   # the usable plate square: 244 mm
PART_GAP_MM = 8.0                       # between parts laid out on a plate
PLATE_GAP_MM = 40.0                     # between plates laid side by side in the exported mesh

PARTS_DIR = "parts"                     # the workspace folder loaded files live in
STL_EXT = ".stl"
MAX_PARTS = 80                          # one hologram; the viewer inlines the whole mesh
MAX_TOTAL_BYTES = 160 * 1024 * 1024     # a bigger set wants splitting into sections anyway
SCALE_MIN, SCALE_MAX = 0.25, 2.0        # the studio's scale slider (1.0 = the files' own size)

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Placement:
    """Where one part's bounding box sits in the laid-out group (its min X/Y corner), and which
    print plate it belongs to (0-based) — before the group is centred on the origin."""
    name: str
    x: float
    y: float
    plate: int


# ---------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------

def safe_part_name(raw: str) -> str:
    """The file name a loaded STL gets inside parts/: the source's base name (any folder path or
    archive path dropped), ASCII letters/digits/dot/dash/underscore only, ending in `.stl`, at most
    60 characters. Empty in → 'part.stl'. Never a path, so mesh() can insist on plain names."""
    text = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if text.lower().endswith(STL_EXT):
        text = text[: -len(STL_EXT)]
    stem = _SAFE_RE.sub("_", text).strip("._-")
    stem = re.sub(r"_+", "_", stem)[:56] or "part"
    if stem in (".", ".."):
        stem = "part"
    return stem + STL_EXT


def unique_names(names: Iterable[str]) -> list[str]:
    """The same names, with `_2`, `_3`… appended to repeats (case-insensitively, as Windows files
    are), in order."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen[key] = 1
            out.append(name)
            continue
        stem = name[: -len(STL_EXT)] if name.lower().endswith(STL_EXT) else name
        while True:
            seen[key] += 1
            candidate = f"{stem}_{seen[key]}{STL_EXT}"
            if candidate.lower() not in seen:
                seen[candidate.lower()] = 1
                out.append(candidate)
                break
    return out


def label_of(file_name: str) -> str:
    """The part's label in the design (the dict key build() returns): the file's stem."""
    name = str(file_name or "")
    return name[: -len(STL_EXT)] if name.lower().endswith(STL_EXT) else name


# ---------------------------------------------------------------------------------------------
# the plate layout
# ---------------------------------------------------------------------------------------------

def pack_plates(boxes: Sequence[tuple[str, float, float]], *, plate: float = PLATE_MM,
                gap: float = PART_GAP_MM, plate_gap: float = PLATE_GAP_MM) -> list[Placement]:
    """Shelf-pack (name, size_x, size_y) footprints, in the order given, onto square print plates:
    parts fill a row left to right until the next one would cross the plate's edge, rows stack
    back (+Y) until the next row would cross it, and then a new plate starts — laid to the right of
    the previous one with `plate_gap` between. A part wider or deeper than the plate gets a row (or
    a plate) of its own and simply overhangs; the bed check names it separately. Deterministic,
    O(n)."""
    out: list[Placement] = []
    plate_i = 0
    x = y = row_h = 0.0
    for name, sx, sy in boxes:
        sx, sy = max(0.0, float(sx)), max(0.0, float(sy))
        if x > 0.0 and x + sx > plate:          # wrap to a new row
            x, y, row_h = 0.0, y + row_h + gap, 0.0
        if y > 0.0 and y + sy > plate:          # this row would leave the plate: new plate
            plate_i += 1
            x, y, row_h = 0.0, 0.0, 0.0
        out.append(Placement(name, plate_i * (plate + plate_gap) + x, y, plate_i))
        x += sx + gap
        row_h = max(row_h, sy)
    return out


def plates_of(placements: Sequence[Placement]) -> list[list[str]]:
    """The part names per plate, in layout order — what the print sheet reads out."""
    groups: dict[int, list[str]] = {}
    for p in placements:
        groups.setdefault(p.plate, []).append(p.name)
    return [groups[i] for i in sorted(groups)]


# ---------------------------------------------------------------------------------------------
# the design text
# ---------------------------------------------------------------------------------------------

def _doc_safe(text: str) -> str:
    """Text that can sit inside the module docstring: no backslashes (a Windows path's `\\U` is
    a unicode escape to Python), no triple quotes, one line."""
    return " ".join(str(text or "").replace("\\", "/").replace('"""', "'").split())


def _fmt(v: float) -> str:
    return f"{float(v):.1f}".rstrip("0").rstrip(".")


def model_source(title: str, parts: Sequence[tuple[str, tuple[float, float, float]]], *,
                 origin: str = "", credit: str = "", scale: float = 1.0) -> str:
    """The model.py for a set of loaded parts: `parts` is [(file name inside parts/, (x, y, z) mm)]
    in layout order. The result passes cadpy.inspect_source (tests pin it): a brief docstring the
    studio and the critic read, one `scale` parameter with its slider range, a PARTS table, and a
    build() that loads each file with helix_parts.mesh — nothing else."""
    heading = _doc_safe(title) or "Loaded parts"
    n = len(parts)
    lines = [f'"""Design: {heading} — {n} STL part{"" if n == 1 else "s"} loaded as printed meshes'
             + (f" ({_doc_safe(credit)})" if credit else "")]
    lines.append("Parts:")
    for file_name, size in parts:
        sx, sy, sz = size
        lines.append(f"- {label_of(file_name)} — {_fmt(sx)} × {_fmt(sy)} × {_fmt(sz)} mm")
    if origin:
        lines.append(f"Loaded from: {_doc_safe(origin)}.")
    lines.append("Each part keeps its file's own print orientation; HELIX lays them out on Bambu P1S "
                 "plates and measures which need supports. Change `scale` to print the set smaller or "
                 "larger; to add a fixture or a stand beside them, edit build() — mesh(file) loads any "
                 "file in parts/.")
    lines.append('"""')
    lines.append("from helix_parts import *")
    lines.append("")
    lines.append("# --- Parameters ---")
    lines.append(f"scale = {float(scale):g}   # [{SCALE_MIN:g}..{SCALE_MAX:g}..0.05] print scale — "
                 f"1.0 is the files' own size")
    lines.append("# --- End Parameters ---")
    lines.append("")
    lines.append("# The loaded files, in layout order (label -> file inside this hologram's parts/ folder).")
    lines.append("PARTS = {")
    for file_name, _size in parts:
        lines.append(f'    "{label_of(file_name)}": "{file_name}",')
    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def build():")
    lines.append("    return {label: mesh(file, scale) for label, file in PARTS.items()}")
    lines.append("")
    return "\n".join(lines)
