"""MakerService — the maker flow's brain: from "I want a thing" to a printed enclosure whose every
number came from the component library, a listing, or the camera's ruler — never from memory.

WHY: the IronEye session (2026-09-04) spent four hours choosing parts from chat memory, baking
guessed sizes into model.py, and never checking the fit against anything real. This service is
the seam that makes the flow one conversation: suggest the parts (ComponentService), keep them on
the project's parts list (PartsService), plan and emit the enclosure deterministically
(domain.enclosure), bake it as an ordinary MODEL build (BuildService + ModelBaker + the repo, so
the studio, the AR panel and the printer see a normal hologram), and put it over the camera at
true scale with a ghost pocket per part (the camera panel's commands over the bus).

Texts here reach the model as tool results. suggest() is readable on autonomous runs, so it
names no fenced tool; everything else is fenced (it writes builds or opens the camera) and may
teach the next step by name. Every dimension in a report is read back from the layout or the
baked meta; a height the generator estimated stays marked as estimated, and a size this module
computes from the shell recipe (the print sheet's per-part sizes) is called planned, while the
overall size measured off the compiled mesh is called measured.

Contract: READ_ME/MAKER_FLOW.md §7.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from helix.domain import cadpy
from helix.domain import enclosure as E
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated, CameraCommandRequested
from helix.domain.models import App, BuildKind, slugify
from helix.domain.vocabulary import kind_label
from helix.logging_setup import get_logger
from helix.services.camera import CameraCommand, read_layout

if TYPE_CHECKING:
    from helix.domain.components import Component
    from helix.services.builds import BuildService
    from helix.services.components import ComponentService
    from helix.services.parts import Part, PartsService

_LOG = get_logger("maker")

META_REL = "assets/model.meta.json"
LAYOUT_REL = "assets/layout.json"
STL_REL = "assets/model.stl"

WALL_MIN, WALL_MAX = 1.6, E.WALL_MAX     # the studio's wall slider range — bands and towers are sized for it
MAX_QTY = 12                            # more copies of one part than this is a typo, not a device

# Spoken/typed words → the generator's lid styles and mount options.
_LID_WORDS = {
    "screw": "screw", "screws": "screw", "screwed": "screw", "screw-down": "screw", "screw down": "screw",
    "snap": "snap", "snap-fit": "snap", "snap fit": "snap", "snapfit": "snap", "friction": "snap", "clip": "snap",
    "slide": "slide", "sliding": "slide", "slide-on": "slide",
}
_MOUNT_WORDS = {
    "": "none", "none": "none", "no mount": "none", "no": "none", "loose": "none", "handheld": "none",
    "wall": "wall_tabs", "wall tabs": "wall_tabs", "wall_tabs": "wall_tabs", "wall-tabs": "wall_tabs",
    "tabs": "wall_tabs", "screw tabs": "wall_tabs", "wall mount": "wall_tabs", "wall-mount": "wall_tabs",
    "strap": "strap", "straps": "strap", "elastic": "strap", "band": "strap", "hat": "strap", "wearable": "strap",
    "din": "din", "din rail": "din", "din-rail": "din", "din_rail": "din", "rail": "din",
    "feet": "flat_feet", "flat feet": "flat_feet", "flat_feet": "flat_feet", "flat-feet": "flat_feet",
    "rubber feet": "flat_feet", "desk": "flat_feet", "desktop": "flat_feet",
}
_MOUNT_OUT = {
    "none": "no mount", "wall_tabs": "two wall tabs on the lid", "strap": "two strap tabs on the lid",
    "din": "a DIN-rail clip (a third printed part)", "flat_feet": "four rubber-foot recesses on the back",
}
_PLACE_OUT = {
    "standoff": "on standoffs (its holes match the library drawing)", "rails": "in side rails",
    "pocket": "in a ribbed pocket", "ring": "in a ring", "bay": "in a bay with a lead notch",
    "clip": "panel-mounted through the wall",
}
_KIND_OUT = {
    "usb_c": "USB-C", "micro_usb": "micro-USB", "usb_a": "USB-A", "barrel_5_5": "5.5 mm barrel jack",
    "sd": "SD slot", "hdmi": "HDMI", "audio_3_5": "3.5 mm audio", "antenna": "antenna", "other": "connector",
    "jst_ph": "JST-PH", "jst_xh": "JST-XH", "header": "pin header", "switch": "switch slot",
    "panel": "panel-mount hole", "lens": "lens", "mic": "mic", "speaker": "speaker", "led": "LED",
    "screen": "screen", "button": "button", "sensor": "sensor", "vent": "vent", "shaft": "shaft",
}

# Rows that are hardware or consumables, never a pocketed part. Matched at the START of the name once
# a leading size/quantity token is stripped ("M3 screws", "8 x M2 inserts", "22 AWG wire", "hot glue"),
# or by the LAST word for the things that are named by what they connect ("speaker wire", "USB-C
# cable", "40-pin header") — so "LiPo cell with JST lead" keeps its pocket and "wire" does not get one.
_SIZE_PREFIX = re.compile(
    r"^(?:\d+\s*(?:x|×|pcs?|pieces?)\s*|m\d+(?:x\d+)?\s*|#\d+\s*|\d+(?:\.\d+)?\s*(?:mm|awg|ga|cm|inch|in)\s*"
    r"|heat[- ]?set\s*|hot\s*|socket[- ]head\s*|countersunk\s*|brass\s*|nylon\s*|steel\s*|silicone\s*"
    r"|double[- ]sided\s*|kapton\s*|electrical\s*|jumper\s*|dupont\s*|stranded\s*|solid[- ]core\s*)+"
)
_HARDWARE = re.compile(
    r"^(screws?|bolts?|nuts?|washers?|inserts?|standoffs?|spacers?|wires?|wiring|cables?|jumpers?|solder|flux|"
    r"tape|glue|epoxy|filament|resistors?|capacitors?|diodes?|transistors?|heat[- ]?shrink|zip[- ]?ties?|"
    r"velcro|foam|elastic|band|magnets?|pin ?headers?|header ?pins?|leads?|thread|string|paint)\b"
)
_HARDWARE_TAIL = frozenset({
    "screw", "screws", "bolt", "bolts", "nut", "nuts", "washer", "washers", "insert", "inserts",
    "standoff", "standoffs", "spacer", "spacers", "wire", "wires", "wiring", "cable", "cables",
    "jumper", "jumpers", "solder", "tape", "glue", "epoxy", "filament", "resistor", "resistors",
    "capacitor", "capacitors", "diode", "diodes", "heatshrink", "velcro", "foam", "magnet", "magnets",
    "header", "headers", "thread", "string", "paint",
})
# A row the user (or the model, on their word) marked as needing no room in the box.
_NO_POCKET = re.compile(r"(?i)\bno[- ]pocket\b|\bnot (?:housed|pocketed|in the box)\b|\bhardware\b|\bconsumable\b")
_FAILED_COMPILE = "The shell didn't compile"


def is_hardware(name: str, note: str = "") -> bool:
    """True for a parts-list row that is hardware or a consumable (screws, inserts, wire, glue…) —
    listed on the BOM, never given a pocket. A note reading 'no pocket' (or 'hardware',
    'consumable', 'not housed') says the same thing about any row."""
    if note and _NO_POCKET.search(str(note)):
        return True
    t = " ".join(str(name or "").lower().split())
    t = _SIZE_PREFIX.sub("", t)
    if _HARDWARE.match(t):
        return True
    words = re.split(r"[^a-z0-9]+", t)
    words = [w for w in words if w]
    return bool(words) and words[-1] in _HARDWARE_TAIL


_SIZE_WORD = re.compile(r'^\d+(?:\.\d+)?(?:mm|cm|in|")?$|^\d+x\d+$', re.I)


def _is_code(word: str) -> bool:
    """A part code the user would recognise beside the pocket: it carries a digit (TP4056,
    SS12D00, ESP32-S3) or is an all-caps word of three letters or more (XIAO, PIR)."""
    return (any(ch.isdigit() for ch in word) and len(word) >= 4) or (word.isupper() and len(word) >= 3)


def label_for(name: str) -> str:
    """The short pocket label a row gets (eight characters at most): the first PART CODE in the
    name ("XIAO", "TP4056", "SS12D00", "MAX98357"), else the first run of words that fits
    ("speaker", "Pi Zero", "LiPo"). Sizes and bare numbers ("28mm", "603048") never become a
    label. Debossed beside the pocket and used in every report, so it must be readable, not a
    truncated hash of the name; an empty label lets the generator fall back to the part's
    category (CAM, SPK, BAT…)."""
    raw = re.sub(r"[^A-Za-z0-9 \-]+", " ", str(name or "")).split()
    words = [w for w in raw if not _SIZE_WORD.match(w) and len(w) > 1]   # drops "2", "x", "28mm"
    if not words:
        return raw[0][:8] if raw else ""
    for w in words:
        if _is_code(w):
            if len(w) <= 8:
                return w
            head = w.split("-", 1)[0]
            return head if 3 <= len(head) <= 8 else w[:8]
    best = ""
    for start in range(len(words)):           # the longest run of words that fits; ties → earliest
        out = ""
        for w in words[start:]:
            cand = f"{out} {w}".strip()
            if len(cand) > 8:
                break
            out = cand
        if len(out) > len(best):
            best = out
    return best or words[0][:8]


def normalize_lid(text: str) -> str:
    return _LID_WORDS.get(" ".join(str(text or "").lower().replace("_", " ").split()), "") or "screw"


def normalize_mount(text: str) -> str | None:
    """None when the words don't name a mount option (the caller says so instead of guessing)."""
    t = " ".join(str(text or "").lower().split())
    if t in _MOUNT_WORDS:
        return _MOUNT_WORDS[t]
    t2 = t.replace("-", " ").replace("_", " ")
    for k, v in _MOUNT_WORDS.items():
        if k and (k == t2 or k in t2.split() or k in t2):
            return v
    return None


@dataclass(frozen=True)
class _Row:
    """One parts-list row on its way into the spec."""
    part: "Part"
    component: "Component | None"
    label: str
    face: str
    on_lid: bool
    qty: int
    note: str = ""       # a plain line for the report when something about the row was adjusted


def _kind_word(kind: str) -> str:
    return _KIND_OUT.get(str(kind or ""), str(kind or "opening"))


def _cut_words(a: dict) -> str:
    kind = str(a.get("kind") or "")
    if "d" in a:
        size = f"Ø{float(a['d']):g}"
    else:
        size = f"{float(a.get('w', 0)):g} × {float(a.get('h', 0)):g}"
    if kind == "lens":
        rec = a.get("recess_d")
        return f"lens bore {size}" + (f" (recess Ø{float(rec):g})" if rec else "")
    if kind == "speaker":
        return f"hex grille {size}" if a.get("grille") else f"speaker opening {size}"
    if kind == "mic":
        return f"mic hole {size}"
    if kind == "switch":
        return f"switch slot {size}"
    return f"{_kind_word(kind)} window {size}"


def _g(v, digits: int = 1) -> str:
    try:
        return f"{round(float(v), digits):g}"
    except (TypeError, ValueError):
        return "?"


class MakerService:
    def __init__(self, components: "ComponentService", parts: "PartsService", builds: "BuildService",
                 baker, repo, bus, *, cad=None) -> None:
        self._components = components
        self._parts = parts
        self._builds = builds
        self._baker = baker      # prepare()/bake()/engine_missing() — the container's lazy proxy is fine
        self._repo = repo
        self._bus = bus
        self._cad = cad          # the CadEngine, for the honest compile problem (optional)

    # ----- suggesting -----
    def suggest(self, project: str, needs: str) -> str:
        """The readable "which parts" brief. Names no fenced tool (tests pin it): the persona teaches
        the next step. A project name only heads the text so the model keeps the rows together."""
        text = self._components.suggest(needs)
        proj = " ".join(str(project or "").split())[:60]
        return f"For {proj}:\n{text}" if proj else text

    # ----- designing -----
    def design_enclosure(self, project: str, *, lid: str = "screw", mount: str = "none",
                         wall: float | None = None, name: str = "", on_progress=None) -> str:
        """Plan an enclosure around the project's parts list, emit model.py + assets/layout.json into
        the MODEL build named `name` (default '<project> enclosure'), bake it, commit it, announce it,
        and return the plain fit report. Refuses — with what's missing per row — when a needed row
        has neither a library match nor a size."""
        proj = " ".join(str(project or "").split())
        if not proj:
            return "Which project? Name the parts list the enclosure is for."
        canonical = self._canonical_project(proj)
        rows = self._parts.rows(proj) if canonical else []
        if canonical is None or not rows:
            known = self._parts.projects()
            tail = f" Saved lists: {', '.join(known)}." if known else " No parts lists are saved yet."
            return (f"There's no parts list called '{proj}' to design from — save the parts first "
                    f"(each with its library key, or its size).{tail}")
        # 1. the rows → library parts (or a precise refusal)
        resolved, unresolved = self._components.resolve_parts(canonical)
        by_key = {r.key: c for r, c in resolved}
        items: list[_Row] = []
        skipped: list[str] = []
        missing: list[str] = []
        notes: list[str] = []
        for row in rows:
            if is_hardware(row.name, row.note) or (row.key not in by_key and is_hardware(row.name, row.spec)):
                skipped.append(f"{row.name} x{row.quantity}")
                continue
            comp = by_key.get(row.key)
            if comp is None:
                missing.append(self._missing_line(row))
                continue
            face, on_lid, note = row.face, row.on_lid, ""
            if face == "back" and not on_lid:
                on_lid, note = True, f"{row.name}: 'back' is the lid's face, so it sits on the lid."
            elif face == "front" and on_lid:
                on_lid, note = False, f"{row.name}: 'front' is the base's face, so it sits in the base (not on the lid)."
            qty = max(1, int(row.quantity))
            if qty > MAX_QTY:
                notes.append(f"{row.name}: {qty} copies is more than an enclosure takes — planned {MAX_QTY}.")
                qty = MAX_QTY
            if note:
                notes.append(note)
            items.append(_Row(row, comp, label_for(row.name) or "", face, on_lid, qty))
        if missing:
            lines = [f"I can't design the '{canonical}' enclosure yet — these rows have no size:"]
            lines += [f"- {m}" for m in missing]
            lines.append("Give each one a library part (its component key), or its length, width and height "
                         "in millimetres — read off the listing, or measured with the camera's ruler — "
                         "or note it 'no pocket' if it needs no room in the box (a cable, a fastener); "
                         "then ask again. Nothing was built.")
            return "\n".join(lines)
        if not items:
            return (f"The '{canonical}' list has no parts to house — only hardware ({', '.join(skipped)}). "
                    "Add the boards, battery, and modules to the list first.")
        # 2. the spec
        lid_style = normalize_lid(lid)
        mount_style = normalize_mount(mount)
        if mount_style is None:
            notes.append(f"Mount '{mount}' isn't one I can build (none, wall tabs, strap, DIN rail, flat feet) — built without a mount.")
            mount_style = "none"
        wall_mm = E.EnclosureSpec.wall if wall is None else float(wall)
        if wall is not None and not (WALL_MIN <= wall_mm <= WALL_MAX):
            clamped = min(WALL_MAX, max(WALL_MIN, wall_mm))
            notes.append(f"Walls of {wall_mm:g} mm are outside the {WALL_MIN:g}–{WALL_MAX:g} mm the shell is sized for — built with {clamped:g} mm.")
            wall_mm = clamped
        build_name = " ".join(str(name or "").split())[:60] or f"{canonical} enclosure"
        spec = E.EnclosureSpec(
            name=build_name,
            items=tuple(E.Item(r.component, qty=r.qty, label=r.label, face=r.face, on_lid=r.on_lid) for r in items),
            wall=wall_mm, lid=lid_style, mount=mount_style,
        )
        # 3. the build on disk — an EXACT name (or slug) updates the same hologram; a loose match
        # never does, or 'IronEye' could quietly overwrite 'IronEye enclosure'.
        prior = self.find_model(build_name, loose=False)
        taken = next((a for a in self._builds.list()
                      if a.build_kind != BuildKind.MODEL
                      and (a.slug == slugify(build_name) or a.name.strip().lower() == build_name.lower())), None)
        if taken is not None:
            what = kind_label(taken.build_kind.value)
            article = "an" if what[:1] in "aeiou" else "a"
            return (f"There's already {article} {what} called '{taken.name}' — "
                    f"give the enclosure a different name.")
        if self._cad is not None:
            try:
                available = bool(self._cad.available())
            except Exception:  # noqa: BLE001 — a probing hiccup reads as missing
                available = False
            if not available:
                hint = ""
                try:
                    hint = self._cad.install_hint() or ""
                except Exception:  # noqa: BLE001
                    hint = ""
                return ("Not started — the hologram engine isn't installed, so there is nothing to compile "
                        "the shell with. " + hint + " Offer install_cad_engine (only after the user says yes), "
                        "then call design_enclosure again.").replace("  ", " ")
        if on_progress is not None:
            on_progress("Planning the layout from the parts list…")
        layout = E.plan_layout(spec)
        problems = E.validate(spec, layout)
        source = E.model_source(spec, layout)
        lints = cadpy.inspect_source(source)
        if lints:  # the generator's output is pinned to pass; if it ever doesn't, say so rather than compile
            return f"The generated design failed its own checks ({' '.join(lints)}) — nothing was built."
        request = (f"An enclosure for the {canonical} parts list: "
                   + ", ".join(f"{r.part.name} x{r.qty}" for r in items)
                   + " — planned by HELIX's enclosure generator from the saved list.")
        app = App.from_request(build_name, request)
        app.build_kind = BuildKind.MODEL
        iterating = prior is not None
        if prior is not None:   # the hologram keeps its own name, slug and birthday; only the design moves
            app.slug, app.name, app.created_at = prior.slug, prior.name, prior.created_at
            app.request = prior.request or request
        ws = self._builds.create_workspace(app)
        self._builds.mark_building(app.slug)
        failure: str | None = None
        try:
            (ws / "model.py").write_text(source, encoding="utf-8")
            assets = ws / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            (ws / LAYOUT_REL).write_text(json.dumps(E.layout_json(layout), indent=2), encoding="utf-8")
            for stale in (STL_REL, META_REL):   # a previous design's artefacts must not read as this one's
                try:
                    (ws / stale).unlink()
                except OSError:
                    pass
            self._baker.prepare(ws)
            if on_progress is not None:
                on_progress("Compiling the shell…")
            failure = self._compile(ws)
            if failure is None:
                self._baker.bake(ws)
                if not (ws / STL_REL).is_file():
                    failure = "the engine produced no mesh"
        except Exception as exc:  # noqa: BLE001 — never leave a half-written build live
            _LOG.warning("design_enclosure failed", exc_info=True)
            failure = f"{exc}"
        if failure is not None:
            self._rollback(app, ws, iterating)
            return (f"{_FAILED_COMPILE}: {failure} Nothing was kept"
                    + (" — the previous version of the hologram stands." if iterating else ".")
                    + " Tell the user plainly; the parts list is unchanged.")
        meta = self._meta(ws)
        try:
            app = self._builds.finalize(app)   # manifest + the version commit (repo.commit_all)
        except Exception as exc:  # noqa: BLE001 — the design is baked; a git hiccup is worth one honest line
            _LOG.warning("finalize/commit failed for %s", app.slug, exc_info=True)
            notes.append(f"The version commit didn't go through ({exc}); the files are on disk and the hologram opens.")
            self._builds.clear_building(app.slug)
        if self._bus is not None:
            self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return self._fit_report(app, layout, problems, meta, notes, skipped, iterating)

    # ----- reading back -----
    def find_model(self, name: str, *, loose: bool = True) -> App | None:
        """The hologram the user named — its slug or its name as spoken (case and spacing don't
        count: 'iron eye enclosure' is 'IronEye enclosure'), then, when `loose`, a contains match.
        Writers pass loose=False so a short name can never pick a longer one."""
        target = " ".join(str(name or "").split()).lower()
        want = re.sub(r"[^a-z0-9]+", "", target)
        if not want:
            return None
        slug = slugify(target)
        models = [a for a in self._builds.list() if a.build_kind == BuildKind.MODEL]

        def squash(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

        hit = next((a for a in models if a.slug == slug or squash(a.name) == want), None)
        if hit is None and loose:
            hit = next((a for a in models if want in squash(a.name)), None)
        return hit

    def layout(self, slug: str) -> dict | None:
        """The §6 layout beside a baked mesh, or None."""
        return read_layout(self._builds.workspace(slug))

    def print_sheet(self, slug: str) -> str:
        """The P1S print sheet for a hologram: settings, the parts to print with sizes and grams,
        the screws/inserts the layout calls for, and the assembly order from the design's brief.
        Works for any baked hologram; richer when it was designed from a parts list."""
        ws = self._builds.workspace(slug)
        src = ws / "model.py"
        if not src.is_file():
            return ""
        name = next((a.name for a in self._builds.list() if a.slug == slug), slug)
        source = src.read_text(encoding="utf-8", errors="replace")
        brief = cadpy.parse_brief(source)
        layout = read_layout(ws)
        meta = self._meta(ws)
        warns = [str(w) for w in (meta.get("print_warnings") or [])]
        overhang = [w for w in warns if w.upper().startswith("OVERHANG")]
        lines = [f"Print sheet — {name} (Bambu Lab P1S, PLA)"]
        supports = ("supports ON where the slicer asks — the compiled model measured steep overhang"
                    if overhang else "no supports (every part prints on its flat face)")
        lines.append(f"Settings: 0.2 mm layers, 3 walls, 15 % infill, {supports}. STEP first in Bambu Studio; "
                     "the STL carries every part laid side by side.")
        parts = [str(p) for p in (meta.get("parts") or [])]
        bbox = meta.get("bbox_mm")
        if layout:
            lines.append("Parts to print (planned sizes, from the shell recipe):")
            for line in self._part_sizes(layout):
                lines.append(f"- {line}")
        elif parts:
            lines.append("Parts to print: " + ", ".join(parts) + ".")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 3:
            lines.append(f"On the plate (measured on the compiled model): {_g(bbox[0])} × {_g(bbox[1])} × "
                         f"{_g(bbox[2])} mm all together (bed 256 × 256 × 256).")
        grams = meta.get("solid_grams_pla")
        if grams is not None:
            lines.append(f"Material: about {_g(grams)} g of PLA if printed solid — at 15 % infill a shell "
                         "prints lighter; the slicer's estimate is the one to trust.")
        hardware = self._hardware(layout)
        if hardware:
            lines.append("Hardware: " + "; ".join(hardware) + ".")
        assembly = self._assembly(source, brief)
        if assembly:
            lines.append(f"Assembly: {assembly}")
        if warns:
            lines.append("Measured print warnings: " + " ".join(warns))
        elif bbox:
            lines.append("Print checks: nothing floating, nothing over the bed, no steep overhang.")
        return "\n".join(lines)

    # ----- the camera -----
    def project(self, name: str) -> tuple[bool, str]:
        """check_fit: raise the camera panel if it isn't open, project the named hologram with its
        component layout, and say how the view gets to true scale. (ok, the line for the model)."""
        hit = self.find_model(name)
        if hit is None:
            models = [a.name for a in self._builds.list() if a.build_kind == BuildKind.MODEL]
            have = ", ".join(models[:8]) or "none yet"
            return False, f"I don't have a hologram called '{name}'. Holograms I have: {have}."
        if self._bus is None:
            return False, "The camera panel isn't available right now."
        opened = self._command("panel", {"action": "open"})
        if opened is None:
            return False, ("The camera panel isn't available right now — ask the user to open it with "
                           "the camera button, then try again.")
        reply = self._command("hologram", {"slug": hit.slug, "name": hit.name})
        if reply is None:
            return False, "The camera panel didn't answer — ask the user to open it and try again."
        if not reply.startswith("Projected"):
            return False, reply
        layout = self.layout(hit.slug)
        if layout:
            n = len(layout.get("components") or [])
            ghosts = (f" Each of its {n} part{'s' if n != 1 else ''} is drawn as a ghost pocket inside the "
                      "shell, so the real parts can be laid inside their ghosts to check the fit.")
        else:
            ghosts = (" It has no component layout (it wasn't designed from a parts list), so there are "
                      "no ghost pockets — only the shell.")
        scale = (" Scale: I can't see whether the panel is calibrated. If the hologram isn't sitting at "
                 "true size, have the user switch the panel to Measure mode (the ruler button) and click "
                 "the two ends of a credit card's long edge lying flat beside the parts — it is 85.6 mm — "
                 "and the hologram snaps to 1:1. Once calibrated, a drag in Measure mode reads real "
                 "millimetres; a part the library doesn't know can be measured with camera_measure.")
        return True, reply + ghosts + scale

    def measure(self, what: str, *, cancel=None) -> str:
        """camera_measure: park a measure ask on the panel (opening it if needed) and wait — up to
        the panel's ceiling, cancel-aware — for the user's ruler line."""
        if self._bus is None:
            return "The camera panel isn't available right now."
        prompt = " ".join(str(what or "").split())[:160]
        cmd = CameraCommand("measure", {"prompt": prompt})
        self._bus.publish(CameraCommandRequested(request=cmd))
        reply = cmd.wait(cancel=cancel)
        if reply is None:
            return ("No measurement came back — the user stopped, or the panel was left waiting too "
                    "long. A measurement they send later still reaches you as a message.")
        if reply.startswith("Measured:"):
            return (reply + " These are real millimetres from the calibrated ruler. Save them to the "
                    "part's row (length, width, height) so the enclosure can be designed from them.")
        return reply

    # ----- helpers -----
    def _canonical_project(self, project: str) -> str | None:
        want = re.sub(r"[^a-z0-9]+", "", project.lower())
        if not want:
            return None
        names = self._parts.projects()
        for n in names:
            if re.sub(r"[^a-z0-9]+", "", n.lower()) == want:
                return n
        for n in names:
            squashed = re.sub(r"[^a-z0-9]+", "", n.lower())
            if want in squashed or squashed in want:
                return n
        return None

    @staticmethod
    def _missing_line(row: "Part") -> str:
        dims = [row.length, row.width, row.height]
        given = sum(1 for d in dims if d is not None)
        if row.component:
            what = f"'{row.component}' isn't a library part"
        else:
            what = f"no library match for '{row.name}'"
        if 0 < given < 3:
            what += f"; its size is incomplete ({given} of length/width/height)"
        else:
            what += "; no size given"
        return f"{row.name}: {what}"

    def _compile(self, ws: Path) -> str | None:
        """One kernel run through the engine, so a failure is reported with the compiler's own words
        (the baker, which runs afterwards, serves its exports from this run's cache)."""
        if self._cad is None:
            return None
        try:
            res = self._cad.compile_stl(ws / "model.py", ws / STL_REL)
        except Exception as exc:  # noqa: BLE001
            return f"{exc}"
        if res.ok:
            return None
        problem = (getattr(res, "problem", "") or "the compile failed").strip()
        detail = " ".join(str(getattr(res, "detail", "") or "").split())[:400]
        return f"{problem} {('The engine said: ' + detail) if detail else ''}".strip()

    def _rollback(self, app: App, ws: Path, iterating: bool) -> None:
        try:
            if iterating:
                self._repo.discard_changes(ws)
                self._builds.clear_building(app.slug)
            elif self._builds.delete(app.slug) and self._bus is not None:
                self._bus.publish(BuildDeleted(app.slug))
        except Exception:  # noqa: BLE001 — a rollback hiccup must not mask the real failure
            _LOG.warning("could not roll back %s", app.slug, exc_info=True)

    def _meta(self, ws: Path) -> dict:
        try:
            data = json.loads((ws / META_REL).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _command(self, command: str, payload: dict) -> str | None:
        cmd = CameraCommand(command, payload)
        self._bus.publish(CameraCommandRequested(request=cmd))
        return cmd.wait()

    # ----- the report texts -----
    def _fit_report(self, app: App, layout: E.Layout, problems: list[str], meta: dict,
                    notes: list[str], skipped: list[str], iterating: bool) -> str:
        L, W, H = layout.outer
        li, wi, hi = layout.inner
        lid = {"screw": "screw-down lid", "snap": "snap lid"}.get(layout.lid, f"{layout.lid} lid")
        lines = [
            f"{'Updated' if iterating else 'Designed'} '{app.name}': a two-half shell {_g(L)} × {_g(W)} × {_g(H)} mm "
            f"outside ({_g(li)} × {_g(wi)} × {_g(hi)} inside), {_g(layout.wall)} mm walls, {lid}, "
            f"{_MOUNT_OUT.get(layout.mount, layout.mount)}. It's a hologram now — open in the studio.",
            "Pockets (plan view, mm from the shell's outer bottom-left corner; a pocket is the part plus its slack per side):",
        ]
        for p in layout.placed:
            where = "on the lid" if p.on_lid else "in the base"
            bits = [f"{_g(p.w)} × {_g(p.h)} pocket at ({_g(p.x)}, {_g(p.y)})",
                    _PLACE_OUT.get(p.mount, p.mount), where]
            if p.face:
                bits.append(f"faces {p.face}")
            bits.append(f"stands {_g(p.z_top)} mm")
            cuts = ", ".join(_cut_words(dict(a)) for a in p.apertures)
            if cuts:
                bits.append(f"through the {p.face or 'front'} face: {cuts}")
            lines.append(f"- {p.label} ({p.name}): " + "; ".join(bits))
        if layout.apertures:
            lines.append("Wall openings:")
            for a in layout.apertures:
                size = f"Ø{_g(a['d'])}" if a.get("shape") == "round" else f"{_g(a['w'])} × {_g(a['h'])}"
                note = f" ({a['note']})" if a.get("note") else ""
                lines.append(f"- {a['face']} wall: {_kind_word(a['kind'])} for {a['for']}, {size} mm, "
                             f"{_g(a['x'])} mm along the wall, centre {_g(a['z'])} mm up{note}")
        else:
            lines.append("Wall openings: none.")
        if layout.screws:
            s = layout.screws[0]
            lines.append(f"Screws: {len(layout.screws)} × {s.get('screw', s['size'])} countersunk into "
                         f"{s['size']} heat-set inserts ({_g(s['insert'])} mm bore).")
        else:
            lines.append("Screws: none — the lid is a friction fit on the lip ring.")
        parts = [str(p) for p in (meta.get("parts") or [])]
        bbox = meta.get("bbox_mm")
        grams = meta.get("solid_grams_pla")
        print_bits = []
        if parts:
            print_bits.append(", ".join(parts) + " laid side by side")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 3:
            print_bits.append(f"{_g(bbox[0])} × {_g(bbox[1])} × {_g(bbox[2])} mm on the plate")
        if grams is not None:
            print_bits.append(f"about {_g(grams)} g of PLA if solid (a 15 % infill shell prints lighter)")
        lines.append("Print: " + (", ".join(print_bits) if print_bits else "no measurements came back from the engine") + ".")
        warns = [str(w) for w in (meta.get("print_warnings") or [])]
        if warns:
            lines.append("Print warnings (measured on the compiled model): " + " ".join(warns))
        elif bbox:
            lines.append("Print checks: nothing floating, nothing over the bed, no steep overhang.")
        if problems:
            lines.append("Problems:")
            lines += [f"- {p}" for p in problems]
        else:
            lines.append("Problems: none.")
        for n in notes:
            lines.append(f"Note: {n}")
        if skipped:
            lines.append("Left out of the enclosure (hardware, not pocketed parts): " + ", ".join(skipped) + ".")
        lines.append(
            "Next: check_fit projects it over the camera with a ghost pocket per part (calibrate once on a "
            "credit card); the studio's sliders loosen wall, clearance and any one pocket; 'make the wall "
            "3 mm' is a build_3d_model edit with this same name; print_hologram prints it with the print sheet."
        )
        return "\n".join(lines)

    @staticmethod
    def _part_sizes(layout: dict) -> list[str]:
        """Each printed part's size from the layout — the shell recipe's fixed heights (a base is
        its floor plus its cavity plus the lip ring; a lid its floor plus its cavity; the towers
        reach 0.5 mm shy of the lid's floor). Planned numbers: the assembled-fit tests measure the
        same recipe on the compiled parts, and the print sheet says which is which."""
        try:
            L, W, _H = [float(v) for v in layout["outer"]]
            floor = float(layout.get("floor", 2.0))
            base_in, lid_in = [float(v) for v in (layout.get("split") or (0.0, 0.0))]
            lip_h = float(layout.get("lip_h", E.LIP_H))
        except (KeyError, TypeError, ValueError):
            return []
        over = E.LID_OVERHANG.get(str(layout.get("mount") or ""), 0.0)
        screws = layout.get("screws") or []
        tower_top = floor + base_in + lid_in - 0.5 if screws else 0.0
        ring_top = floor + base_in + 2.0 + lip_h
        out = [f"base {_g(L)} × {_g(W)} × {_g(max(tower_top, ring_top))} mm (prints face down)",
               f"lid {_g(L + 2 * over)} × {_g(W)} × {_g(floor + lid_in)} mm (prints outside-face down)"]
        if layout.get("mount") == "din":
            out.append(f"din_clip {_g(E.DIN_CLIP_W)} mm wide (prints flat)")
        return out

    @staticmethod
    def _hardware(layout: dict | None) -> list[str]:
        if not layout:
            return []
        out: list[str] = []
        screws = layout.get("screws") or []
        if screws:
            s = screws[0]
            n = len(screws)
            out.append(f"{n} × {s.get('screw') or s.get('size', 'M3')} countersunk screws (DIN 965) and "
                       f"{n} × {s.get('size', 'M3')} heat-set inserts ({_g(s.get('insert', 0))} mm bore)")
        mount = str(layout.get("mount") or "none")
        if mount == "wall_tabs":
            out.append("2 × #8 (M4) wood screws through the lid's wall tabs (4.5 mm holes)")
        elif mount == "strap":
            out.append("a 20 mm elastic band through the two strap slots")
        elif mount == "din":
            out.append("2 × M3 countersunk screws and 2 × M3 heat-set inserts for the DIN clip; a 35 mm DIN rail")
        elif mount == "flat_feet":
            out.append("4 × 10 mm stick-on rubber feet")
        if any(c.get("on_lid") for c in (layout.get("components") or [])):
            out.append("a foam pad or a dab of hot glue for each lid pocket")
        return out

    @staticmethod
    def _assembly(source: str, brief: dict) -> str:
        """The 'Assembly: …' sentence of the design's brief — the generator writes one; an LLM-authored
        design may carry one in its Parts list or none at all."""
        text = " ".join(str(brief.get("summary") or "").split())
        m = re.search(r"Assembly:\s*(.*?)(?:\s+Planned by HELIX|$)", text)
        if m and m.group(1).strip():
            return m.group(1).strip()
        doc = re.search(r'"""(.*?)"""', source, re.S)
        if doc:
            m = re.search(r"Assembly:\s*(.*?)(?:\n\s*Planned by HELIX|\Z)", doc.group(1), re.S)
            if m and m.group(1).strip():
                return " ".join(m.group(1).split())
        parts = brief.get("parts") or []
        return ("seat the parts, close the lid, drive the screws (see the Parts list)." if parts else "")
