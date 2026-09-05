"""The enclosure generator — a parts list in, a model.py out, deterministically.

WHY: every enclosure HELIX made before this was a fresh LLM CAD run whose pockets were sized from
memory ("XIAO_W, XIAO_H = 22, 18"). Here the numbers come from the component library
(helix/domain/components.py) and the geometry from one fixed recipe — the same two-half shell the
coder prompt teaches (lip_ring + lip_rebate, screw towers with the tower-height formula, mirrored
mating written out, both halves authored plate-face-down) — so the result compiles, passes the
runner's print checks, and fits the parts it was planned around. The LLM edit path still works on
the output: it is ordinary model.py with a parameter block and a layout table.

Pure Python (stdlib + the component schema): no build123d here. The kernel work happens when the
emitted source is compiled by helix/cad/runner.py with helix_parts seeded beside it.

Contract: READ_ME/MAKER_FLOW.md §4 (this module) and §6 (layout.json).

Frames, once and for all:
  - PLAN frame (the layout): x right, y "back", millimetres, origin at the enclosure's OUTER
    bottom-left corner, looking down at the BASE's inner face in its print orientation (plate face
    on the plate, cavity opening upward). Every component — on the base or on the lid — is placed
    in this one frame. `front` = the base's plate face, `back` = the lid's plate face,
    `left`/`right` = the x = 0 / x = L walls, `bottom`/`top` = the y = 0 / y = W walls.
  - MODEL frame (model.py): the same plan, centred on the origin (x - L/2, y - W/2). The lid is
    authored in ITS print orientation, which is the plan mirrored in x (the lid flips about Y to
    mate), and the generated source carries that mirror explicitly.
  - Heights: a wall aperture's `z` is measured from the enclosure's outer FRONT face (the base's
    plate face); a component's `z_top` is its top above its own half's inner floor.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from helix.domain import cadpy
from helix.domain.components import Aperture, Component, Port

# ----- the recipe's fixed numbers (mm) -----
AIR = 3.0              # air above the tallest base part before the lid's contents
FEATURE_AIR = 2.0      # air kept between interior features (towers, pockets, bands)
STANDOFF_H = 3.0       # boss height under a board with verified holes
RAIL_H = 3.0           # slot height under a hole-less board in side rails
RIB = 1.6              # pocket wall thickness (two perimeter passes)
LIP_H = 3.0            # the lip ring's engagement into the lid
LID_MIN_IN = 5.5       # the lid cavity is never shallower than this (lip_h 3 + base 2 + 0.5 clearance)
BASE_MIN_IN = 8.0
LABEL_SIZE = 3.4       # font size of the debossed labels (caps come out ~2.5 mm)
LABEL_BAND = 4.4       # the strip beside a pocket a label is cut into (a pocket may grow 1 mm
                       # by slider and still not reach the letters)
SLACK_GROW = 1.0       # the most a pocket can grow per side by the clearance/extra sliders
LID_OVERHANG = {"strap": 11.0, "wall_tabs": 11.0}   # how far the lid's tabs reach past its wall
APPROX_EXTRA = 0.5     # extra room per side around community-measured parts
PORT_REACH = 6.0       # a port this close to a wall (component edge → wall) gets an opening
PLATE_RECESS = 1.0     # lens recess depth on the plate face (bridged clean at this height)
WALL_MAX = 3.5         # the wall slider's top — bands and towers are sized for it
WALLS = ("left", "right", "top", "bottom")
PLATES = ("front", "back")
INTERNAL_PORTS = frozenset({"jst_ph", "jst_xh", "header"})
LID_STYLES = ("screw", "snap")
MOUNTS = ("none", "wall_tabs", "strap", "din", "flat_feet")
STRAP_SLOT = (22.0, 4.0)      # slot for a 20 mm elastic band (strap mounts)
TAB_HOLE = 4.5                # wall-tab screw hole (M4 / #8 wood screw)
FOOT_D, FOOT_INSET = 10.5, 15.0  # stick-on rubber bumper recesses on the back (flat_feet)
DIN_HOLE_X = 14.0             # the DIN clip's two screws sit at (±14, 0): symmetric, so the
                              # clip-to-lid mirror pairing is satisfied trivially
DIN_CLIP_W = 46.0
BED_MARGIN = 6.0              # the runner keeps parts this far inside the bed
MIN_CAVITY = 20.0             # no cavity side shrinks below this, whatever the parts list says
PLATE_GAP = 8.0               # the runner's gap between laid-out parts

# Two screw sizes: small shells take M2 (3.2 mm heat-set inserts), larger ones M3.
SCREWS = {
    "M2": {"insert": 3.2, "od": 7.6, "csk": (2.4, 4.4), "screw": "M2x8", "const": "INSERT_M2"},
    "M3": {"insert": 4.0, "od": 8.4, "csk": (3.4, 6.3), "screw": "M3x10", "const": "INSERT_M3"},
}
M3_FROM = 90.0                 # a cavity this long (or wide) moves up to M3

# Wall openings for the plug of each external port kind: (width along the wall, height, and the
# ESTIMATED height of the connector's centre above the board's underside — a 1.6 mm PCB plus half
# the receptacle; generous openings absorb the estimate).
PORT_OPENINGS: dict[str, tuple[float, float, float]] = {
    "usb_c": (12.0, 7.0, 3.2), "micro_usb": (11.0, 7.0, 3.0), "usb_a": (15.0, 8.5, 5.5),
    "barrel_5_5": (11.0, 11.0, 7.0), "sd": (14.0, 3.5, 2.4), "hdmi": (17.0, 8.0, 4.0),
    "audio_3_5": (8.0, 8.0, 4.0), "antenna": (7.0, 7.0, 3.5),
}
# Panel-mount connectors (mount="clip"): the hole for the threaded bushing, Ø.
PANEL_HOLES = {"barrel_5_5": 8.2, "audio_3_5": 6.2, "antenna": 6.6}
# Switch slots — the same table helix_parts.SWITCH_SLOTS carries (kind → shape, along, tall).
SWITCH_SLOTS: dict[str, tuple[str, float, float]] = {
    "ss12d00": ("slot", 8.5, 3.6), "kcd1": ("rect", 19.2, 13.5), "tact_6": ("round", 6.5, 6.5),
    "push_12": ("round", 12.4, 12.4), "ky040": ("round", 7.2, 7.2),
}
_CATEGORY_LABELS = {
    "mcu": "MCU", "camera": "CAM", "mic": "MIC", "amp": "AMP", "speaker": "SPK", "battery": "BAT",
    "charger": "CHG", "power": "PWR", "switch": "SW", "button": "BTN", "display": "DISP",
    "sensor": "SENS", "motor": "MOT", "driver": "DRV", "led": "LED", "connector": "CONN",
    "storage": "SD", "comm": "RF", "misc": "PART",
}


# ---------------------------------------------------------------------------------------------
# The contract's shapes (MAKER_FLOW §4)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    component: Component
    qty: int = 1
    label: str = ""
    face: str = ""            # front | back | left | right | top | bottom — which wall it must reach
    on_lid: bool = False


@dataclass(frozen=True)
class EnclosureSpec:
    name: str
    items: tuple[Item, ...]
    wall: float = 2.0
    clearance: float = 0.6
    corner_r: float = 3.0
    floor: float = 2.0
    lid: str = "screw"        # screw | snap | slide (slide is not generated yet — see problems)
    mount: str = "none"       # none | wall_tabs | strap | din | flat_feet
    channel: float = 4.0      # wire trench width between pockets
    labels: bool = True
    bed: tuple[float, float, float] = (256.0, 256.0, 256.0)


@dataclass(frozen=True)
class Placed:
    key: str
    label: str
    x: float                  # the pocket's inside rectangle, plan frame (bottom-left corner)
    y: float
    w: float
    h: float
    rot: int                  # 0 | 90 | 180 | 270 — the component's rotation in plan
    face: str
    mount: str                # standoff | rails | pocket | ring | bay | clip
    on_lid: bool
    z_top: float              # the part's top above its half's inner floor
    flip: bool = False        # the component lies face-down (its top face looks through a plate)
    slack: float = 0.0        # room per side between the part and the pocket inside
    rib: float = 0.0          # pocket wall thickness (0 for standoffs/rails/clip)
    pocket_h: float = 0.0     # rib height (or the standoff/rail height under a board)
    omit: str = ""            # the pocket side left open against a wall (plan side name)
    name: str = ""            # the component's name, for the table and the report
    apertures: tuple[dict, ...] = ()   # plate-face cuts made for it (plan frame)
    holes: tuple[tuple[float, float], ...] = ()   # the board's mounting holes (component frame)
    hole_d: float = 0.0

    def rect(self) -> tuple[float, float, float, float]:
        """The pocket's outside (ribs included, none on the omitted side), plan frame."""
        return (self.x - (0.0 if self.omit == "left" else self.rib),
                self.y - (0.0 if self.omit == "bottom" else self.rib),
                self.x + self.w + (0.0 if self.omit == "right" else self.rib),
                self.y + self.h + (0.0 if self.omit == "top" else self.rib))


@dataclass(frozen=True)
class Layout:
    outer: tuple[float, float, float]
    inner: tuple[float, float, float]
    wall: float
    floor: float
    placed: tuple[Placed, ...]
    apertures: tuple[dict, ...]
    screws: tuple[dict, ...]
    lid: str
    problems: tuple[str, ...]
    split: tuple[float, float] = (0.0, 0.0)   # (base cavity depth, lid cavity depth)
    lip_h: float = LIP_H
    name: str = ""
    mount: str = "none"
    channel: float = 4.0
    labels: bool = True
    screw_size: str = "M2"


# ---------------------------------------------------------------------------------------------
# Small geometry helpers (plan frame)
# ---------------------------------------------------------------------------------------------

def _r(v: float) -> float:
    return round(float(v) + 0.0, 2)


def _num(v: float) -> str:
    """A number as a clean Python literal: 12 → '12.0', 12.5 → '12.5', 0.75 → '0.75'."""
    s = f"{_r(v):.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _safe(text: str) -> str:
    """A name or label as it may appear inside the generated source's strings and docstrings:
    no quotes, no backslashes, no newlines (a user-typed part name is data, never code)."""
    return " ".join(str(text or "").replace("\\", "").replace('"', "'").split())


def _ident(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "part"
    if base[0].isdigit():
        base = "p_" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    taken.add(name)
    return name


def _plan_point(ax: float, ay: float, length: float, width: float, rot: int, flip: bool) -> tuple[float, float]:
    """A point in a component's own frame (bottom-left, lying flat, face up) → the footprint's
    frame after the optional face-down flip (mirror in x) and a CCW rotation, both keeping the
    footprint in the positive quadrant."""
    if flip:
        ax = length - ax
    if rot == 0:
        return ax, ay
    if rot == 90:
        return width - ay, ax
    if rot == 180:
        return length - ax, width - ay
    return ay, length - ax


_SIDE_NORMAL = {"left": (-1, 0), "right": (1, 0), "front": (0, -1), "back": (0, 1)}
_WALL_NORMAL = {"left": (-1, 0), "right": (1, 0), "bottom": (0, -1), "top": (0, 1)}


def _side_normal(side: str, rot: int, flip: bool) -> tuple[int, int]:
    nx, ny = _SIDE_NORMAL.get(side, (0, 0))
    if flip:
        nx = -nx
    for _ in range(rot // 90):
        nx, ny = -ny, nx
    return nx, ny


def _wall_of(normal: tuple[int, int]) -> str:
    for wall, n in _WALL_NORMAL.items():
        if n == normal:
            return wall
    return ""


def _rot_for(side: str, wall: str, flip: bool) -> int:
    """The rotation that makes a component side face a wall (the first of 0/90/180/270)."""
    for rot in (0, 90, 180, 270):
        if _side_normal(side, rot, flip) == _WALL_NORMAL[wall]:
            return rot
    return 0


def _port_point(port: Port, length: float, width: float) -> tuple[float, float]:
    """Where a port's centre sits on the component's outline, component frame. Port.x is
    measured from the side's LEFT end as seen from OUTSIDE the component (the schema's words)."""
    if port.side == "left":
        return 0.0, width - port.x
    if port.side == "right":
        return length, port.x
    if port.side == "back":
        return length - port.x, width
    return port.x, 0.0


def _overlap(a, b, gap: float = 0.0) -> bool:
    return not (a[2] + gap <= b[0] + 1e-6 or b[2] + gap <= a[0] + 1e-6
                or a[3] + gap <= b[1] + 1e-6 or b[3] + gap <= a[1] + 1e-6)


# ---------------------------------------------------------------------------------------------
# Classifying an item: how it mounts, what it must reach, how much room it takes
# ---------------------------------------------------------------------------------------------

@dataclass
class _Cell:
    """One item as the packer sees it: a rectangle with margins, a mounting recipe, and the
    feature (port or aperture) that decides its wall."""

    uid: str
    label: str
    comp: Component
    on_lid: bool
    face: str                 # the hint, validated
    kind: str                 # standoff | rails | pocket | ring | bay | clip | switch | wallmount
    slack: float
    rib: float
    standing: float           # z_top above the half's inner floor
    mount_h: float            # standoff / rail height under a board (0 for pockets)
    pocket_h: float
    rots: tuple[int, ...]
    flip: bool = False
    wall: str = ""            # the wall the cell must touch ("" = free)
    plate: str = ""           # front | back when a plate aperture is cut for it
    port: Port | None = None  # the port that decided the wall (or the reachable one)
    wall_aperture: Aperture | None = None   # an aperture looking through a side wall
    switch: str = ""          # switch slot kind (SWITCH_SLOTS)
    panel_d: float = 0.0      # panel-mount bushing hole Ø
    plan_dims: dict = field(default_factory=dict)   # rot → (fw, fh) of the PART footprint
    x: float = -1.0
    y: float = -1.0
    rot: int = 0
    problems: list = field(default_factory=list)

    def cell_dims(self, rot: int, labels: bool, channel: float) -> tuple[float, float]:
        fw, fh = self.plan_dims[rot]
        m = self.margins(labels, channel)
        return (fw + 2 * self.slack + m["left"] + m["right"],
                fh + 2 * self.slack + m["bottom"] + m["top"])

    def margins(self, labels: bool, channel: float) -> dict[str, float]:
        """Per plan side: rib + trench (channel/2), the label band below (above when the pocket
        backs onto the bottom wall), nothing on the open side against a wall."""
        half = channel / 2.0
        m = {s: self.rib + half for s in WALLS}
        if labels and self.kind != "clip":
            below = "top" if self.wall == "bottom" else "bottom"
            m[below] = self.rib + max(half, LABEL_BAND)
        if self.wall:
            m[self.wall] = 0.0
        return m


def _switch_kind(comp: Component) -> str:
    hay = " ".join([comp.key, comp.name, *comp.aliases]).lower()
    flat = re.sub(r"[^a-z0-9]+", "", hay)
    if "ss12d00" in flat or "ss12d" in flat:
        return "ss12d00"
    if "kcd1" in flat:
        return "kcd1"
    if "ky040" in flat or "rotaryencoder" in flat:
        return "ky040"
    if ("push" in hay or "latching" in hay or "momentary" in hay) and "12" in hay:
        return "push_12"
    if "tact" in hay and ("6x6" in flat or "6mm" in flat or "6 mm" in hay or "tact_6" in hay):
        return "tact_6"
    return ""


def _external_ports(comp: Component) -> list[Port]:
    out = []
    for p in comp.ports:
        if p.kind in INTERNAL_PORTS:
            continue
        if p.kind == "other" and not (p.width and p.height):
            continue
        if p.kind not in PORT_OPENINGS and p.kind != "other":
            continue
        out.append(p)
    return out


def _opening_for(port: Port, comp: Component) -> tuple[float, float, float]:
    """(width, height, centre height above the board's underside) of the wall opening a port
    needs — the centre height is an estimate unless the library gave the opening explicitly."""
    w, h, z = PORT_OPENINGS.get(port.kind, (0.0, 0.0, 2.6))
    if port.width and port.height:
        w, h = float(port.width), float(port.height)
    if port.kind == "other":
        z = min(comp.height / 2.0, max(2.0, comp.height - h / 2.0 - 0.5))
    return w, h, z


def _default_label(comp: Component) -> str:
    if comp.category == "mcu" and "vision" in comp.tags:
        return "CAM"
    return _CATEGORY_LABELS.get(comp.category, "PART")


def _no_slot_line(label: str) -> str:
    return (f"{label}: no slot size is known for this switch — nothing was cut; edit model.py or pick "
            f"one of {', '.join(SWITCH_SLOTS)}.")


def _classify(item: Item, spec: EnclosureSpec, uid: str, label: str) -> _Cell:
    c = item.component
    face = (item.face or "").strip().lower()
    problems: list[str] = []
    if face and face not in WALLS and face not in PLATES:
        problems.append(f"{label}: face '{face}' isn't one of front/back/left/right/top/bottom — placed freely.")
        face = ""
    slack = spec.clearance + c.clearance + (APPROX_EXTRA if c.approx else 0.0)
    L, W, H = float(c.length), float(c.width), float(c.height)
    verified_holes = bool(c.holes) and c.mount == "standoff"
    kind = "pocket"
    if c.category == "speaker" and abs(L - W) < 0.5 and any(a.kind == "speaker" for a in c.apertures):
        kind = "ring"
    elif c.category == "battery" and not verified_holes:
        kind = "bay"
    elif c.category in ("switch", "button"):
        kind = "switch"
    elif c.category == "connector" and c.mount == "clip":
        kind = "clip"
    elif c.mount == "rails" and not verified_holes:
        kind = "rails"
    elif verified_holes:
        kind = "standoff"
    rib = 0.0 if kind in ("standoff", "rails", "clip") else RIB
    mount_h = STANDOFF_H if kind == "standoff" else (RAIL_H if kind == "rails" else 0.0)
    standing = mount_h + H
    if kind == "ring":
        pocket_h = H + 0.5
    elif kind == "bay":
        pocket_h = min(H + 0.5, 8.0)
    elif kind == "switch":
        pocket_h = min(max(H, 3.0), 4.0)
    elif kind in ("standoff", "rails"):
        pocket_h = mount_h
    elif kind == "clip":
        pocket_h = 0.0
    else:
        pocket_h = max(3.0, min(6.0, H - 1.0))
    cell = _Cell(uid=uid, label=label, comp=c, on_lid=item.on_lid, face=face, kind=kind, slack=slack,
                 rib=rib, standing=standing, mount_h=mount_h, pocket_h=pocket_h, rots=(0, 90),
                 problems=problems)
    cell.plan_dims = {0: (L, W), 90: (W, L), 180: (L, W), 270: (W, L)}
    plate_apertures = [a for a in c.apertures if a.face in ("top", "bottom")]
    ext_ports = _external_ports(c)
    cell.switch = _switch_kind(c) if kind == "switch" else ""

    if face in PLATES:
        if (face == "front" and item.on_lid) or (face == "back" and not item.on_lid):
            problems.append(f"{label}: faces '{face}' but sits {'on the lid' if item.on_lid else 'on the base'} — "
                            f"{'clear' if item.on_lid else 'set'} on_lid so it can reach that face; placed freely.")
            cell.face = ""
        elif kind == "switch":
            if not cell.switch:
                problems.append(_no_slot_line(label))
            cell.plate = face
        elif plate_apertures:
            cell.plate = face
            cell.flip = any(a.face == "top" for a in plate_apertures)
        else:
            problems.append(f"{label}: faces '{face}' but the library lists no aperture (lens, mic, "
                            f"speaker, screen…) for it — nothing was cut; placed freely.")
            cell.face = ""
        if ext_ports:
            cell.port = ext_ports[0]   # reachable only if it lands near a wall
        return cell

    if face in WALLS:
        cell.wall = face
        if kind == "clip":
            dims = sorted((L, W, H), reverse=True)
            into, along, tall = dims[0], dims[1], dims[2]
            for p in c.ports:
                if p.kind in PANEL_HOLES:
                    cell.panel_d = PANEL_HOLES[p.kind]
                    break
            if not cell.panel_d:
                if ext_ports:
                    cell.port = ext_ports[0]
                else:
                    problems.append(f"{label}: a panel-mount connector with no port kind in the library — no hole was cut.")
            cell.standing = tall
            cell.pocket_h = 0.0
            _set_wall_dims(cell, face, along, into)
            return cell
        if kind == "switch":
            if not cell.switch:
                problems.append(_no_slot_line(label))
            cell.standing = W
            cell.pocket_h = min(max(W - 1.0, 2.5), 4.0)
            _set_wall_dims(cell, face, L, H)
            return cell
        if ext_ports:
            port = ext_ports[0]
            cell.port = port
            cell.rot = _rot_for(port.side, face, False)
            cell.rots = (cell.rot,)
            return cell
        if plate_apertures:
            # Stands on edge with its face against the wall so the aperture looks through it.
            cell.kind = "wallmount"
            cell.wall_aperture = plate_apertures[0]
            cell.standing = W
            cell.pocket_h = min(max(W - 1.0, 2.5), 4.0)
            _set_wall_dims(cell, face, L, H)
            return cell
        problems.append(f"{label}: faces the {face} wall but has no port or aperture to bring to it — "
                        f"placed against that wall anyway.")
        return cell

    if ext_ports:
        cell.port = ext_ports[0]
    return cell


def _set_wall_dims(cell: _Cell, wall: str, along: float, into: float) -> None:
    """A wall-mounted footprint: `along` runs along the wall, `into` into the cavity."""
    dims = (into, along) if wall in ("left", "right") else (along, into)
    cell.plan_dims = {0: dims, 90: dims, 180: dims, 270: dims}
    cell.rot = 0
    cell.rots = (0,)


# ---------------------------------------------------------------------------------------------
# The packer: corner-point bottom-left packing in a fixed bin, grown until everything fits
# ---------------------------------------------------------------------------------------------

def _cell_area(c: _Cell, labels: bool, channel: float) -> float:
    return max(c.cell_dims(r, labels, channel)[0] * c.cell_dims(r, labels, channel)[1] for r in c.rots)


POLICIES = ("bl", "br", "lb", "bbox")   # placement orders the packer tries for every bin


def _score(policy: str, cell: _Cell, rot: int, rect, L: float, W: float, placed: list) -> tuple:
    """Lower is better. bl = bottom-left, br = bottom-right, lb = left-bottom (column first),
    bbox = the smallest bounding box of everything placed so far. A cell whose port side lands
    on a wall always wins over one whose port stays inside."""
    port = _port_score(cell, rot, rect, L, W)
    x, y = rect[0], rect[1]
    if policy == "br":
        return (port, y, -x, rot)
    if policy == "lb":
        return (port, x, y, rot)
    if policy == "bbox":
        xs = [r[2] for r in placed] + [rect[2]]
        ys = [r[3] for r in placed] + [rect[3]]
        return (port, round(max(xs) * max(ys), 3), y, x, rot)
    return (port, y, x, rot)


def _pack(cells: list[_Cell], L: float, W: float, obstacles: list, labels: bool, channel: float,
          policy: str = "bl") -> dict | None:
    """Place every cell inside [0, L] × [0, W] (the cavity), never overlapping each other or an
    obstacle. Wall-hinted cells first, biggest first, each at the best corner point (per the
    policy) that honours its wall. None when something can't be placed."""
    order = sorted(cells, key=lambda c: (0 if c.wall else 1, -_cell_area(c, labels, channel), c.label, c.uid))
    placed: list[tuple[float, float, float, float]] = []
    out: dict[str, tuple[float, float, int]] = {}
    for cell in order:
        best = None
        for rot in cell.rots:
            cw, ch = cell.cell_dims(rot, labels, channel)
            if cw > L + 1e-6 or ch > W + 1e-6:
                continue
            blockers = [r for tag, r in obstacles if not (cell.wall and tag == "band_" + cell.wall)] + placed
            xs = {0.0, L - cw} | {r[2] for r in blockers} | {r[0] - cw for r in blockers}
            ys = {0.0, W - ch} | {r[3] for r in blockers} | {r[1] - ch for r in blockers}
            for y in sorted(ys):
                if y < -1e-6 or y + ch > W + 1e-6:
                    continue
                for x in sorted(xs):
                    if x < -1e-6 or x + cw > L + 1e-6:
                        continue
                    if cell.wall == "left" and x > 1e-6:
                        continue
                    if cell.wall == "right" and abs(x + cw - L) > 1e-6:
                        continue
                    if cell.wall == "bottom" and y > 1e-6:
                        continue
                    if cell.wall == "top" and abs(y + ch - W) > 1e-6:
                        continue
                    rect = (x, y, x + cw, y + ch)
                    if any(_overlap(rect, b) for b in blockers):
                        continue
                    score = _score(policy, cell, rot, rect, L, W, placed)
                    if best is None or score < best[0]:
                        best = (score, x, y, rot)
        if best is None:
            return None
        _, x, y, rot = best
        cw, ch = cell.cell_dims(rot, labels, channel)
        placed.append((x, y, x + cw, y + ch))
        out[cell.uid] = (round(x, 3), round(y, 3), rot)
    return out


def _port_score(cell: _Cell, rot: int, rect, L: float, W: float) -> int:
    """0 when an unhinted cell's port side lands on a wall (so a plug can reach it), else 1."""
    if cell.wall or cell.port is None:
        return 0
    wall = _wall_of(_side_normal(cell.port.side, rot, cell.flip))
    touching = {"left": rect[0] <= 1e-6, "right": rect[2] >= L - 1e-6,
                "bottom": rect[1] <= 1e-6, "top": rect[3] >= W - 1e-6}
    return 0 if touching.get(wall) else 1


def _tower_keepouts(L: float, W: float, n: int, k: float) -> list:
    corners = [("tower_bl", (0.0, 0.0, k, k)), ("tower_tr", (L - k, W - k, L, W))]
    if n >= 4:
        corners += [("tower_br", (L - k, 0.0, L, k)), ("tower_tl", (0.0, W - k, k, W))]
    return corners


def _lid_bands(L: float, W: float, b: float) -> list:
    return [("band_left", (0.0, 0.0, b, W)), ("band_right", (L - b, 0.0, L, W)),
            ("band_bottom", (0.0, 0.0, L, b)), ("band_top", (0.0, W - b, L, W))]


def _din_obstacles(L: float, W: float, mount: str) -> list:
    if mount != "din":
        return []
    r = SCREWS["M3"]["od"] / 2.0 + FEATURE_AIR
    return [("din_l", (L / 2 - DIN_HOLE_X - r, W / 2 - r, L / 2 - DIN_HOLE_X + r, W / 2 + r)),
            ("din_r", (L / 2 + DIN_HOLE_X - r, W / 2 - r, L / 2 + DIN_HOLE_X + r, W / 2 + r))]


def _try(base_cells, lid_cells, L, W, towers, keep, band, mount, labels, channel):
    """Pack both halves into one bin; each half takes the first policy that fits it."""
    obstacles = _tower_keepouts(L, W, towers, keep) if towers else []
    pb = None
    for policy in POLICIES:
        pb = _pack(base_cells, L, W, obstacles, labels, channel, policy)
        if pb is not None:
            break
    if pb is None:
        return None
    pl: dict = {}
    if lid_cells:
        lid_obs = obstacles + _lid_bands(L, W, band) + _din_obstacles(L, W, mount)
        pl = None
        for policy in POLICIES:
            pl = _pack(lid_cells, L, W, lid_obs, labels, channel, policy)
            if pl is not None:
                break
        if pl is None:
            return None
    return pb, pl


def _search(base_cells, lid_cells, spec: EnclosureSpec, towers: int, keep: float, band: float, mount: str):
    """Try bins of growing area at several aspect ratios; keep the tightest that packs both
    halves. Deterministic: candidates are generated and tried in a fixed order."""
    labels, channel = spec.labels, spec.channel
    cap_l = spec.bed[0] - BED_MARGIN - 2 * spec.wall
    cap_w = spec.bed[1] - BED_MARGIN - 2 * spec.wall
    area_base = sum(_cell_area(c, labels, channel) for c in base_cells)
    area_lid = sum(_cell_area(c, labels, channel) for c in lid_cells)
    area = max(area_base, area_lid, 400.0) + towers * keep * keep
    # the smallest cavity worth making: room for the towers' keep-outs (or a hand-sized void)
    min_side = 2 * keep + 1.0 if towers else MIN_CAVITY
    for c in base_cells + lid_cells:
        min_side = max(min_side, min(min(c.cell_dims(r, labels, channel)) for r in c.rots))
    results = []
    for aspect in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
        for step in range(48):
            s = 1.0 + 0.05 * step
            L = max(math.ceil(math.sqrt(area * s * aspect)), math.ceil(min_side))
            W = max(math.ceil(math.sqrt(area * s / aspect)), math.ceil(min_side))
            if L > cap_l or W > cap_w:
                break
            res = _try(base_cells, lid_cells, float(L), float(W), towers, keep, band, mount, labels, channel)
            if res is not None:
                results.append((L * W, abs(aspect - 1.6), L, W, res))
                break
    if not results:
        return None
    results.sort(key=lambda r: (r[0], r[1], r[3], r[2]))
    _, _, L, W, (pb, pl) = results[0]
    # Tighten: the area steps are 5 % — shave each axis a millimetre at a time while it still packs.
    L, W = float(L), float(W)
    shrunk = True
    while shrunk:
        shrunk = False
        for dl, dw in ((1.0, 0.0), (0.0, 1.0)):
            res = _try(base_cells, lid_cells, L - dl, W - dw, towers, keep, band, mount, labels, channel)
            if res is not None and L - dl >= min_side and W - dw >= min_side:
                L, W, (pb, pl), shrunk = L - dl, W - dw, res, True
    return L, W, pb, pl


def _best_effort(base_cells, lid_cells, spec, towers, keep, band, mount, problems):
    """At the bed's limit: keep the cells that still pack, name the ones that don't."""
    L = spec.bed[0] - BED_MARGIN - 2 * spec.wall
    W = spec.bed[1] - BED_MARGIN - 2 * spec.wall
    kept_base: list[_Cell] = []
    kept_lid: list[_Cell] = []
    for c in base_cells:
        if _try(kept_base + [c], [], L, W, towers, keep, band, mount, spec.labels, spec.channel) is None:
            problems.append(f"{c.label} ({c.comp.name}) doesn't fit the printer's bed with the other parts — left out of the plan.")
        else:
            kept_base.append(c)
    for c in lid_cells:
        if _try(kept_base, kept_lid + [c], L, W, towers, keep, band, mount, spec.labels, spec.channel) is None:
            problems.append(f"{c.label} ({c.comp.name}) doesn't fit the lid with the other parts — left out of the plan.")
        else:
            kept_lid.append(c)
    res = _try(kept_base, kept_lid, L, W, towers, keep, band, mount, spec.labels, spec.channel)
    pb, pl = res if res is not None else ({}, {})
    problems.append("The parts need a shell at the printer bed's limit — check the sizes before printing.")
    return L, W, pb, pl


# ---------------------------------------------------------------------------------------------
# plan_layout
# ---------------------------------------------------------------------------------------------

def plan_layout(spec: EnclosureSpec) -> Layout:
    """Deterministic packing of the spec's items into a two-half shell. Never raises for a fit
    problem — every problem is a plain line in Layout.problems."""
    problems: list[str] = []
    lid_style = spec.lid if spec.lid in LID_STYLES else "screw"
    if spec.lid == "slide":
        problems.append("A slide lid isn't generated yet — built with a screw lid instead.")
    elif spec.lid not in LID_STYLES:
        problems.append(f"Lid style '{spec.lid}' is unknown — built with a screw lid.")
    mount = spec.mount if spec.mount in MOUNTS else "none"
    if spec.mount not in MOUNTS:
        problems.append(f"Mount '{spec.mount}' is unknown — built without a mount.")

    # 1. expand quantities, settle labels (duplicates get 1, 2, …)
    base_labels = [(_safe(it.label or _default_label(it.component)).replace("'", "")[:8] or "PART")
                   for it in spec.items]
    counts: dict[str, int] = {}
    for lbl, it in zip(base_labels, spec.items):
        counts[lbl] = counts.get(lbl, 0) + max(1, int(it.qty))
    seen: dict[str, int] = {}
    cells: list[_Cell] = []
    for idx, (lbl, item) in enumerate(zip(base_labels, spec.items)):
        for n in range(max(1, int(item.qty))):
            if counts[lbl] > 1:
                seen[lbl] = seen.get(lbl, 0) + 1
                label = f"{lbl[:7]}{seen[lbl]}"
            else:
                label = lbl
            cells.append(_classify(item, spec, f"{idx}.{n}", label))
    for c in cells:
        problems.extend(c.problems)
    base_cells = [c for c in cells if not c.on_lid]
    lid_cells = [c for c in cells if c.on_lid]

    # 2. screws, towers, the lid's lip band, then the search
    size_guess = max((max(c.plan_dims[r][0] for r in c.rots) for c in cells), default=40.0)
    screw = "M3" if size_guess >= M3_FROM else "M2"
    band = _r(0.15 + max(1.2, WALL_MAX * 0.6) + FEATURE_AIR)

    def geometry(sz: str) -> tuple[float, float]:
        r = SCREWS[sz]["od"] / 2.0
        inset = r + 0.8
        return inset, inset + r + FEATURE_AIR

    tower_inset, keep = geometry(screw)
    towers = 2 if lid_style == "screw" else 0
    found = _search(base_cells, lid_cells, spec, towers, keep, band, mount)
    if found is not None and towers and (found[0] >= 70.0 or found[1] >= 50.0):
        again = _search(base_cells, lid_cells, spec, 4, keep, band, mount)
        if again is not None:
            towers, found = 4, again
    if found is not None and screw == "M2" and max(found[0], found[1]) >= M3_FROM:
        screw = "M3"
        tower_inset, keep = geometry(screw)
        again = _search(base_cells, lid_cells, spec, towers, keep, band, mount)
        if again is not None:
            found = again
    if found is None:
        found = _best_effort(base_cells, lid_cells, spec, towers, keep, band, mount, problems)
    L_in, W_in, pb, pl = found
    for c in cells:
        pos = (pl if c.on_lid else pb).get(c.uid)
        if pos is not None:
            c.x, c.y, c.rot = pos

    # 3. heights
    base_stack = max((c.standing for c in base_cells if c.x >= 0), default=0.0)
    lid_stack = max((c.standing for c in lid_cells if c.x >= 0), default=0.0)
    if mount == "din":
        lid_stack = max(lid_stack, 6.0)
    base_in = _r(max(base_stack + AIR, BASE_MIN_IN))
    lid_in = _r(max(lid_stack + (0.5 if lid_stack else 0.0), LID_MIN_IN))
    inner_h = _r(base_in + lid_in)
    wall, floor = spec.wall, spec.floor
    outer = (_r(L_in + 2 * wall), _r(W_in + 2 * wall), _r(inner_h + 2 * floor))
    base_depth = floor + base_in
    total_h = outer[2]
    frame = (outer, base_depth, total_h, floor)

    # 4. placements in the plan frame, apertures, screws
    placed: list[Placed] = []
    apertures: list[dict] = []
    for c in cells:
        if c.x < 0:
            continue
        m = c.margins(spec.labels, spec.channel)
        fw, fh = c.plan_dims[c.rot]
        px, py = wall + c.x + m["left"], wall + c.y + m["bottom"]
        pw, ph = fw + 2 * c.slack, fh + 2 * c.slack
        ox, oy = px + c.slack, py + c.slack           # the part's own outline
        Lc, Wc = float(c.comp.length), float(c.comp.width)
        plate_cuts: list[dict] = []
        if c.plate:
            for a in c.comp.apertures:
                if a.face not in ("top", "bottom"):
                    continue
                cut = _plate_cut(a)
                if cut is None:
                    continue
                ax, ay = _plan_point(a.x, a.y, Lc, Wc, c.rot, c.flip)
                entry = {"kind": a.kind, "x": _r(ox + ax), "y": _r(oy + ay), "face": c.plate, "for": c.label}
                entry.update(cut)
                plate_cuts.append(entry)
            if c.kind == "switch" and c.switch:
                shape, sw, sh = SWITCH_SLOTS[c.switch]
                entry = {"kind": "switch", "x": _r(ox + fw / 2), "y": _r(oy + fh / 2), "face": c.plate,
                         "for": c.label, "switch": c.switch}
                entry.update({"d": sw} if shape == "round" else {"w": sw, "h": sh})
                plate_cuts.append(entry)
        if c.wall:
            axis = "y" if c.wall in ("left", "right") else "x"
            centre = (oy + fh / 2) if axis == "y" else (ox + fw / 2)
            if c.kind == "switch":
                if c.switch:
                    shape, sw, sh = SWITCH_SLOTS[c.switch]
                    apertures.append(_wall_entry(c, "switch", centre, floor + c.standing / 2.0, sw, sh, shape,
                                                 frame, switch=c.switch))
            elif c.kind == "clip":
                z = floor + c.standing / 2.0
                if c.panel_d:
                    apertures.append(_wall_entry(c, "panel", centre, z, c.panel_d, c.panel_d, "round", frame))
                elif c.port is not None:
                    w_, h_, _ = _opening_for(c.port, c.comp)
                    apertures.append(_wall_entry(c, c.port.kind, centre, z, w_, h_, "rect", frame))
            elif c.kind == "wallmount" and c.wall_aperture is not None:
                a = c.wall_aperture
                start = oy if axis == "y" else ox
                cut = _plate_cut(a) or {}
                shape = "round" if "d" in cut else "rect"
                sw, sh = (cut.get("d", 0.0), cut.get("d", 0.0)) if shape == "round" else (cut.get("w", 0.0), cut.get("h", 0.0))
                if sw and sh:
                    apertures.append(_wall_entry(c, a.kind, start + a.x, floor + a.y, sw, sh, shape, frame))
            elif c.port is not None:
                # every external port that faces the hinted wall gets its opening (a Pi Zero's
                # HDMI and two micro-USB share one edge)
                for port in _external_ports(c.comp):
                    if _wall_of(_side_normal(port.side, c.rot, c.flip)) == c.wall:
                        apertures.append(_port_entry(c, port, ox, oy, Lc, Wc, axis, frame))
        elif c.port is not None:
            # unhinted: a port gets an opening only when its side landed within reach of a wall
            inside: list[str] = []
            for port in _external_ports(c.comp):
                wall_dir = _wall_of(_side_normal(port.side, c.rot, c.flip))
                edge = {"left": ox - wall, "right": outer[0] - wall - (ox + fw),
                        "bottom": oy - wall, "top": outer[1] - wall - (oy + fh)}.get(wall_dir, 99.0)
                if wall_dir and edge <= PORT_REACH:
                    c.wall = wall_dir
                    apertures.append(_port_entry(c, port, ox, oy, Lc, Wc,
                                                 "y" if wall_dir in ("left", "right") else "x", frame))
                    c.wall = ""
                elif port.kind != "other":
                    inside.append(port.kind)
            if inside:
                problems.append(f"{c.label}: its {', '.join(inside)} port{'s stay' if len(inside) > 1 else ' stays'} "
                                f"inside the box (no wall opening) — give it a face (left/right/top/bottom) to "
                                f"bring the port to a wall.")
        if c.comp.apertures and not c.plate and c.kind not in ("wallmount", "switch") and not c.face:
            kinds = ", ".join(sorted({a.kind for a in c.comp.apertures}))
            problems.append(f"{c.label}: has a {kinds} aperture but no face — nothing was cut for it; "
                            f"set face to front (the show face) or a wall.")
        placed.append(Placed(
            key=c.comp.key, label=c.label, x=_r(px), y=_r(py), w=_r(pw), h=_r(ph), rot=c.rot, face=c.face,
            mount={"wallmount": "pocket", "switch": "pocket"}.get(c.kind, c.kind), on_lid=c.on_lid,
            z_top=_r(c.standing), flip=c.flip, slack=_r(c.slack), rib=c.rib, pocket_h=_r(c.pocket_h),
            omit=c.wall if c.face in WALLS else "", name=_safe(c.comp.name), apertures=tuple(plate_cuts),
            holes=tuple((_r(h.x), _r(h.y)) for h in c.comp.holes) if c.kind == "standoff" else (),
            hole_d=_r(c.comp.holes[0].d) if (c.kind == "standoff" and c.comp.holes) else 0.0,
        ))
    screws: list[dict] = []
    if lid_style == "screw":
        pts = [(tower_inset, tower_inset), (L_in - tower_inset, W_in - tower_inset)]
        if towers >= 4:
            pts += [(L_in - tower_inset, tower_inset), (tower_inset, W_in - tower_inset)]
        for (x, y) in pts:
            screws.append({"x": _r(wall + x), "y": _r(wall + y), "size": screw,
                           "insert": SCREWS[screw]["insert"], "screw": SCREWS[screw]["screw"]})
    if mount == "flat_feet" and (outer[0] < 2 * FOOT_INSET + FOOT_D or outer[1] < 2 * FOOT_INSET + FOOT_D):
        problems.append("The shell is too small for four foot recesses on the back — built without them.")
    return Layout(
        outer=outer, inner=(_r(L_in), _r(W_in), inner_h), wall=wall, floor=floor, placed=tuple(placed),
        apertures=tuple(apertures), screws=tuple(screws), lid=lid_style, problems=tuple(problems),
        split=(base_in, lid_in), lip_h=LIP_H, name=spec.name, mount=mount, channel=spec.channel,
        labels=spec.labels, screw_size=screw,
    )


def _plate_cut(a: Aperture) -> dict | None:
    """The cutter a plate-face aperture gets: kind → (d) or (w, h). None = nothing sensible."""
    d, w, h = float(a.d or 0), float(a.w or 0), float(a.h or 0)
    if a.kind == "lens":
        if d > 0:
            return {"d": _r(d + 1.0), "recess_d": _r(d + 4.0)}
        return {"w": _r(w + 1.0), "h": _r(h + 1.0)} if (w > 0 and h > 0) else None
    if a.kind == "speaker":
        size = d if d > 0 else max(w, h)
        return {"d": _r(size), "grille": True} if size > 0 else None
    if a.kind == "mic":
        return {"d": _r(max(d, 1.5))}
    if a.kind == "screen":
        return {"w": _r(w + 1.0), "h": _r(h + 1.0)} if (w > 0 and h > 0) else None
    if a.kind == "led":
        return {"d": _r(d + 0.6)} if d > 0 else None
    if a.kind in ("button", "shaft", "sensor", "vent", "antenna"):
        if d > 0:
            return {"d": _r(d + 0.6)}
        return {"w": _r(w + 0.6), "h": _r(h + 0.6)} if (w > 0 and h > 0) else None
    return None


def _wall_entry(c: _Cell, kind: str, along: float, z: float, w: float, h: float, shape: str,
                frame, note: str = "", switch: str = "") -> dict:
    """One wall opening, plan frame: x = along the wall (left/right walls: the plan y; top/bottom
    walls: the plan x), z = centre height above the front face (lid items are flipped into that
    frame). `halves` says which half carries the cut — both, as a notch, when it straddles the rim."""
    outer, base_depth, total_h, floor = frame
    if c.on_lid:
        z = total_h - z
    # the opening stays inside the walls' height: never into either half's floor plate
    z = max(z, floor + h / 2.0 + 0.2)
    z = min(z, total_h - floor - h / 2.0 - 0.2)
    face = c.wall
    px = 0.0 if face == "left" else (outer[0] if face == "right" else along)
    py = 0.0 if face == "bottom" else (outer[1] if face == "top" else along)
    z0, z1 = z - h / 2.0, z + h / 2.0
    halves = []
    if z0 < base_depth - 0.5:
        halves.append("base")
    if z1 > base_depth + 0.5:
        halves.append("lid")
    entry = {"face": face, "kind": kind, "x": _r(along), "z": _r(z), "w": _r(w), "h": _r(h),
             "for": c.label, "plan": [_r(px), _r(py)], "halves": halves, "shape": shape}
    if shape == "round":
        entry["d"] = _r(w)
    if note:
        entry["note"] = note
    if switch:
        entry["switch"] = switch
    return entry


def _port_entry(c: _Cell, port: Port, ox: float, oy: float, Lc: float, Wc: float, axis: str, frame) -> dict:
    floor = frame[3]
    w, h, zc = _opening_for(port, c.comp)
    px_, py_ = _port_point(port, Lc, Wc)
    qx, qy = _plan_point(px_, py_, Lc, Wc, c.rot, c.flip)
    along = (oy + qy) if axis == "y" else (ox + qx)
    z = floor + c.mount_h + ((c.comp.height - zc) if c.flip else zc)
    note = "" if (port.width and port.height) else \
        "centre height estimated (1.6 mm PCB + half the receptacle) — the opening is generous"
    return _wall_entry(c, port.kind, along, z, w, h, "rect", frame, note=note)


# ---------------------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------------------

def validate(spec: EnclosureSpec, layout: Layout) -> list[str]:
    """Independent checks over a layout: overlaps, out of cavity, taller than the cavity,
    apertures off their wall, the bed, thin walls. Plain lines; empty means the plan is clean.
    Includes layout.problems."""
    out: list[str] = list(layout.problems)
    L, W, H = layout.outer
    wall, floor = layout.wall, layout.floor
    if wall < 1.2:
        out.append(f"Walls of {wall:g} mm print as a single fragile pass — use 1.6 mm or more.")
    if floor < 1.2:
        out.append(f"A {floor:g} mm floor is too thin to print flat — use 1.6 mm or more.")
    if L > spec.bed[0] - BED_MARGIN or W > spec.bed[1] - BED_MARGIN or H > spec.bed[2]:
        out.append(f"The shell ({L:g} × {W:g} × {H:g} mm) exceeds the printer's bed "
                   f"({spec.bed[0]:g} × {spec.bed[1]:g} × {spec.bed[2]:g}, keep {BED_MARGIN:g} mm inside).")
    rects = []
    for p in layout.placed:
        r = p.rect()
        rects.append((p, r))
        if r[0] < wall - 1e-6 or r[1] < wall - 1e-6 or r[2] > L - wall + 1e-6 or r[3] > W - wall + 1e-6:
            out.append(f"{p.label} sits outside the cavity.")
        cavity = layout.split[1] if p.on_lid else layout.split[0]
        if p.z_top > cavity + 1e-6:
            out.append(f"{p.label} stands {p.z_top:g} mm tall — taller than its half's cavity ({cavity:g} mm).")
    for i, (a, ra) in enumerate(rects):
        for b, rb in rects[i + 1:]:
            if a.on_lid == b.on_lid and _overlap(ra, rb):
                out.append(f"{a.label} and {b.label} overlap.")
    for ap in layout.apertures:
        face = ap.get("face")
        who = ap.get("for", "?")
        if face not in WALLS:
            out.append(f"{who}: aperture on '{face}' is not on a wall.")
            continue
        span = L if face in ("top", "bottom") else W
        x, w = float(ap.get("x", 0)), float(ap.get("w", ap.get("d", 0)))
        z, h = float(ap.get("z", 0)), float(ap.get("h", ap.get("d", 0)))
        if x - w / 2 < wall - 1e-6 or x + w / 2 > span - wall + 1e-6:
            out.append(f"{who}: its {ap.get('kind')} opening runs off the {face} wall.")
        if z - h / 2 < floor - 1e-6 or z + h / 2 > H - floor + 1e-6:
            out.append(f"{who}: its {ap.get('kind')} opening runs off the {face} wall's height.")
    seen: set[str] = set()
    uniq = []
    for line in out:
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return uniq


# ---------------------------------------------------------------------------------------------
# layout_json (MAKER_FLOW §6)
# ---------------------------------------------------------------------------------------------

def print_origins(layout: Layout) -> dict[str, list[float]]:
    """Where each printed part's SHELL outer bottom-left corner sits in the exported STL/3MF: the
    runner lays build()'s parts along X by their bounding boxes with an 8 mm gap, centred as a
    group, on Z=0 — so a lid with tabs starts its shell one tab-width in from its box."""
    L, W = layout.outer[0], layout.outer[1]
    over = LID_OVERHANG.get(layout.mount, 0.0)
    parts = ["base", "lid"] + (["din_clip"] if layout.mount == "din" else [])
    widths = [L, L + 2 * over] + ([DIN_CLIP_W] if layout.mount == "din" else [])
    total = sum(widths) + PLATE_GAP * (len(widths) - 1)
    x = -total / 2.0
    origins = {}
    for name, w in zip(parts, widths):
        inset = over if name == "lid" else 0.0
        origins[name] = [_r(x + inset), _r(-(W if name != "din_clip" else DIN_CLIP_W) / 2.0)]
        x += w + PLATE_GAP
    return origins


def layout_json(layout: Layout) -> dict:
    """The §6 shape — plus additive keys: `split`, `lip_h`, `mount`, per-component `flip`/`name`/
    `slack`/`rib`/`pocket_h`, per-aperture `plan`/`halves`/`shape`, and `print` (where each printed
    part sits in the exported mesh, which the runner lays out along X)."""
    comps = []
    for p in layout.placed:
        comps.append({
            "key": p.key, "label": p.label, "x": p.x, "y": p.y, "w": p.w, "h": p.h, "rot": p.rot,
            "face": p.face, "mount": p.mount, "on_lid": p.on_lid, "z_top": p.z_top, "flip": p.flip,
            "name": p.name, "slack": p.slack, "rib": p.rib, "pocket_h": p.pocket_h,
            "apertures": [dict(a) for a in p.apertures],
        })
    return {
        "units": "mm", "name": layout.name, "outer": list(layout.outer), "inner": list(layout.inner),
        "wall": layout.wall, "floor": layout.floor, "lid": layout.lid,
        "components": comps, "apertures": [dict(a) for a in layout.apertures],
        "screws": [dict(s) for s in layout.screws], "problems": list(layout.problems),
        "split": list(layout.split), "lip_h": layout.lip_h, "mount": layout.mount,
        "print": {"parts": ["base", "lid"] + (["din_clip"] if layout.mount == "din" else []),
                  "gap": PLATE_GAP, "origins": print_origins(layout), "lid_mirror_x": True,
                  "lid_overhang": LID_OVERHANG.get(layout.mount, 0.0),
                  "note": "origins: each part's shell outer bottom-left corner in the exported mesh "
                          "(parts laid along X by bounding box, centred as a group). The lid is "
                          "authored mirrored in x: a plan point (x, y) sits at lid origin + (L - x, y)."},
    }


# ---------------------------------------------------------------------------------------------
# model_source — the model.py text
# ---------------------------------------------------------------------------------------------

def _mc(x: float, y: float, layout: Layout, lid: bool = False) -> tuple[float, float]:
    """Plan → model coordinates; the lid is mirrored in x."""
    L, W = layout.outer[0], layout.outer[1]
    mx, my = x - L / 2.0, y - W / 2.0
    return (_r(-mx if lid else mx), _r(my))


def _side_model(side: str, lid: bool) -> str:
    """A plan side → the pocket helper's side name in the model frame (x mirrored on the lid)."""
    m = {"left": "left", "right": "right", "bottom": "front", "top": "back"}.get(side, "")
    if lid and m in ("left", "right"):
        m = "right" if m == "left" else "left"
    return m


def _notch_side(p: Placed, layout: Layout) -> str:
    """The closed rib side facing the cavity centre — where the wire notch goes."""
    L, W = layout.outer[0], layout.outer[1]
    cx, cy = p.x + p.w / 2.0, p.y + p.h / 2.0
    dx, dy = L / 2.0 - cx, W / 2.0 - cy
    pull = {"left": -dx, "right": dx, "bottom": -dy, "top": dy}
    for s in sorted(WALLS, key=lambda s: -pull[s]):
        if s != p.omit:
            return s
    return "top"


def _brief(spec: EnclosureSpec, layout: Layout) -> str:
    L, W, H = layout.outer
    base_labels = [p.label for p in layout.placed if not p.on_lid]
    lid_labels = [p.label for p in layout.placed if p.on_lid]
    plate_cuts = [f"{a['kind']} for {p.label}" for p in layout.placed for a in p.apertures]
    wall_cuts = [f"{a['kind']} for {a['for']} ({a['face']} wall)" for a in layout.apertures]
    n_screws = len(layout.screws)
    size = SCREWS[layout.screw_size]
    names = ", ".join(f"{p.label} = {p.name}" for p in layout.placed)
    base_line = (f"base — front shell, prints face down: {len(base_labels)} labelled pocket"
                 f"{'s' if len(base_labels) != 1 else ''} ({', '.join(base_labels) or 'none'})")
    if plate_cuts:
        base_line += "; through the face: " + ", ".join(plate_cuts)
    if wall_cuts:
        base_line += "; wall openings: " + ", ".join(wall_cuts)
    if layout.lid == "screw":
        base_line += (f"; {n_screws} x {layout.screw_size} screw towers ({size['insert']:g} mm heat-set "
                      f"inserts); lip ring on the rim")
    else:
        base_line += "; lip ring on the rim (friction fit)"
    lid_line = "lid — back panel, prints outside-face down: lip rebate at the rim"
    if lid_labels:
        lid_line += f"; {', '.join(lid_labels)} on its inner face"
    if layout.lid == "screw":
        lid_line += f"; {n_screws} x {layout.screw_size} countersunk holes"
    lid_line += {"wall_tabs": "; two wall tabs (4.5 mm holes) on the ends",
                 "strap": "; two strap tabs (22 x 4 slots) on the ends",
                 "din": "; two M3 inserts for the DIN clip",
                 "flat_feet": "; four recesses for stick-on rubber feet"}.get(layout.mount, "")
    parts = [base_line, lid_line]
    if layout.mount == "din":
        parts.append("din_clip — slide-on 35 mm DIN rail clip, prints flat; 2 x M3 countersunk into the lid")
    pairs = []
    for s in layout.screws:
        tx, ty = _mc(s["x"], s["y"], layout)
        hx, hy = _mc(s["x"], s["y"], layout, lid=True)
        pairs.append(f"lid hole ({hx:g}, {hy:g}) <-> tower ({tx:g}, {ty:g})")
    assembly = ("Assembly: seat each part in its labelled pocket (labels read from inside, beside the "
                "pocket), route wires through the rib notches into the trenches")
    if lid_labels:
        verb = "sits in its lid pocket" if len(lid_labels) == 1 else "sit in their lid pockets"
        assembly += f"; {', '.join(lid_labels)} {verb} (a foam pad or a dab of hot glue holds them)"
    assembly += ". Close it: the lid flips about Y so the lip ring seats into its rebate"
    if layout.lid == "screw":
        assembly += (f"; {n_screws} x {size['screw']} countersunk (DIN 965) screws through the lid into the "
                     f"towers' inserts. Mirrored pairing (x -> -x): " + "; ".join(pairs))
    assembly += "."
    title = (f"Design: {_safe(spec.name) or 'Enclosure'} — two-half {layout.lid} shell {L:g} x {W:g} x {H:g} mm around "
             f"{len(layout.placed)} part{'s' if len(layout.placed) != 1 else ''} ({names})")
    lines = [title, "Parts:"] + [f"- {p}" for p in parts] + [
        assembly,
        "Planned by HELIX's enclosure generator from the parts list: sizes come from the component "
        "library; community-measured parts already carry 0.5 mm more room; wall opening heights are "
        "estimated where the library has no connector height.",
    ]
    return "\n".join(lines)


def model_source(spec: EnclosureSpec, layout: Layout) -> str:
    """model.py for the pipeline: helix_parts only (math allowed), brief docstring, parameter
    block, the layout table, one function per part, build() → {"base", "lid"[, "din_clip"]}."""
    L, W, H = layout.outer
    L_in, W_in, _ = layout.inner
    base_in, lid_in = layout.split
    wall, floor = layout.wall, layout.floor
    taken = {"wall", "clearance", "corner_r", "lid_style", "label_deep", "math", "build", "base",
             "lid", "din_clip", "s", "body", "tx", "ty", "sx", "sy", "tab", "plate"}
    idents = {p.label: _ident(p.label, taken) for p in layout.placed}
    out: list[str] = []
    w = out.append
    w('"""' + _brief(spec, layout) + '\n"""')
    w("from helix_parts import *")
    w("")
    w(cadpy.PARAM_START)
    w(f"wall = {_num(wall):<12}# [1.6..{WALL_MAX:g}] wall thickness, mm")
    w(f"clearance = {_num(spec.clearance):<7}# [0.2..1.5] room around every part, mm per side")
    w(f"corner_r = {_num(spec.corner_r):<8}# [1..6] outside corner radius, mm")
    w(f'lid_style = "{layout.lid}"{" " * max(1, 5 - len(layout.lid))}# [screw, snap] how the lid attaches')
    w("label_deep = 0.4     # [0.2..0.8] label engraving depth, mm")
    for p in layout.placed:
        if p.mount == "clip":
            continue
        name = f"{idents[p.label]}_extra = 0.0"
        w(f"{name:<21}# [0..1] extra slack in the {p.label} pocket, mm per side")
    w(cadpy.PARAM_END)
    w("")
    w("# --- Layout --- plan view, mm from the enclosure's outer bottom-left corner; x, y, w, h = the pocket inside")
    w("#  label     part                           x       y       w       h  rot  face    mount     half")
    for p in layout.placed:
        w(f"#  {p.label:<9} {p.name[:28]:<28}{p.x:>8.1f}{p.y:>8.1f}{p.w:>8.1f}{p.h:>8.1f}{p.rot:>5}  "
          f"{(p.face or '-'):<7} {p.mount:<9} {'lid' if p.on_lid else 'base'}")
    for a in layout.apertures:
        w(f"#  opening   {a['kind']} for {a['for']}: {a['face']} wall, {a['w']:g} x {a['h']:g} at x={a['x']:g} z={a['z']:g}"
          + (" (" + a["note"] + ")" if a.get("note") else ""))
    w("")
    w(f"IN_L, IN_W = {_num(L_in)}, {_num(W_in)}      # the cavity, mm (plan)")
    w(f"FLOOR = {_num(floor)}                  # plate face thickness, mm")
    w(f"BASE_IN, LID_IN = {_num(base_in)}, {_num(lid_in)}    # cavity depth above each half's floor")
    w(f"CLEAR0 = {_num(spec.clearance)}                 # the planned clearance (the slider moves relative to it)")
    w(f"RIB = {_num(RIB)}")
    w(f"LIP_H = {_num(layout.lip_h)}")
    w(f"LABEL_SIZE = {_num(LABEL_SIZE)}")
    if layout.mount == "wall_tabs":
        w(f"TAB_HOLE = {_num(TAB_HOLE)}")
    w("TOWER_H = BASE_IN + LID_IN - 0.5   # the tower formula: 0.5 shy of the lid's inner floor")
    towers_m = [_mc(s["x"], s["y"], layout) for s in layout.screws]
    w("TOWERS = (" + ", ".join(f"({_num(x)}, {_num(y)})" for x, y in towers_m) + ("," if len(towers_m) == 1 else "") + ")")
    w("")
    w("")
    w("def _r_in():")
    w('    """The cavity\'s corner radius: the outside radius less the wall, never sharp."""')
    w("    return max(0.6, corner_r - wall)")
    w("")
    w("")
    w("def _slack(planned, extra):")
    w('    """A pocket\'s per-side room: the planned value moved by the clearance slider and the')
    w("    pocket's own extra, clamped so a pocket can never grow into its neighbours or the wall.\"\"\"")
    w(f"    return planned + max(-0.4, min({_num(SLACK_GROW)}, (clearance - CLEAR0) + extra))")
    w("")
    w("")
    w("def _label(body, text, x, y, z):")
    w('    """Deboss a label into the floor at (x, y), reading from inside."""')
    w("    return body - Pos(x, y, z - label_deep) * deboss_text(text, LABEL_SIZE, label_deep)")
    w("")
    w("")
    w("def _flip(part):")
    w('    """A face-down (or lid-mirrored) feature: mirrored in x about the part\'s own centre."""')
    w("    return mirror(part, about=Plane.YZ)")
    w("")
    w("")
    for p in layout.placed:
        if p.mount != "clip":
            _emit_part_fn(w, p, layout, idents[p.label])
    _emit_base(w, layout, idents)
    _emit_lid(w, layout, idents)
    if layout.mount == "din":
        _emit_din_clip(w)
    w("def build():")
    if layout.mount == "din":
        w('    return {"base": base(), "lid": lid(), "din_clip": din_clip()}')
    else:
        w('    return {"base": base(), "lid": lid()}')
    return "\n".join(out) + "\n"


def _emit_part_fn(w, p: Placed, layout: Layout, ident: str) -> None:
    lid = p.on_lid
    cx, cy = _mc(p.x + p.w / 2.0, p.y + p.h / 2.0, layout, lid=lid)
    fw, fh = _r(p.w - 2 * p.slack), _r(p.h - 2 * p.slack)
    part_h = p.z_top - p.pocket_h if p.mount in ("standoff", "rails") else p.z_top
    w(f"def {ident}_mount():")
    w(f'    """{p.label} — {p.name}, {fw:g} x {fh:g} x {part_h:g} mm{", face-down" if p.flip else ""} ({p.mount}).')
    w(f'    Centred at ({cx:g}, {cy:g}) on the {"lid" if lid else "base"}, rising from the floor."""')
    w(f"    s = _slack({_num(p.slack)}, {ident}_extra)")
    z = "FLOOR - 0.01"
    if p.mount in ("standoff", "rails"):
        # The board's numbers are written INTO the design (an inline BoardSpec), so model.py stays a
        # faithful record of what was planned even if the seeded catalog moves later.
        helper = "standoffs_for" if p.mount == "standoff" else "side_rails"
        expr = _orient(f"{helper}({_inline_board(p)}, {_num(p.pocket_h)} + 0.01)", p, lid)
        w(f"    return Pos({cx:g}, {cy:g}, {z}) * {expr}")
    elif p.mount == "ring":
        w(f"    ring = Cylinder({_num(fw / 2)} + s + RIB, {_num(p.pocket_h)} + 0.01, align=(Align.CENTER, Align.CENTER, Align.MIN))")
        w(f"    ring -= Pos(0, 0, -1) * Cylinder({_num(fw / 2)} + s, {_num(p.pocket_h)} + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))")
        w(f"    return Pos({cx:g}, {cy:g}, {z}) * ring")
    elif p.mount == "bay":
        side = _side_model(_notch_side(p, layout), lid)
        w(f'    return Pos({cx:g}, {cy:g}, {z}) * battery_bay({_num(fw)}, {_num(fh)}, {_num(p.pocket_h)} + 0.01, RIB, s, side="{side}")')
    else:
        omit = _side_model(p.omit, lid) if p.omit else ""
        tail = f', omit="{omit}"' if omit else ""
        w(f"    return Pos({cx:g}, {cy:g}, {z}) * pocket({_num(fw)}, {_num(fh)}, {_num(p.pocket_h)} + 0.01, RIB, s{tail})")
    w("")
    w("")


def _orient(expr: str, p: Placed, lid: bool) -> str:
    """Rotate/flip a board-centred helper into the placement. The lid is authored mirrored, and a
    mirror conjugates the rotation: mirror(Rot(r) * F) == Rot(-r) * mirror(F)."""
    rot = p.rot
    if p.flip != lid:
        expr = f"_flip({expr})"
    if lid:
        rot = (-rot) % 360
    if rot:
        expr = f"Rot(0, 0, {rot}) * {expr}"
    return expr


def _inline_board(p: Placed) -> str:
    """A BoardSpec literal for the part as the library sees it: the UNROTATED outline (the helper
    is rotated afterwards) with its holes in the component frame."""
    L, W = (p.h, p.w) if p.rot in (90, 270) else (p.w, p.h)
    holes = "(" + ", ".join(f"({_num(x)}, {_num(y)})" for x, y in p.holes) + ("," if len(p.holes) == 1 else "") + ")"
    name = p.name.replace("\\", "").replace('"', "'")
    return (f'BoardSpec("{name}", {_num(L - 2 * p.slack)}, {_num(W - 2 * p.slack)}, {holes}, '
            f'{_num(p.hole_d)}, {_num(p.z_top - p.pocket_h)})')


def _emit_cut_plate(w, entry: dict, layout: Layout, lid: bool) -> None:
    x, y = _mc(entry["x"], entry["y"], layout, lid=lid)
    kind = entry.get("kind")
    at = f"Pos({x:g}, {y:g}, 0)"
    if entry.get("grille"):
        w(f"    body -= {at} * grille({_num(entry['d'])}, depth=FLOOR + 1)  # {entry['for']}")
    elif kind == "lens" and "d" in entry:
        w(f"    body -= {at} * lens_bore({_num(entry['d'])}, FLOOR + 1, recess_d={_num(entry.get('recess_d', 0))}, "
          f"recess_h={_num(PLATE_RECESS)})  # {entry['for']}")
    elif kind == "mic":
        w(f"    body -= {at} * mic_hole({_num(entry['d'])}, depth=FLOOR + 1)  # {entry['for']}")
    elif kind == "switch":
        w(f'    body -= {at} * switch_slot("{entry["switch"]}", depth=FLOOR + 1)  # {entry["for"]}')
    elif "d" in entry:
        w(f"    body -= {at} * led_window({_num(entry['d'])}, depth=FLOOR + 1)  # {entry['for']} {kind}")
    else:
        w(f"    body -= {at} * screen_window({_num(entry['w'])}, {_num(entry['h'])}, 1.0, depth=FLOOR + 1)  # {entry['for']} {kind}")


def _emit_wall_cuts(w, layout: Layout, half: str) -> None:
    """The wall openings this half carries: enclosed slots, or notches through the rim."""
    L, W, H = layout.outer
    base_in, lid_in = layout.split
    floor = layout.floor
    depth = floor + (base_in if half == "base" else lid_in)
    lid = half == "lid"
    for a in layout.apertures:
        if half not in a["halves"]:
            continue
        face = a["face"]
        z, h = float(a["z"]), float(a["h"])
        z0, z1 = z - h / 2.0, z + h / 2.0
        if lid:
            z0, z1 = H - z1, H - z0
        z0 = max(z0, floor + 0.2)
        notch = z1 >= depth - 1.0
        along, wd = float(a["x"]), float(a["w"])
        if face in ("left", "right"):
            _, my = _mc(0.0, along, layout, lid=lid)
            sign = -1 if (face == "left") != lid else 1
            xexpr, yexpr, rot = f"{sign} * (IN_L / 2 + wall / 2)", f"{my:g}", "Rot(0, 0, 90) * "
        else:
            mx, _ = _mc(along, 0.0, layout, lid=lid)
            sign = -1 if face == "bottom" else 1
            xexpr, yexpr, rot = f"{mx:g}", f"{sign} * (IN_W / 2 + wall / 2)", ""
        label = f"{a['kind']} for {a['for']}"
        if notch:
            top = "FLOOR + BASE_IN + LIP_H + 3" if half == "base" else "FLOOR + LID_IN + LIP_H + 3"
            w(f"    # {label}: notch through the {face} wall's rim (the opening reaches the joint)")
            w(f"    body -= Pos({xexpr}, {yexpr}, {_num(z0)}) * {rot}Box({_num(wd)}, wall + 2, ({top}) - {_num(z0)}, "
              f"align=(Align.CENTER, Align.CENTER, Align.MIN))")
        elif a.get("shape") == "round":
            w(f"    # {label}: {a['d']:g} mm hole through the {face} wall")
            w(f"    body -= Pos({xexpr}, {yexpr}, {_num((z0 + z1) / 2)}) * {rot}Rot(90, 0, 0) * "
              f"Cylinder({_num(float(a['d']) / 2)}, wall + 2, align=(Align.CENTER, Align.CENTER, Align.CENTER))")
        else:
            w(f"    # {label}: {wd:g} x {_r(z1 - z0):g} through the {face} wall")
            w(f"    body -= Pos({xexpr}, {yexpr}, {_num((z0 + z1) / 2)}) * {rot}port_slot({_num(wd)}, {_num(z1 - z0)}, wall)")


def _emit_labels_and_notches(w, layout: Layout, half: str) -> None:
    lid = half == "lid"
    for p in layout.placed:
        if p.on_lid != lid or p.mount == "clip":
            continue
        if p.mount in ("pocket", "ring"):
            side = _notch_side(p, layout)
            if p.mount == "ring":
                R = p.w / 2.0 + p.rib
                nx, ny = _WALL_NORMAL[side]
                px = p.x + p.w / 2.0 + nx * (R - p.rib / 2.0)
                py = p.y + p.h / 2.0 + ny * (R - p.rib / 2.0)
            else:
                px = {"left": p.x - p.rib / 2.0, "right": p.x + p.w + p.rib / 2.0}.get(side, p.x + p.w / 2.0)
                py = {"bottom": p.y - p.rib / 2.0, "top": p.y + p.h + p.rib / 2.0}.get(side, p.y + p.h / 2.0)
            mx, my = _mc(px, py, layout, lid=lid)
            span = p.h if side in ("left", "right") else p.w
            width = _r(max(2.0, min(layout.channel, span - 4.0)))
            rot = "Rot(0, 0, 90) * " if side in ("left", "right") else ""
            w(f"    body -= Pos({mx:g}, {my:g}, FLOOR) * {rot}wire_notch({_num(width)}, {_num(p.pocket_h)})  # {p.label} wires out")
        if layout.labels:
            above = p.omit == "bottom"
            lx = p.x + p.w / 2.0
            ly = (p.y + p.h + p.rib + LABEL_BAND / 2.0) if above else (p.y - p.rib - LABEL_BAND / 2.0)
            mx, my = _mc(lx, ly, layout, lid=lid)
            w(f'    body = _label(body, "{p.label}", {mx:g}, {my:g}, FLOOR)')


def _emit_base(w, layout: Layout, idents: dict) -> None:
    w("def base():")
    w('    """The front shell: plate face on the plate, cavity up, every feature rising from the floor."""')
    w("    body = shell_box(IN_L, IN_W, BASE_IN, wall, _r_in(), floor=FLOOR)")
    for p in layout.placed:
        if not p.on_lid and p.mount != "clip":
            w(f"    body += {idents[p.label]}_mount()")
    w('    if lid_style == "screw":')
    w("        for tx, ty in TOWERS:")
    w(f"            body += Pos(tx, ty, FLOOR - 0.01) * screw_boss(TOWER_H + 0.01, {SCREWS[layout.screw_size]['const']})")
    w("    body += Pos(0, 0, FLOOR + BASE_IN - 0.01) * lip_ring(IN_L, IN_W, wall, _r_in(), lip_h=LIP_H)")
    _emit_lip_notches(w, layout)
    plate = [e for p in layout.placed if not p.on_lid for e in p.apertures]
    if plate:
        w("    # through the front face")
        for e in plate:
            _emit_cut_plate(w, e, layout, lid=False)
    _emit_wall_cuts(w, layout, "base")
    _emit_labels_and_notches(w, layout, "base")
    w("    return body")
    w("")
    w("")


def _emit_lip_notches(w, layout: Layout) -> None:
    """Where a LID pocket backs onto a wall, the base's lip (which enters the lid cavity along
    that wall) would land on the pocket's ribs when the halves close. The lip is notched over the
    pocket's span there — the flange below it, seated in the rebate, stays, so the joint keeps
    its seat and the other three walls their lip. (The hand-made IronEye did the same at its
    towers.)"""
    L, W, _ = layout.outer
    wall = layout.wall
    band = _r(0.15 + max(1.2, WALL_MAX * 0.6) + FEATURE_AIR)
    for p in layout.placed:
        if not (p.on_lid and p.omit):
            continue
        r = p.rect()
        # the cut runs from the cavity wall line inward (the ring's cavity-side overhang and the
        # lip) and from just under the ring's bottom upward — below the rim that zone is air, so
        # the wall itself and the flange over it are never touched
        if p.omit == "left":
            x0, x1, y0, y1 = wall, wall + band, r[1] - 1.0, r[3] + 1.0
        elif p.omit == "right":
            x0, x1, y0, y1 = L - wall - band, L - wall, r[1] - 1.0, r[3] + 1.0
        elif p.omit == "bottom":
            x0, x1, y0, y1 = r[0] - 1.0, r[2] + 1.0, wall, wall + band
        else:
            x0, x1, y0, y1 = r[0] - 1.0, r[2] + 1.0, W - wall - band, W - wall
        cx, cy = _mc((x0 + x1) / 2.0, (y0 + y1) / 2.0, layout)
        w(f"    # the lip is notched over {p.label}'s lid pocket on the {p.omit} wall (the flange stays seated)")
        w(f"    body -= Pos({cx:g}, {cy:g}, FLOOR + BASE_IN - 0.02) * Box({_num(x1 - x0)}, {_num(y1 - y0)}, LIP_H + 4, "
          "align=(Align.CENTER, Align.CENTER, Align.MIN))")


def _emit_lid(w, layout: Layout, idents: dict) -> None:
    L, W, H = layout.outer
    w("def lid():")
    w('    """The back panel, authored in ITS print orientation (outside face down) — the plan')
    w("    mirrored in x. Flips about Y to mate: a hole at (+x, y) meets the tower at (-x, y).\"\"\"")
    w("    body = shell_box(IN_L, IN_W, LID_IN, wall, _r_in(), floor=FLOOR)")
    w("    body -= Pos(0, 0, FLOOR + LID_IN - 2.2) * lip_rebate(IN_L, IN_W, wall, _r_in())")
    csk = SCREWS[layout.screw_size]["csk"]
    w('    if lid_style == "screw":')
    w("        for tx, ty in TOWERS:")
    w(f"            body -= Pos(-tx, ty, 0) * csk_hole({_num(csk[0])}, {_num(csk[1])}, FLOOR + 1)")
    for p in layout.placed:
        if p.on_lid and p.mount != "clip":
            w(f"    body += {idents[p.label]}_mount()")
    plate = [e for p in layout.placed if p.on_lid for e in p.apertures]
    if plate:
        w("    # through the back face")
        for e in plate:
            _emit_cut_plate(w, e, layout, lid=True)
    _emit_wall_cuts(w, layout, "lid")
    _emit_labels_and_notches(w, layout, "lid")
    if layout.mount == "wall_tabs":
        w("    # wall tabs: flat on the plate, overlapping 5 mm into the body, holes clear of the wall")
        w("    tab_w = 16.0")
        w("    for sx in (-1.0, 1.0):")
        w("        tab = rbox(tab_w, 16.0, FLOOR, 3.0) - Pos(sx * 3.0, 0, -1) * Cylinder(TAB_HOLE / 2, FLOOR + 2, "
          "align=(Align.CENTER, Align.CENTER, Align.MIN))")
        w("        body += Pos(sx * (IN_L / 2 + wall + tab_w / 2 - 5.0), 0, 0) * tab")
    elif layout.mount == "strap":
        w("    # strap tabs on both ends: flat slotted anchors, overlapping 5 mm into the body")
        w(f"    slot_w, slot_h, margin = {_num(STRAP_SLOT[0])}, {_num(STRAP_SLOT[1])}, 6.0")
        w("    for sx in (-1.0, 1.0):")
        w("        tab = Rot(0, 0, 90) * strap_tab(slot_w, slot_h, FLOOR, margin)")
        w("        body += Pos(sx * (IN_L / 2 + wall + (slot_h + 2 * margin) / 2 - 5.0), 0, 0) * tab")
    elif layout.mount == "din":
        w("    # two M3 inserts on the inner face for the DIN clip's screws (clip holes at the same +/-x)")
        w("    for sx in (-1.0, 1.0):")
        w(f"        body += Pos(sx * {_num(DIN_HOLE_X)}, 0, FLOOR - 0.01) * screw_boss(6.0 + 0.01, INSERT_M3)")
        w(f"        body -= Pos(sx * {_num(DIN_HOLE_X)}, 0, -1) * Cylinder(1.7, FLOOR + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))")
    elif layout.mount == "flat_feet":
        fx, fy = L / 2.0 - FOOT_INSET, W / 2.0 - FOOT_INSET
        if fx > FOOT_D / 2 + 1 and fy > FOOT_D / 2 + 1:
            w(f"    # four {FOOT_D:g} mm x 1 mm recesses on the back for stick-on rubber feet")
            w("    for sx in (-1.0, 1.0):")
            w("        for sy in (-1.0, 1.0):")
            w(f"            body -= Pos(sx * {_num(fx)}, sy * {_num(fy)}, -1) * Cylinder({_num(FOOT_D / 2)}, 2.0, "
              "align=(Align.CENTER, Align.CENTER, Align.MIN))")
    w("    return body")
    w("")
    w("")


def _emit_din_clip(w) -> None:
    w("def din_clip():")
    w('    """Slide-on clip for a 35 mm DIN rail, printed flat: a plate with two side rails whose inward')
    w("    lips hook behind the rail's flanges (45 degree undersides, no supports). Two countersunk M3")
    w("    through the plate into the lid's inserts at (+/-14, 0) — symmetric, so the mirror pairing holds.\"\"\"")
    w(f"    plate = rbox({_num(DIN_CLIP_W)}, {_num(DIN_CLIP_W)}, 2.4, 3.0)")
    w(f"    span = {_num(DIN_CLIP_W - 6.0)}   # the rails stop short of the plate's rounded corners")
    w("    for sy in (-1.0, 1.0):")
    w("        rail = Pos(0, sy * (35.4 / 2 + 1.5), 2.4 - 0.5) * Box(span, 3.0, 5.0, "
      "align=(Align.CENTER, Align.CENTER, Align.MIN))")
    w("        lip = Pos(0, sy * (35.4 / 2 - 0.5), 2.4 + 1.6) * Box(span, 4.0, 2.8, "
      "align=(Align.CENTER, Align.CENTER, Align.MIN))")
    w("        try:")
    w("            lip = chamfer(lip.edges().group_by(Axis.Z)[0].filter_by(Axis.X), 1.5)")
    w("        except Exception:  # noqa: BLE001 — without the chamfer the lip is a 2.5 mm bridge, still printable")
    w("            pass")
    w("        plate = plate + rail + lip")
    w("    for sx in (-1.0, 1.0):")
    w(f"        plate -= Pos(sx * {_num(DIN_HOLE_X)}, 0, 0) * csk_hole(3.4, 6.3, 2.4 + 1)")
    w("    return plate")
    w("")
    w("")


# ---------------------------------------------------------------------------------------------
# The HELIX AR calibration marker
# ---------------------------------------------------------------------------------------------

def calibration_marker_source() -> str:
    """model.py for the printable HELIX AR marker: an 80 × 80 × 3 mm plate whose OUTER square is
    the scale reference; a 2 mm border, four bold corner squares and a centre ring debossed into
    the top face, plus the text "HELIX 80 mm". Prints flat, no supports."""
    return '''"""Design: HELIX AR marker — an 80 x 80 x 3 mm calibration plate; its outer square is exactly 80 mm
Parts:
- plate — 80 x 80 x 3 mm, prints flat on its back; a 2 mm border square whose OUTSIDE edge is the
  80 mm reference, four 12 mm corner squares and a 20 mm centre ring debossed 0.8 mm into the top
  face, "HELIX 80 mm" debossed below the ring
Assembly: none. Lay it flat in the camera view, in the same plane as the parts, and calibrate on its
outer edge (80.0 mm). Run a marker pen over the recesses for contrast if the webcam struggles to see them.
"""
from helix_parts import *

# --- Parameters ---
size = 80.0        # [60..120] outer square, mm — the scale reference
thick = 3.0        # [2..4] plate thickness, mm
border = 2.0       # [1.5..3] border line width, mm
deboss = 0.8       # [0.4..1.2] recess depth, mm
# --- End Parameters ---

C = Align.CENTER
MN = Align.MIN


def plate():
    body = Box(size, size, thick, align=(C, C, MN))
    top = thick - deboss
    # the border: a square line whose outside edge IS the plate edge
    frame = (Box(size + 2, size + 2, deboss + 1, align=(C, C, MN))
             - Box(size - 2 * border, size - 2 * border, deboss + 3, align=(C, C, C)))
    body -= Pos(0, 0, top) * frame
    # four bold corner squares, inset from the border
    sq = 12.0
    off = size / 2 - border - 4.0 - sq / 2
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            body -= Pos(sx * off, sy * off, top) * Box(sq, sq, deboss + 1, align=(C, C, MN))
    # the centre ring
    ring = Cylinder(10.0, deboss + 1, align=(C, C, MN)) - Cylinder(7.0, deboss + 3, align=(C, C, C))
    body -= Pos(0, 0, top) * ring
    # the text, reading from above
    body -= Pos(0, -size / 2 + border + 9.0, top) * deboss_text("HELIX 80 mm", 6.0, deboss)
    return body


def build():
    return plate()
'''
