"""The maker flow's brain (helix/services/maker.py) and its wiring — MAKER_FLOW §7, against fakes.

MakerService composes the real ComponentService, PartsService, BuildService and enclosure generator
with a fake baker (writes the artefacts a bake leaves behind), a fake repo (records commits and
rollbacks), a fake engine (available / compiles / fails on demand) and a fake bus that answers camera
commands the way the shell does. No kernel here — tests/test_enclosure_compile.py compiles the
generator's output; this file pins what the brain does with it: the files it writes, the events it
publishes, the report it reads back, the refusals, the rollbacks, the print sheet, the camera
hand-offs, the registry's offer/dispatch, the fence, and the studio routes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from helix.domain import cadpy
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated, CameraCommandRequested
from helix.domain.models import App, BuildKind
from helix.domain.vocabulary import friendly_tool_label
from helix.ports.cad import CadResult
from helix.services.builds import BuildService
from helix.services.components import ComponentService
from helix.services.conversation import BUILD_TOOLS
from helix.services.maker import MakerService, is_hardware, label_for, normalize_lid, normalize_mount
from helix.services.parts import PartsService
from helix.services.tools import ToolRegistry

# The IronEye list (MAKER_FLOW §8 reference 1) as the model would save it: library keys, face hints,
# a LiPo by its size code, the battery on the lid, and two hardware rows that get no pocket.
IRONEYE = [
    {"name": "XIAO ESP32-S3 Sense", "component": "xiao_esp32s3_sense", "face": "front"},
    {"name": "MAX98357A amp", "component": "max98357a"},
    {"name": "28mm speaker", "component": "speaker_28mm", "face": "front"},
    {"name": "LiPo 603048", "spec": "3.7V 500mAh", "on_lid": True},
    {"name": "TP4056 USB-C charger", "component": "tp4056_usb_c", "face": "left"},
    {"name": "Slide switch SS12D00", "component": "switch_ss12d00", "face": "top"},
    {"name": "M2 screws", "quantity": 4},
    {"name": "USB-C cable", "note": "no pocket"},
]


# ---- fakes --------------------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Clock:
    def now(self):
        return datetime(2026, 9, 4, 18, 0)


class _Repo:
    """Records what the build service asked of git; never shells out."""

    def __init__(self):
        self.commits: list[tuple[Path, str]] = []
        self.discarded: list[Path] = []

    def init(self, ws):
        Path(ws).mkdir(parents=True, exist_ok=True)

    def commit_all(self, ws, message):
        self.commits.append((Path(ws), message))
        return SimpleNamespace(sha="abc123", summary=message, at=datetime(2026, 9, 4))

    def discard_changes(self, ws):
        self.discarded.append(Path(ws))

    def log(self, ws, limit=100):
        return []


class _Baker:
    """Writes what a real bake leaves behind (mesh, meta, viewer page) — or nothing, on demand."""

    def __init__(self, *, mesh=True, warnings=(), grams=31.4):
        self.mesh = mesh
        self.warnings = list(warnings)
        self.grams = grams
        self.prepared: list[Path] = []
        self.baked: list[Path] = []

    def prepare(self, ws):
        self.prepared.append(Path(ws))

    def bake(self, ws):
        ws = Path(ws)
        self.baked.append(ws)
        if not self.mesh:
            return
        (ws / "assets").mkdir(parents=True, exist_ok=True)
        (ws / "assets" / "model.stl").write_bytes(b"solid helix\nendsolid helix\n")
        (ws / "assets" / "model.meta.json").write_text(json.dumps({
            "parts": ["base", "lid"], "bbox_mm": [218.0, 74.0, 22.5], "volume_cm3": 25.3,
            "solid_grams_pla": self.grams, "print_warnings": self.warnings, "problems": [],
        }), encoding="utf-8")
        (ws / "index.html").write_text("<html>viewer</html>", encoding="utf-8")

    def engine_missing(self):
        return False


class _Cad:
    def __init__(self, *, available=True, ok=True):
        self._available = available
        self._ok = ok
        self.compiled: list[Path] = []

    def available(self):
        return self._available

    def install_hint(self):
        return "Holograms are computed by the build123d CAD kernel — free, about a minute to install."

    def compile_stl(self, source, out, *, timeout_s=180.0):
        self.compiled.append(Path(source))
        if self._ok:
            return CadResult(ok=True, output=Path(out), problem=None, detail=None, seconds=3.2)
        return CadResult(ok=False, output=None, problem="The hologram's source couldn't be compiled.",
                         detail="NameError in model.py, line 41: name 'wdth' is not defined", seconds=1.1)


class _Bus:
    """Records events; answers camera commands the way the shell does (synchronously), from a table
    of command → reply (a str, or a callable of the command)."""

    def __init__(self, answers=None):
        self.published: list = []
        self.answers = dict(answers or {})

    def publish(self, event):
        self.published.append(event)
        if isinstance(event, CameraCommandRequested):
            cmd = event.request
            ans = self.answers.get(cmd.command)
            if callable(ans):
                ans = ans(cmd)
            if ans is not None:
                cmd.settle(ans)

    def commands(self):
        return [e.request for e in self.published if isinstance(e, CameraCommandRequested)]


class _Cancel:
    def __init__(self, on=False):
        self._on = on

    def is_set(self):
        return self._on


def _rig(tmp_path, *, baker=None, cad=None, bus=None, rows=IRONEYE, project="IronEye"):
    parts = PartsService(_Store(), clock=lambda: "2026-09-04T18:00")
    if rows:
        parts.save(project, rows)
    repo = _Repo()
    builds = BuildService(tmp_path / "builds", repo, _Clock())
    baker = baker or _Baker()
    bus = bus if bus is not None else _Bus()
    maker = MakerService(ComponentService(parts), parts, builds, baker, repo, bus, cad=cad)
    return SimpleNamespace(parts=parts, repo=repo, builds=builds, baker=baker, bus=bus, maker=maker, cad=cad)


# ---- the helpers --------------------------------------------------------------------------------

def test_labels_prefer_a_part_code_and_never_a_size():
    assert label_for("XIAO ESP32-S3 Sense") == "XIAO"
    assert label_for("Slide switch SS12D00") == "SS12D00"
    assert label_for("TP4056 USB-C charger") == "TP4056"
    assert label_for("MAX98357A amp") == "MAX98357"          # a long code is cut, not the name's 8th char
    assert label_for("28mm speaker") == "speaker"             # the size is never the label
    assert label_for("LiPo 603048") == "LiPo"                 # nor the cell code
    assert label_for("Raspberry Pi Zero 2 W") == "Pi Zero"    # the longest run that fits
    assert label_for("2 x 18650 holder") == "holder"
    assert label_for("") == ""                                # the generator falls back to the category


def test_hardware_rows_are_recognised_by_head_tail_or_note():
    for name in ("M3 screws", "8 x M2 inserts", "22 AWG silicone wire", "hot glue", "speaker wire",
                 "USB-C cable", "40-pin header", "elastic band", "Heat-set inserts M2"):
        assert is_hardware(name), name
    for name in ("LiPo cell with JST lead", "Speaker", "JST-PH connector", "XIAO ESP32-S3 Sense",
                 "TP4056 charger", "Pi Camera v3"):
        assert not is_hardware(name), name
    assert is_hardware("Camera ribbon", "no pocket") and is_hardware("Camera ribbon", "Hardware only")
    assert not is_hardware("Camera ribbon", "dims measured")


def test_lid_and_mount_words_normalise_and_unknown_mounts_are_not_guessed():
    assert normalize_lid("") == "screw" and normalize_lid("snap fit") == "snap" and normalize_lid("Screws") == "screw"
    assert normalize_mount("") == "none" and normalize_mount("hat") == "strap"
    assert normalize_mount("wall tabs") == "wall_tabs" and normalize_mount("DIN rail") == "din"
    assert normalize_mount("rubber feet") == "flat_feet"
    assert normalize_mount("magnets") is None


# ---- design_enclosure: the happy path -----------------------------------------------------------

def test_design_enclosure_writes_the_build_publishes_it_and_reports_the_fit(tmp_path):
    rig = _rig(tmp_path, cad=_Cad())
    progress: list[str] = []
    text = rig.maker.design_enclosure("iron eye", mount="hat", on_progress=progress.append)
    # the report, top to bottom
    assert text.startswith("Designed 'IronEye enclosure': a two-half shell ")
    assert "mm outside (" in text and "screw-down lid" in text and "two strap tabs on the lid" in text
    assert "Pockets (plan view" in text
    for label in ("XIAO (Seeed XIAO ESP32-S3 Sense)", "SS12D00 (", "TP4056 (", "speaker (", "LiPo (", "MAX98357 ("):
        assert label in text, label
    assert "lens bore" in text and "mic hole" in text and "hex grille" in text      # the face cuts
    assert "on the lid" in text                                                     # the battery
    assert "Wall openings:" in text and "left wall: USB-C for TP4056" in text
    assert "top wall: switch slot for SS12D00" in text
    assert "Screws: " in text and "heat-set inserts" in text
    assert "Print: base, lid laid side by side, 218 × 74 × 22.5 mm on the plate, about 31.4 g of PLA" in text
    assert "Print checks: nothing floating" in text
    assert "Left out of the enclosure (hardware, not pocketed parts): M2 screws x4, USB-C cable x1." in text
    assert "Next: check_fit" in text and "print_hologram" in text
    assert progress == ["Planning the layout from the parts list…", "Compiling the shell…"]
    # the files on disk
    ws = rig.builds.workspace("ironeye-enclosure")
    source = (ws / "model.py").read_text(encoding="utf-8")
    assert cadpy.inspect_source(source) == [] and "from helix_parts import *" in source
    layout = json.loads((ws / "assets" / "layout.json").read_text(encoding="utf-8"))
    assert set(layout) >= {"units", "name", "outer", "inner", "wall", "floor", "lid", "components",
                           "apertures", "screws", "problems"}                       # the §6 shape
    assert layout["units"] == "mm" and layout["name"] == "IronEye enclosure" and layout["lid"] == "screw"
    assert len(layout["components"]) == 6 and {c["label"] for c in layout["components"]} == {
        "XIAO", "MAX98357", "speaker", "LiPo", "TP4056", "SS12D00"}
    assert any(c["on_lid"] for c in layout["components"] if c["label"] == "LiPo")
    assert layout["mount"] == "strap"
    # the pipeline was driven in order: prepare → the engine → bake → finalize (a commit)
    assert rig.baker.prepared == [ws] and rig.cad.compiled == [ws / "model.py"]
    assert rig.baker.baked == [ws]
    assert [m for _w, m in rig.repo.commits] == ["scaffold", "build: IronEye enclosure"]
    manifest = json.loads((ws / ".helixbuild.json").read_text(encoding="utf-8"))
    assert manifest["build_kind"] == "model" and manifest["entry_point"] == "index.html"
    assert not (ws / ".building").exists()
    # the menu and the studio hear about it
    created = [e for e in rig.bus.published if isinstance(e, BuildCreated)]
    assert len(created) == 1 and created[0].app.slug == "ironeye-enclosure"
    assert created[0].app.build_kind == BuildKind.MODEL


def test_designing_again_updates_the_same_hologram_and_says_so(tmp_path):
    rig = _rig(tmp_path, cad=_Cad())
    rig.maker.design_enclosure("IronEye")
    text = rig.maker.design_enclosure("IronEye", lid="snap")
    assert text.startswith("Updated 'IronEye enclosure'") and "snap lid" in text
    assert "Screws: none — the lid is a friction fit" in text
    assert [type(e).__name__ for e in rig.bus.published if isinstance(e, (BuildCreated, BuildIterated))] == \
        ["BuildCreated", "BuildIterated"]
    assert len([a for a in rig.builds.list() if a.build_kind == BuildKind.MODEL]) == 1
    assert [m for _w, m in rig.repo.commits] == ["scaffold", "build: IronEye enclosure", "build: IronEye enclosure"]


def test_a_short_name_never_updates_a_longer_hologram(tmp_path):
    # find_model's loose match is for spoken names; a WRITE must hit an exact name or slug, or
    # 'IronEye' would quietly overwrite 'IronEye enclosure'.
    rig = _rig(tmp_path, cad=_Cad())
    rig.maker.design_enclosure("IronEye")
    text = rig.maker.design_enclosure("IronEye", name="IronEye")
    assert text.startswith("Designed 'IronEye'")
    assert {a.slug for a in rig.builds.list()} == {"ironeye-enclosure", "ironeye"}
    # …while the same name SPOKEN differently is the same hologram (case and spacing don't count)
    assert rig.maker.design_enclosure("IronEye", name="iron eye ENCLOSURE").startswith("Updated 'IronEye enclosure'")
    assert len(rig.builds.list()) == 2


def test_option_words_are_honoured_or_named_back_never_guessed(tmp_path):
    rig = _rig(tmp_path, cad=_Cad())
    text = rig.maker.design_enclosure("IronEye", mount="magnets", wall=9)
    assert "Note: Mount 'magnets' isn't one I can build" in text and "no mount." in text
    assert "Note: Walls of 9 mm are outside the 1.6–3.5 mm" in text and "built with 3.5 mm" in text
    assert "3.5 mm walls" in text
    rig2 = _rig(tmp_path / "two", cad=_Cad(), rows=[
        {"name": "XIAO ESP32-S3 Sense", "component": "xiao_esp32s3_sense", "face": "back"},
        {"name": "28mm speaker", "component": "speaker_28mm", "face": "front", "on_lid": True},
        {"name": "LiPo 603048", "quantity": 40},
    ])
    text = rig2.maker.design_enclosure("IronEye")
    assert "'back' is the lid's face, so it sits on the lid" in text
    assert "'front' is the base's face, so it sits in the base" in text
    assert "40 copies is more than an enclosure takes — planned 12" in text


def test_a_row_the_library_does_not_know_can_carry_its_own_size(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(), rows=[
        {"name": "Mystery sensor board", "length": 20, "width": 10, "height": 3},
        {"name": "XIAO ESP32-S3 Sense", "component": "xiao_esp32s3_sense"},
    ])
    text = rig.maker.design_enclosure("IronEye")
    assert text.startswith("Designed")
    line = next(ln for ln in text.splitlines() if ln.startswith("- Mystery (Mystery sensor board): "))
    # 20 × 10 × 3 plus the ad-hoc slack per side (the packer may turn it 90°); the height is read as given
    assert " pocket at (" in line and "in a ribbed pocket" in line and "stands 3 mm" in line
    assert ("22.2 × 12.2" in line) or ("12.2 × 22.2" in line)


# ---- design_enclosure: the refusals -------------------------------------------------------------

def test_unresolved_rows_stop_the_design_and_are_named_with_what_is_missing(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(), rows=[
        {"name": "XIAO ESP32-S3 Sense", "component": "xiao_esp32s3_sense"},
        {"name": "Mystery sensor board"},
        {"name": "Half-measured thing", "length": 20, "width": 10},
        {"name": "Ghost part", "component": "not_a_key"},
    ])
    text = rig.maker.design_enclosure("IronEye")
    assert text.startswith("I can't design the 'IronEye' enclosure yet — these rows have no size:")
    assert "- Mystery sensor board: no library match for 'Mystery sensor board'; no size given" in text
    assert "- Half-measured thing: no library match" in text and "incomplete (2 of length/width/height)" in text
    assert "- Ghost part: 'not_a_key' isn't a library part; no size given" in text
    assert "- XIAO" not in text                                        # the resolved row isn't blamed
    assert "measured with the camera's ruler" in text and "'no pocket'" in text and "Nothing was built." in text
    assert rig.builds.list() == [] and rig.bus.published == [] and rig.baker.baked == []


def test_missing_lists_and_hardware_only_lists_are_refused_plainly(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(), rows=[{"name": "M3 screws"}, {"name": "hot glue"}], project="Bits")
    assert rig.maker.design_enclosure("Bits").startswith("The 'Bits' list has no parts to house — only hardware (M3 screws x1, hot glue x1).")
    text = rig.maker.design_enclosure("Nope")
    assert text.startswith("There's no parts list called 'Nope'") and "Saved lists: Bits." in text
    assert rig.maker.design_enclosure("  ") == "Which project? Name the parts list the enclosure is for."
    assert rig.builds.list() == []


def test_a_name_taken_by_another_kind_is_refused(tmp_path):
    rig = _rig(tmp_path, cad=_Cad())
    app = App.from_request("IronEye enclosure", "a notes app")
    rig.builds.create_workspace(app)
    rig.builds.finalize(app)
    text = rig.maker.design_enclosure("IronEye")
    assert text == "There's already an app called 'IronEye enclosure' — give the enclosure a different name."
    assert rig.baker.baked == []


def test_a_missing_engine_stops_before_anything_is_written(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(available=False))
    text = rig.maker.design_enclosure("IronEye")
    assert text.startswith("Not started — the hologram engine isn't installed")
    assert "install_cad_engine" in text and "design_enclosure again" in text
    assert rig.builds.list() == [] and not (tmp_path / "builds").exists()


# ---- design_enclosure: rollbacks ----------------------------------------------------------------

def test_a_compile_failure_rolls_a_new_build_back_and_says_so(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(ok=False))
    text = rig.maker.design_enclosure("IronEye")
    assert text.startswith("The shell didn't compile: The hologram's source couldn't be compiled. The engine said: NameError")
    assert "Nothing was kept." in text and "parts list is unchanged" in text
    assert rig.builds.list() == [] and not rig.builds.workspace("ironeye-enclosure").exists()
    assert any(isinstance(e, BuildDeleted) and e.slug == "ironeye-enclosure" for e in rig.bus.published)
    assert not any(isinstance(e, BuildCreated) for e in rig.bus.published)
    assert rig.baker.baked == []                       # nothing baked after a failed compile


def test_a_failure_while_updating_restores_the_previous_version(tmp_path):
    cad = _Cad()
    rig = _rig(tmp_path, cad=cad)
    rig.maker.design_enclosure("IronEye")
    cad._ok = False
    text = rig.maker.design_enclosure("IronEye")
    assert "the previous version of the hologram stands" in text
    ws = rig.builds.workspace("ironeye-enclosure")
    assert rig.repo.discarded == [ws] and not (ws / ".building").exists()
    assert len([e for e in rig.bus.published if isinstance(e, BuildIterated)]) == 0
    assert len(rig.builds.list()) == 1


def test_an_engine_that_yields_no_mesh_is_a_failure_too(tmp_path):
    rig = _rig(tmp_path, baker=_Baker(mesh=False), cad=_Cad())
    text = rig.maker.design_enclosure("IronEye")
    assert text.startswith("The shell didn't compile: the engine produced no mesh")
    assert rig.builds.list() == []


# ---- the print sheet ----------------------------------------------------------------------------

def test_the_print_sheet_reads_settings_parts_hardware_and_assembly(tmp_path):
    rig = _rig(tmp_path, cad=_Cad())
    rig.maker.design_enclosure("IronEye", mount="strap")
    sheet = rig.maker.print_sheet("ironeye-enclosure")
    lines = sheet.splitlines()
    assert lines[0] == "Print sheet — IronEye enclosure (Bambu Lab P1S, PLA)"
    assert lines[1].startswith("Settings: 0.2 mm layers, 3 walls, 15 % infill, no supports")
    assert "Parts to print (planned sizes, from the shell recipe):" in sheet
    assert "- base " in sheet and "(prints face down)" in sheet
    assert "- lid " in sheet and "(prints outside-face down)" in sheet
    assert "On the plate (measured on the compiled model): 218 × 74 × 22.5 mm" in sheet
    assert "Material: about 31.4 g of PLA if printed solid" in sheet
    assert "Hardware: " in sheet and "countersunk screws (DIN 965)" in sheet and "heat-set inserts" in sheet
    assert "a 20 mm elastic band through the two strap slots" in sheet
    assert "a foam pad or a dab of hot glue for each lid pocket" in sheet
    assert "Assembly: seat each part in its labelled pocket" in sheet and "Mirrored pairing" in sheet
    assert "Print checks: nothing floating" in sheet
    assert rig.maker.print_sheet("no-such-hologram") == ""


def test_the_print_sheet_turns_supports_on_only_when_overhang_was_measured(tmp_path):
    rig = _rig(tmp_path, baker=_Baker(warnings=["OVERHANG: ≈4.2 cm² of faces steeper than 45° (lid)"]), cad=_Cad())
    rig.maker.design_enclosure("IronEye")
    sheet = rig.maker.print_sheet("ironeye-enclosure")
    assert "supports ON where the slicer asks" in sheet
    assert "Measured print warnings: OVERHANG" in sheet and "Print checks: nothing floating" not in sheet


def test_the_print_sheet_works_for_a_hologram_the_coder_drew(tmp_path):
    rig = _rig(tmp_path, rows=[])
    app = App.from_request("Bracket", "a bracket")
    app.build_kind = BuildKind.MODEL
    ws = rig.builds.create_workspace(app)
    (ws / "model.py").write_text(
        '"""Design: Bracket — a saddle bracket\nParts:\n- plate\n- saddle\n"""\n'
        "from helix_parts import *\n\n# --- Parameters ---\nwidth = 80.0  # [40..200] width\n"
        "# --- End Parameters ---\n\n\ndef build():\n    return Box(width, 40, 5)\n", encoding="utf-8")
    rig.baker.bake(ws)
    rig.builds.finalize(app)
    sheet = rig.maker.print_sheet("bracket")
    assert sheet.startswith("Print sheet — Bracket (Bambu Lab P1S, PLA)")
    assert "Parts to print: base, lid." in sheet            # from the meta, no layout to plan from
    assert "Hardware:" not in sheet
    assert "Assembly: seat the parts, close the lid, drive the screws (see the Parts list)." in sheet


# ---- the camera: check_fit and camera_measure ---------------------------------------------------

def _projecting_bus():
    return _Bus(answers={
        "panel": "Camera panel's open — look, draw, or project whenever you like.",
        "hologram": lambda cmd: (f"Projected {cmd.payload['name']} onto the camera view — the user can drag it "
                                 "into place, scroll to scale it, and shift-drag to tilt it; it then tracks the board."),
    })


def test_check_fit_opens_the_panel_projects_with_ghosts_and_teaches_the_card(tmp_path):
    bus = _projecting_bus()
    rig = _rig(tmp_path, cad=_Cad(), bus=bus)
    rig.maker.design_enclosure("IronEye")
    ok, line = rig.maker.project("iron eye enclosure")
    assert ok
    assert line.startswith("Projected IronEye enclosure onto the camera view")
    assert "Each of its 6 parts is drawn as a ghost pocket" in line
    assert "credit card's long edge" in line and "85.6 mm" in line and "snaps to 1:1" in line
    assert "camera_measure" in line
    cmds = bus.commands()
    assert [c.command for c in cmds] == ["panel", "hologram"]
    assert cmds[0].payload == {"action": "open"}
    assert cmds[1].payload == {"slug": "ironeye-enclosure", "name": "IronEye enclosure"}


def test_check_fit_names_the_missing_hologram_or_the_missing_panel(tmp_path):
    rig = _rig(tmp_path, cad=_Cad(), bus=_projecting_bus())
    rig.maker.design_enclosure("IronEye")
    ok, line = rig.maker.project("Garden Gnome")
    assert not ok and line == "I don't have a hologram called 'Garden Gnome'. Holograms I have: IronEye enclosure."
    silent = _rig(tmp_path / "s", cad=_Cad(), bus=_Bus(answers={"panel": "opened", "hologram": None}))
    silent.maker.design_enclosure("IronEye")
    ok, line = silent.maker.project("IronEye enclosure")
    assert not ok and "didn't answer" in line
    refused = _rig(tmp_path / "r", cad=_Cad(), bus=_Bus(answers={
        "panel": "opened", "hologram": "'IronEye enclosure' has no mesh to project yet — open it in the Studio once so it bakes, or ask me to rebuild it."}))
    refused.maker.design_enclosure("IronEye")
    ok, line = refused.maker.project("IronEye enclosure")
    assert not ok and line.startswith("'IronEye enclosure' has no mesh to project yet")
    nobus = _rig(tmp_path / "n", cad=_Cad(), bus=None)
    nobus.maker._bus = None
    nobus.maker.design_enclosure("IronEye")
    assert nobus.maker.project("IronEye enclosure") == (False, "The camera panel isn't available right now.")


def test_check_fit_on_a_coder_drawn_hologram_says_there_are_no_ghosts(tmp_path):
    rig = _rig(tmp_path, rows=[], bus=_projecting_bus())
    app = App.from_request("Bracket", "a bracket")
    app.build_kind = BuildKind.MODEL
    rig.builds.create_workspace(app)
    rig.builds.finalize(app)
    ok, line = rig.maker.project("Bracket")
    assert ok and "no component layout" in line and "no ghost pockets — only the shell" in line


def test_camera_measure_relays_the_ruler_line_the_cancel_or_the_silence(tmp_path):
    line = "Measured: XIAO 21.1 × 17.6 mm; hole pitch 15.2 mm (0.19 mm/px, card long edge)"
    rig = _rig(tmp_path, bus=_Bus(answers={"measure": line}))
    text = rig.maker.measure("  the XIAO board,   long edge then short  ")
    assert text.startswith(line) and "real millimetres" in text and "length, width, height" in text
    cmd = rig.bus.commands()[-1]
    assert cmd.command == "measure" and cmd.payload == {"prompt": "the XIAO board, long edge then short"}
    cancelled = _rig(tmp_path / "c", bus=_Bus(answers={"measure": "The user cancelled the measurement."}))
    assert cancelled.maker.measure("the mic") == "The user cancelled the measurement."
    quiet = _rig(tmp_path / "q", bus=_Bus())
    text = quiet.maker.measure("the mic", cancel=_Cancel(on=True))    # the turn stopped: no wait
    assert text.startswith("No measurement came back")
    assert quiet.bus.commands()[-1].abandoned
    long_prompt = _rig(tmp_path / "l", bus=_Bus(answers={"measure": line}))
    long_prompt.maker.measure("x" * 400)
    assert len(long_prompt.bus.commands()[-1].payload["prompt"]) == 160


# ---- suggesting ---------------------------------------------------------------------------------

def test_suggest_heads_the_brief_with_the_project_and_names_no_fenced_tool(tmp_path):
    rig = _rig(tmp_path, rows=[])
    text = rig.maker.suggest("IronEye", "vision, hearing and speech; runs on a battery, charges over USB-C")
    assert text.startswith("For IronEye:\n")
    assert "Vision (camera)" in text and "Speaking (amp + speaker)" in text and "Power (battery)" in text
    for fenced in BUILD_TOOLS:
        assert fenced not in text, fenced
    assert not rig.maker.suggest("", "a garden sensor node").startswith("For ")


# ---- the registry -------------------------------------------------------------------------------

class _FakeMaker:
    def __init__(self):
        self.calls: list = []

    def suggest(self, project, needs):
        self.calls.append(("suggest", project, needs))
        return "S"

    def design_enclosure(self, project, *, lid="screw", mount="none", wall=None, name="", on_progress=None):
        self.calls.append(("design", project, lid, mount, wall, name, on_progress is not None))
        return "D"

    def project(self, name):
        self.calls.append(("project", name))
        return True, "P"

    def measure(self, what, *, cancel=None):
        self.calls.append(("measure", what, cancel))
        return "M"

    def print_sheet(self, slug):
        self.calls.append(("sheet", slug))
        return f"Print sheet — {slug} (Bambu Lab P1S, PLA)"


class _Builds:
    def __init__(self, root, *apps):
        self.root = root
        self.apps = list(apps)

    def list(self):
        return list(self.apps)

    def workspace(self, slug):
        return self.root / slug


def test_registry_offers_the_maker_tools_by_what_is_wired_and_dispatches_them():
    maker = _FakeMaker()
    reg = ToolRegistry(forge=None, builds=None, maker=maker)
    names = {s.name for s in reg.specs()}
    assert {"suggest_components", "design_enclosure"} <= names
    assert not ({"check_fit", "camera_measure"} & names)       # the camera needs a bus to answer
    reg = ToolRegistry(forge=None, builds=None, maker=maker, bus=_Bus())
    names = {s.name for s in reg.specs()}
    assert {"suggest_components", "design_enclosure", "check_fit", "camera_measure"} <= names
    bare = {s.name for s in ToolRegistry(forge=None, builds=None, bus=_Bus()).specs()}
    assert not ({"suggest_components", "design_enclosure", "check_fit", "camera_measure"} & bare)
    assert reg.dispatch("suggest_components", {"project": "IronEye", "needs": "vision"}) == "S"
    assert reg.dispatch("design_enclosure", {"project": "IronEye", "lid": "snap", "mount": "hat", "wall": "2.4",
                                             "name": "Hat cam"}, on_progress=lambda s: None) == "D"
    assert reg.dispatch("design_enclosure", {"project": "IronEye", "wall": "thick"}) == "D"
    token = _Cancel()
    assert reg.dispatch("check_fit", {"name": "IronEye enclosure"}) == "P"
    assert reg.dispatch("camera_measure", {"what": "the mic"}, cancel=token) == "M"
    assert maker.calls == [
        ("suggest", "IronEye", "vision"),
        ("design", "IronEye", "snap", "hat", 2.4, "Hat cam", True),
        ("design", "IronEye", "screw", "", None, "", False),
        ("project", "IronEye enclosure"),
        ("measure", "the mic", token),
    ]


def test_the_fence_and_the_spoken_labels_hold_for_the_maker_tools():
    assert {"design_enclosure", "check_fit", "camera_measure"} <= BUILD_TOOLS
    assert "suggest_components" not in BUILD_TOOLS
    for name in ("suggest_components", "design_enclosure", "check_fit", "camera_measure"):
        label = friendly_tool_label(name)
        assert label != "Working…" and "_" not in label, name


def test_the_specs_teach_the_physical_fields_and_the_enclosure_preference():
    reg = ToolRegistry(forge=None, builds=None, maker=_FakeMaker(), parts=SimpleNamespace(),
                       queue=SimpleNamespace(), bus=_Bus())
    specs = {s.name: s for s in reg.specs()}
    props = specs["save_parts"].input_schema["properties"]["items"]["items"]["properties"]
    assert {"component", "length", "width", "height", "face", "on_lid"} <= set(props)
    assert props["face"]["enum"] == ["front", "back", "left", "right", "top", "bottom"]
    assert "design_enclosure" in specs["build_3d_model"].description
    assert "search_amazon" in specs["suggest_components"].description       # prices are a separate read
    assert "credit card" in specs["check_fit"].description and "85.6" in specs["check_fit"].description
    assert "Never type a dimension from memory" in specs["camera_measure"].description
    assert specs["design_enclosure"].input_schema["required"] == ["project"]
    assert specs["suggest_components"].input_schema["required"] == ["needs"]


def test_print_hologram_carries_the_print_sheet_when_it_hands_the_model_over(tmp_path, monkeypatch):
    from helix.adapters import bambu_printer as bp

    app = SimpleNamespace(name="IronEye enclosure", slug="ironeye-enclosure", build_kind=BuildKind.MODEL, is_model=True)
    builds = _Builds(tmp_path, app)
    (tmp_path / "ironeye-enclosure" / "assets").mkdir(parents=True)
    (tmp_path / "ironeye-enclosure" / "assets" / "model.stl").write_bytes(b"solid\n")
    monkeypatch.setattr(bp, "open_in_studio", lambda target: True)
    reg = ToolRegistry(forge=None, builds=builds, bambu=lambda key: None, maker=_FakeMaker())
    out = reg.dispatch("print_hologram", {"name": "IronEye enclosure"})
    assert out.startswith("I've loaded 'IronEye enclosure' into Bambu Studio")
    assert "\n\nPrint sheet — ironeye-enclosure (Bambu Lab P1S, PLA)" in out
    # without a maker (a headless registry) the reply is exactly what it always was
    plain = ToolRegistry(forge=None, builds=builds, bambu=lambda key: None)
    assert "Print sheet" not in plain.dispatch("print_hologram", {"name": "IronEye enclosure"})

    class _Broken(_FakeMaker):
        def print_sheet(self, slug):
            raise RuntimeError("no")

    assert "Print sheet" not in ToolRegistry(forge=None, builds=builds, bambu=lambda key: None,
                                             maker=_Broken()).dispatch("print_hologram", {"name": "IronEye enclosure"})


# ---- the studio routes --------------------------------------------------------------------------

LAYOUT = {"units": "mm", "name": "IronEye", "outer": [115.0, 48.0, 35.0], "inner": [110.0, 43.0, 30.0],
          "wall": 2.5, "floor": 2.0, "lid": "screw",
          "components": [{"key": "xiao_esp32s3_sense", "label": "CAM", "x": 84.0, "y": 12.0, "w": 22.5,
                          "h": 18.5, "rot": 0, "face": "front", "mount": "pocket", "on_lid": False,
                          "z_top": 9.0, "apertures": []}],
          "apertures": [], "screws": [{"x": 5.0, "y": 5.0, "size": "M2", "insert": 3.2}], "problems": []}

MODEL_PY = ('"""Design: IronEye enclosure — two-half screw shell\nParts:\n- base\n- lid\n"""\n'
            "from helix_parts import *\n\n# --- Parameters ---\nwall = 2.0  # [1.6..3.5] wall, mm\n"
            "# --- End Parameters ---\n\n\ndef build():\n    return Box(10, 10, 10)\n")


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _server(root: Path, *, maker=True):
    from helix.api.server import EventHub, build_app

    apps = [SimpleNamespace(slug="ironeye-enclosure", name="IronEye enclosure", build_kind=BuildKind.MODEL)]
    container = SimpleNamespace(
        settings=_Settings(web_token="tok-test"), paths=SimpleNamespace(builds="does-not-exist"),
        builds=_Builds(root, *apps), cad=SimpleNamespace(version=lambda: "0.11.1"),
    )
    if maker:
        container.maker = _FakeMaker()
    shell = SimpleNamespace(snapshot=lambda: {"t": "snapshot"})
    return build_app(container, shell, EventHub(), None), container


def _call(app, method, path, body: bytes = b"", content_type: str | None = None):
    headers = [(b"host", b"127.0.0.1:8737"), (b"x-helix-token", b"tok-test")]
    if content_type:
        headers.append((b"content-type", content_type.encode()))
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    out = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = int(message["status"])
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def test_the_hologram_payload_carries_the_layout_and_the_print_sheet(tmp_path):
    app, container = _server(tmp_path)
    ws = tmp_path / "ironeye-enclosure"
    (ws / "assets").mkdir(parents=True)
    (ws / "model.py").write_text(MODEL_PY, encoding="utf-8")
    (ws / "assets" / "layout.json").write_text(json.dumps(LAYOUT), encoding="utf-8")
    status, raw = _call(app, "GET", "/api/holograms/ironeye-enclosure")
    assert status == 200
    payload = json.loads(raw)
    assert payload["layout"] == LAYOUT
    assert payload["print_sheet"] == "Print sheet — ironeye-enclosure (Bambu Lab P1S, PLA)"
    assert payload["brief"]["title"] == "IronEye enclosure" and payload["params"][0]["name"] == "wall"
    (ws / "assets" / "layout.json").unlink()
    payload = json.loads(_call(app, "GET", "/api/holograms/ironeye-enclosure")[1])
    assert payload["layout"] is None                       # a hologram the coder drew
    # no maker wired (an older container): the payload still opens, with an empty sheet
    app2, _c = _server(tmp_path, maker=False)
    payload = json.loads(_call(app2, "GET", "/api/holograms/ironeye-enclosure")[1])
    assert payload["print_sheet"] == "" and payload["layout"] is None


def test_the_project_route_goes_through_the_maker_brain(tmp_path):
    app, container = _server(tmp_path)
    status, raw = _call(app, "POST", "/api/holograms/ironeye-enclosure/project")
    assert status == 200 and json.loads(raw) == {"ok": True, "line": "P"}
    assert container.maker.calls == [("project", "ironeye-enclosure")]
    app2, _c = _server(tmp_path, maker=False)
    status, _raw = _call(app2, "POST", "/api/holograms/ironeye-enclosure/project")
    assert status == 501
