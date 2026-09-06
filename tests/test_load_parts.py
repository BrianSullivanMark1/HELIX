"""Loading STL files into a hologram (MakerService.load_parts) and its wiring — against fakes.

The real ComponentService/PartsService/BuildService with a fake baker (writes what a bake of loaded
meshes leaves behind), a fake repo, a fake engine and a fake bus. Pins: what lands on disk (parts/,
model.py, a clean assets folder), the events, the folder tag, the report, the refusals, the caps,
the rollbacks, the print sheet for meshes, the registry's offer/dispatch, the fence, the label, and
the prompts that teach it. No kernel here — tests/test_mesh_compile.py compiles the design for real.
"""
from __future__ import annotations

import json
import struct
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from helix.domain import cadpy
from helix.domain import meshes as M
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated
from helix.domain.models import App, BuildKind
from helix.domain.vocabulary import friendly_tool_label
from helix.ports.cad import CadResult
from helix.services import prompts
from helix.services.builds import BuildService
from helix.services.components import ComponentService
from helix.services.conversation import BUILD_TOOLS
from helix.services.maker import MakerService, _glob_files, _source_list
from helix.services.parts import PartsService
from helix.services.tools import ToolRegistry


# ---- fixtures -----------------------------------------------------------------------------------

def _binary_stl(triangles) -> bytes:
    out = bytearray(b"HELIX test mesh".ljust(80, b"\0"))
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        out += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vx, vy, vz in tri:
            out += struct.pack("<3f", vx, vy, vz)
        out += struct.pack("<H", 0)
    return bytes(out)


BOX = _binary_stl([((5.0, 5.0, 0.0), (45.0, 5.0, 0.0), (45.0, 30.0, 0.0)),
                   ((5.0, 30.0, 10.0), (45.0, 30.0, 10.0), (5.0, 5.0, 10.0))])   # 40 × 25 × 10
WEDGE = _binary_stl([((0.0, 0.0, 0.0), (12.0, 0.0, 0.0), (12.0, 8.0, 3.0))])   # 12 × 8 × 3


class _Store:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Clock:
    def now(self):
        return datetime(2026, 9, 6, 15, 0)


class _Repo:
    def __init__(self):
        self.commits = []
        self.discarded = []

    def init(self, ws):
        Path(ws).mkdir(parents=True, exist_ok=True)

    def commit_all(self, ws, message):
        self.commits.append((Path(ws), message))
        return SimpleNamespace(sha="abc123", summary=message, at=datetime(2026, 9, 6))

    def discard_changes(self, ws):
        self.discarded.append(Path(ws))

    def log(self, ws, limit=100):
        return []


class _Baker:
    """Writes what the runner leaves behind for a set of loaded meshes, read off the workspace's
    parts/ folder so the meta names the real labels."""

    def __init__(self, *, mesh=True, plates=2, supports=("thumb5",)):
        self.mesh = mesh
        self.plates = plates
        self.supports = list(supports)
        self.prepared = []
        self.baked = []

    def prepare(self, ws):
        self.prepared.append(Path(ws))

    def bake(self, ws):
        ws = Path(ws)
        self.baked.append(ws)
        if not self.mesh:
            return
        labels = [M.label_of(p.name) for p in sorted((ws / "parts").glob("*.stl"))]
        (ws / "assets").mkdir(parents=True, exist_ok=True)
        (ws / "assets" / "model.stl").write_bytes(b"solid helix\nendsolid helix\n")
        per = max(1, -(-len(labels) // self.plates))
        plates = [labels[i:i + per] for i in range(0, len(labels), per)] or [[]]
        supports = [s for s in self.supports if s in labels]
        (ws / "assets" / "model.meta.json").write_text(json.dumps({
            "parts": labels, "parts_mm": {lb: [40.0, 25.0, 10.0] for lb in labels},
            "bbox_mm": [300.0, 60.0, 10.0], "volume_cm3": 20.0, "solid_grams_pla": 24.8,
            "plates": plates, "mesh_parts": labels, "supports": supports,
            "print_warnings": [f"SUPPORTS: '{s}' (a loaded mesh) measures ≈8.0 cm² of faces steeper "
                               f"than 45° downward — print it with supports on" for s in supports],
            "problems": ["step: skipped — loaded meshes carry no STEP geometry; use the STL or 3MF"],
        }), encoding="utf-8")
        (ws / "index.html").write_text("<html>viewer</html>", encoding="utf-8")

    def engine_missing(self):
        return False


class _Cad:
    def __init__(self, *, available=True, ok=True):
        self._available = available
        self._ok = ok
        self.compiled = []

    def available(self):
        return self._available

    def install_hint(self):
        return "Holograms are computed by the build123d CAD kernel — free, about a minute to install."

    def compile_stl(self, source, out, *, timeout_s=180.0):
        self.compiled.append(Path(source))
        if self._ok:
            return CadResult(ok=True, output=Path(out), problem=None, detail=None, seconds=2.0)
        return CadResult(ok=False, output=None, problem="The hologram's source couldn't be compiled.",
                         detail="ValueError: mesh('x.stl'): there is no x.stl", seconds=0.4)


class _Bus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def _rig(tmp_path, *, baker=None, cad=None, bus=None):
    parts = PartsService(_Store(), clock=lambda: "2026-09-06T15:00")
    repo = _Repo()
    builds = BuildService(tmp_path / "data" / "builds", repo, _Clock())
    baker = baker or _Baker()
    bus = bus if bus is not None else _Bus()
    cad = cad if cad is not None else _Cad()
    maker = MakerService(ComponentService(parts), parts, builds, baker, repo, bus, cad=cad)
    return SimpleNamespace(parts=parts, repo=repo, builds=builds, baker=baker, bus=bus, maker=maker, cad=cad)


def _hand(tmp_path) -> Path:
    """A downloaded section: two STLs and a README, the way a repo folder lands in Downloads."""
    d = tmp_path / "Downloads" / "InMoov" / "Right-Hand"
    d.mkdir(parents=True)
    (d / "thumb5.stl").write_bytes(BOX)
    (d / "Index3.stl").write_bytes(WEDGE)
    (d / "README.md").write_text("print at 100%", encoding="utf-8")
    return d


# ---- loading ------------------------------------------------------------------------------------

def test_a_folder_loads_its_stls_writes_the_design_and_reports(tmp_path):
    r = _rig(tmp_path)
    folder = _hand(tmp_path)
    out = r.maker.load_parts("InMoov Right Hand", [str(folder)], project="InMoov",
                             credit="InMoov by Gael Langevin, CC BY-NC 4.0", on_progress=lambda s: None)
    ws = r.builds.workspace("inmoov-right-hand")
    assert sorted(p.name for p in (ws / "parts").iterdir()) == ["Index3.stl", "thumb5.stl"]
    assert (ws / "parts" / "thumb5.stl").read_bytes() == BOX
    src = (ws / "model.py").read_text(encoding="utf-8")
    assert cadpy.inspect_source(src) == []
    assert '"Index3": "Index3.stl",' in src and '"thumb5": "thumb5.stl",' in src
    assert "CC BY-NC 4.0" in src and "Downloads/InMoov/Right-Hand" in src
    # the brief measured the files: the box is 40 × 25 × 10, the wedge 12 × 8 × 3
    assert "thumb5 — 40 × 25 × 10 mm" in src and "Index3 — 12 × 8 × 3 mm" in src
    assert r.baker.prepared == [ws] and r.baker.baked == [ws]
    assert r.cad.compiled == [ws / "model.py"]
    assert (ws / "assets" / "model.stl").is_file() and (ws / "index.html").is_file()
    assert ("build: InMoov Right Hand") in [m for _, m in r.repo.commits]
    app = next(a for a in r.builds.list() if a.slug == "inmoov-right-hand")
    assert app.build_kind == BuildKind.MODEL and app.project == "InMoov"
    assert "2 STL parts loaded from" in app.request and "CC BY-NC" in app.request
    assert [type(e) for e in r.bus.published] == [BuildCreated]
    assert r.bus.published[0].app.project == "InMoov"
    # the report
    assert out.startswith("Loaded 2 STL parts into 'InMoov Right Hand' (filed under 'InMoov'):")
    assert "every part fits the Bambu P1S bed" in out and "40 × 25 × 10 mm" in out
    assert "laid out on 2 P1S plates — plate 1: Index3; plate 2: thumb5." in out
    assert "print with supports on: thumb5." in out
    assert "about 24.8 g of PLA" in out
    assert "scale is a slider in the studio" in out


def test_files_globs_and_zips_load_too_and_names_are_made_safe_and_unique(tmp_path):
    r = _rig(tmp_path)
    folder = _hand(tmp_path)
    other = tmp_path / "Downloads" / "Left-Hand"
    other.mkdir(parents=True)
    (other / "thumb5.stl").write_bytes(WEDGE)                      # the same name, another folder
    archive = tmp_path / "Downloads" / "release v2 (final).zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("models/Wrist small V4.stl", BOX)
        zf.writestr("models/notes.txt", "ignored")
        zf.writestr("models/sub/", "")
    out = r.maker.load_parts("Mixed", [str(folder / "thumb5.stl"), str(other / "*.stl"), str(archive)])
    ws = r.builds.workspace("mixed")
    assert sorted(p.name for p in (ws / "parts").iterdir()) == ["Wrist_small_V4.stl", "thumb5.stl", "thumb5_2.stl"]
    assert (ws / "parts" / "thumb5.stl").read_bytes() == BOX and (ws / "parts" / "thumb5_2.stl").read_bytes() == WEDGE
    assert out.startswith("Loaded 3 STL parts into 'Mixed':")
    # the origin line names the sources as given (three at most), with forward slashes
    assert "- from " in out and "\\" not in out.split("- from ", 1)[1].splitlines()[0]


def test_sources_come_as_a_list_or_one_string_and_globs_expand_from_their_anchor(tmp_path):
    assert _source_list(None) == [] and _source_list("") == []
    assert _source_list(' "C:/a b/x.stl" ; ~/y.zip\nC:/a b/x.stl') == ["C:/a b/x.stl", "~/y.zip"]
    assert _source_list(["a", "", "a", "b"]) == ["a", "b"]
    folder = _hand(tmp_path)
    got = _glob_files(str(tmp_path / "Downloads" / "**" / "*.stl"))
    assert [p.name for p in got] == ["Index3.stl", "thumb5.stl"]
    assert _glob_files(str(folder / "nothing*.stl")) == []
    assert _glob_files(str(folder / "thumb5.stl")) == [folder / "thumb5.stl"]


def test_refusals_name_what_is_missing_and_write_nothing(tmp_path):
    r = _rig(tmp_path)
    assert r.maker.load_parts("", ["x"]).startswith("What should the hologram be called?")
    assert r.maker.load_parts("Hand", []).startswith("Which files?")
    assert r.maker.load_parts("Hand", None).startswith("Which files?")
    missing = r.maker.load_parts("Hand", [str(tmp_path / "nowhere")])
    assert missing.startswith("I couldn't find any STL files to load.") and "Nothing at" in missing
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "No STL files in" in r.maker.load_parts("Hand", [str(empty)])
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    assert "isn't an STL or a zip" in r.maker.load_parts("Hand", [str(tmp_path / "notes.txt")])
    junk = tmp_path / "broken.stl"
    junk.write_bytes(b"\x00\x01 not a mesh")
    assert "None of those files read as an STL mesh" in r.maker.load_parts("Hand", [str(junk)])
    assert not r.builds.list() and not r.baker.baked and not r.bus.published


def test_helix_s_own_data_folder_is_sealed_but_its_builds_are_not(tmp_path):
    r = _rig(tmp_path)
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "helix_secrets.stl").write_bytes(BOX)               # anything under data/ stays private
    out = r.maker.load_parts("Hand", [str(data / "helix_secrets.stl")])
    assert "inside HELIX's own data folder" in out and "Nothing was built" in out
    own = data / "builds" / "ironeye" / "assets"
    own.mkdir(parents=True)
    (own / "model.stl").write_bytes(BOX)                         # the user's own creations are theirs
    out = r.maker.load_parts("Hand", [str(own / "model.stl")])
    assert out.startswith("Loaded 1 STL part into 'Hand':")


def test_a_name_taken_by_another_kind_and_a_missing_engine_stop_before_anything_is_written(tmp_path):
    r = _rig(tmp_path)
    folder = _hand(tmp_path)
    app = App.from_request("Tip Calculator", "a tip calculator")
    r.builds.create_workspace(app)
    r.builds.finalize(app)
    out = r.maker.load_parts("Tip Calculator", [str(folder)])
    assert out == "There's already an app called 'Tip Calculator' — give the hologram a different name."
    r2 = _rig(tmp_path / "two", cad=_Cad(available=False))
    out = r2.maker.load_parts("Hand", [str(folder)])
    assert out.startswith("Not started — the hologram engine isn't installed")
    assert "install_cad_engine" in out and not r2.builds.list() and not r2.baker.prepared


def test_a_compile_failure_rolls_a_new_build_back_and_a_reload_failure_keeps_the_old_one(tmp_path):
    folder = _hand(tmp_path)
    r = _rig(tmp_path, cad=_Cad(ok=False))
    out = r.maker.load_parts("Hand", [str(folder)])
    assert out.startswith("The parts didn't compile:") and "Nothing was kept." in out
    assert not r.builds.exists("hand") and [type(e) for e in r.bus.published] == [BuildDeleted]
    # a reload that fails leaves the previous version standing (git discards the half-write)
    r2 = _rig(tmp_path / "two")
    r2.maker.load_parts("Hand", [str(folder)], project="InMoov")
    r2.cad._ok = False
    out = r2.maker.load_parts("Hand", [str(folder / "thumb5.stl")])
    assert "the previous version of the hologram stands" in out
    assert r2.repo.discarded == [r2.builds.workspace("hand")]
    assert r2.builds.exists("hand") and not r2.builds.is_building("hand")


def test_reloading_replaces_the_parts_keeps_the_folder_and_says_so(tmp_path):
    r = _rig(tmp_path)
    folder = _hand(tmp_path)
    r.maker.load_parts("Hand", [str(folder)], project="InMoov")
    ws = r.builds.workspace("hand")
    (ws / "assets" / "layout.json").write_text("{}", encoding="utf-8")   # a stale enclosure layout
    out = r.maker.load_parts("hand", [str(folder / "Index3.stl")])       # the slug, no project given
    assert out.startswith("Reloaded 1 STL part into 'Hand' (filed under 'InMoov'):")
    assert [p.name for p in (ws / "parts").iterdir()] == ["Index3.stl"]  # thumb5 is gone
    assert not (ws / "assets" / "layout.json").exists()
    assert [type(e) for e in r.bus.published] == [BuildCreated, BuildIterated]
    assert r.builds.project_of("hand") == "InMoov"
    # a loose match never reloads: 'Han' is a new hologram, not an edit of 'Hand'
    r.maker.load_parts("Han", [str(folder)])
    assert r.builds.exists("han") and r.builds.exists("hand")


def test_scale_lands_in_the_parameter_block_and_is_clamped_with_a_note(tmp_path):
    r = _rig(tmp_path)
    folder = _hand(tmp_path)
    r.maker.load_parts("Half", [str(folder)], scale=0.5)
    (p,) = cadpy.parse_params((r.builds.workspace("half") / "model.py").read_text(encoding="utf-8"))
    assert p.name == "scale" and p.value == "0.5"
    out = r.maker.load_parts("Huge", [str(folder)], scale=9)
    assert "outside the 0.25–2 the studio slider covers — loaded at 2." in out
    (p,) = cadpy.parse_params((r.builds.workspace("huge") / "model.py").read_text(encoding="utf-8"))
    assert p.value == "2"
    r.maker.load_parts("Odd", [str(folder)], scale="x")
    (p,) = cadpy.parse_params((r.builds.workspace("odd") / "model.py").read_text(encoding="utf-8"))
    assert p.value == "1"


def test_the_caps_stop_a_runaway_set_with_a_note(tmp_path, monkeypatch):
    r = _rig(tmp_path)
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(6):
        (folder / f"p{i}.stl").write_bytes(WEDGE)
    monkeypatch.setattr(M, "MAX_PARTS", 4)
    out = r.maker.load_parts("Many", [str(folder)])
    assert out.startswith("Loaded 4 STL parts") and "Stopped at the cap (4 parts" in out
    assert len(list((r.builds.workspace("many") / "parts").iterdir())) == 4


def test_a_part_bigger_than_the_bed_is_named_in_the_report(tmp_path):
    class _Big(_Baker):
        def bake(self, ws):
            super().bake(ws)
            meta_path = Path(ws) / "assets" / "model.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["parts_mm"]["thumb5"] = [300.0, 25.0, 10.0]
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

    r = _rig(tmp_path, baker=_Big())
    out = r.maker.load_parts("Hand", [str(_hand(tmp_path))])
    assert "1 part exceed the Bambu P1S bed (256 mm each way): thumb5 at 300 × 25 × 10 mm" in out
    assert "scale the set down" in out


def test_the_print_sheet_for_loaded_meshes_reads_supports_sizes_and_plates(tmp_path):
    r = _rig(tmp_path)
    r.maker.load_parts("Hand", [str(_hand(tmp_path))])
    sheet = r.maker.print_sheet("hand")
    assert sheet.startswith("Print sheet — Hand (Bambu Lab P1S, PLA)")
    assert "supports ON for thumb5 (measured steep overhang), off for the rest" in sheet
    assert "import the STL in Bambu Studio (they carry no STEP)" in sheet
    assert "Parts to print (measured off the compiled model):" in sheet
    assert "- Index3 — 40 × 25 × 10 mm" in sheet
    assert "Plates (each fits the 256 mm bed): plate 1: Index3; plate 2: thumb5." in sheet
    assert "Measured print warnings: SUPPORTS: 'thumb5'" in sheet


# ---- the wiring ---------------------------------------------------------------------------------

class _FakeMaker:
    def __init__(self):
        self.calls = []

    def load_parts(self, name, sources, *, project="", scale=None, credit="", on_progress=None):
        self.calls.append((name, sources, project, scale, credit))
        return f"Loaded {len(sources or [])} STL parts into '{name}':"

    def suggest(self, project, needs):
        return "brief"

    def design_enclosure(self, project, **kw):
        return "designed"

    def print_sheet(self, slug):
        return ""


def test_the_registry_offers_dispatches_fences_and_labels_the_loader():
    maker = _FakeMaker()
    reg = ToolRegistry(forge=None, builds=None, maker=maker, parts=SimpleNamespace(), queue=SimpleNamespace())
    specs = {s.name: s for s in reg.specs()}
    spec = specs["load_hologram_parts"]
    assert spec.input_schema["required"] == ["name", "sources"]
    assert spec.input_schema["properties"]["sources"]["type"] == "array"
    assert "InMoov" in spec.description and "build_3d_model" in spec.description
    assert "never a path HELIX made up" in spec.description
    out = reg.dispatch("load_hologram_parts", {"name": "InMoov Hand", "sources": ["C:/x/Right-Hand"],
                                               "project": "InMoov", "scale": "0.5", "credit": "InMoov"})
    assert out == "Loaded 1 STL parts into 'InMoov Hand':"
    assert maker.calls == [("InMoov Hand", ["C:/x/Right-Hand"], "InMoov", 0.5, "InMoov")]
    reg.dispatch("load_hologram_parts", {"name": "X", "sources": ["a"], "scale": "big"})
    assert maker.calls[-1][3] is None
    # fenced (it reads the user's disk into a build) and speakable
    assert "load_hologram_parts" in BUILD_TOOLS
    assert friendly_tool_label("load_hologram_parts") == "Loading the parts"
    assert friendly_tool_label("load_hologram_parts", {"name": "InMoov Hand"}) == "Loading InMoov Hand"
    # without a maker the tool is not offered at all
    assert "load_hologram_parts" not in {s.name for s in ToolRegistry(forge=None, builds=None).specs()}


def test_the_persona_and_the_coder_prompt_teach_loading():
    assert "load_hologram_parts" in prompts.CONSOLE_SYSTEM
    assert "InMoov" in prompts.CONSOLE_SYSTEM and "mirrored in the slicer" in prompts.CONSOLE_SYSTEM
    coder = prompts.build_3d_model_prompt("Hand", "add a stand under it")
    assert "LOADED MESHES" in coder and 'mesh("<file>.stl", scale)' in coder
    assert "mesh(file, scale=1.0)" in coder      # the cheat-sheet line rides in with HELIX_LIB_DOC
