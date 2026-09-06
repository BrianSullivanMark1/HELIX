"""domain.meshes — the pure half of loading STL files into a hologram: safe part names, the
print-plate packing the compile worker uses, and the generated model.py (which must pass the
design gate byte-for-byte, since it is compiled without a coder in the loop)."""
from __future__ import annotations

from helix.domain import cadpy
from helix.domain import meshes as M


# ---- names --------------------------------------------------------------------------------------

def test_part_names_are_plain_ascii_stl_file_names_never_paths():
    assert M.safe_part_name("Right-Hand/Auriculaire3.stl") == "Auriculaire3.stl"
    assert M.safe_part_name(r"C:\Users\brian\Downloads\InMoov\thumb 5 (v2).STL") == "thumb_5_v2.stl"
    assert M.safe_part_name("../../etc/passwd") == "passwd.stl"
    assert M.safe_part_name("..") == "part.stl"
    assert M.safe_part_name("") == "part.stl"
    assert M.safe_part_name("héllo wörld.stl") == "h_llo_w_rld.stl"
    assert len(M.safe_part_name("x" * 200 + ".stl")) <= 60
    for name in ("a.stl", "Bolt_entretoise7.stl", "robpart2V4.stl"):
        assert M.safe_part_name(name) == name


def test_repeated_names_get_numbered_case_insensitively():
    assert M.unique_names(["hand.stl", "Hand.stl", "hand.stl", "thumb.stl"]) == [
        "hand.stl", "Hand_2.stl", "hand_3.stl", "thumb.stl"]
    assert M.label_of("robpart2V4.stl") == "robpart2V4"
    assert M.label_of("odd") == "odd"


# ---- the plate layout ---------------------------------------------------------------------------

def test_a_short_row_stays_one_row_on_one_plate():
    slots = M.pack_plates([("a", 100.0, 20.0), ("b", 100.0, 20.0)])
    assert [(s.x, s.y, s.plate) for s in slots] == [(0.0, 0.0, 0), (108.0, 0.0, 0)]
    assert M.plates_of(slots) == [["a", "b"]]


def test_rows_wrap_inside_the_plate_and_stack_back():
    # 100 mm wide parts: two per 244 mm row, the third wraps to a new row 8 mm behind the first
    slots = M.pack_plates([("a", 100.0, 20.0), ("b", 100.0, 30.0), ("c", 100.0, 20.0)])
    assert (slots[0].x, slots[0].y) == (0.0, 0.0)
    assert (slots[1].x, slots[1].y) == (108.0, 0.0)
    assert (slots[2].x, slots[2].y) == (0.0, 38.0)       # row height is the deepest part (30) + gap
    assert all(s.plate == 0 for s in slots)


def test_a_full_plate_starts_the_next_one_to_the_right():
    # 100 × 120 parts: two per row, a second row would reach 128 + 120 > 244 → a new plate
    boxes = [(f"p{i}", 100.0, 120.0) for i in range(6)]
    slots = M.pack_plates(boxes)
    assert [s.plate for s in slots] == [0, 0, 1, 1, 2, 2]
    assert slots[2].x == M.PLATE_MM + M.PLATE_GAP_MM and slots[2].y == 0.0
    assert M.plates_of(slots) == [["p0", "p1"], ["p2", "p3"], ["p4", "p5"]]


def test_an_oversize_part_gets_its_own_row_and_the_layout_never_raises():
    slots = M.pack_plates([("wide", 300.0, 10.0), ("b", 50.0, 10.0), ("tall", 10.0, 300.0), ("c", 10.0, 10.0)])
    assert slots[0].x == 0.0 and slots[1].y == 18.0            # the 300 wide part owns its row
    assert slots[2].plate == 1 and slots[2].y == 0.0           # the 300 deep part starts a plate…
    assert slots[3].plate == 1 and slots[3].x == slots[2].x + 18.0   # …and a small one sits beside it
    assert M.pack_plates([]) == []
    assert M.pack_plates([("neg", -5.0, -5.0)])[0].x == 0.0


def test_the_numbers_are_the_p1s_bed_with_the_runners_margin():
    assert M.BED_MM == 256.0 and M.PLATE_MM == 244.0 and M.PART_GAP_MM == 8.0


# ---- the design text ----------------------------------------------------------------------------

PARTS = [("Auriculaire3.stl", (33.83, 60.3, 23.22)), ("thumb5.stl", (45.0, 70.5, 30.0))]


def test_the_generated_design_passes_the_gate_and_reads_back():
    src = M.model_source("InMoov Right Hand", PARTS, origin=r"C:\Users\brian\Downloads\InMoov\Right-Hand",
                         credit='InMoov by Gael Langevin, CC BY-NC 4.0 """not a docstring end')
    assert cadpy.inspect_source(src) == []
    brief = cadpy.parse_brief(src)
    assert brief["title"] == "InMoov Right Hand"
    assert brief["parts"] == ["Auriculaire3 — 33.8 × 60.3 × 23.2 mm", "thumb5 — 45 × 70.5 × 30 mm"]
    assert "CC BY-NC 4.0" in brief["summary"] and "'not a docstring end" in brief["summary"]
    assert src.count('"""') == 2                       # the credit's triple quote was neutralised
    # a Windows path must not become a unicode escape: forward slashes only in the docstring
    assert "\\" not in src and "C:/Users/brian/Downloads/InMoov/Right-Hand" in src
    (scale,) = cadpy.parse_params(src)
    assert (scale.name, scale.value, scale.minimum, scale.maximum, scale.step) == ("scale", "1", 0.25, 2.0, 0.05)
    assert '"Auriculaire3": "Auriculaire3.stl",' in src and '"thumb5": "thumb5.stl",' in src
    assert "def build():" in src and "mesh(file, scale)" in src
    assert "from helix_parts import *" in src


def test_the_design_executes_against_a_stand_in_mesh_loader():
    """build() must load every file with the parameter's scale — run it with a fake mesh()."""
    src = M.model_source("Two parts", PARTS, scale=0.5)
    ns = {"mesh": lambda file, scale: (file, scale)}
    exec(src.split("from helix_parts import *")[1], ns)  # noqa: S102 — our own generated text
    assert ns["build"]() == {"Auriculaire3": ("Auriculaire3.stl", 0.5), "thumb5": ("thumb5.stl", 0.5)}
    (scale,) = cadpy.parse_params(src)
    assert scale.value == "0.5"


def test_the_library_seeds_the_loader_and_the_cheat_sheet_names_it():
    assert "def mesh(file: str, scale: float = 1.0)" in cadpy.HELIX_LIB
    assert 'PARTS_DIR = _Path(__file__).resolve().parent / "parts"' in cadpy.HELIX_LIB
    assert "mesh(file, scale=1.0)" in cadpy.HELIX_LIB_DOC
    # the design file itself still may not import anything new — mesh() rides on helix_parts
    assert cadpy.ALLOWED_IMPORTS == frozenset({"helix_parts", "build123d", "math", "typing", "dataclasses"})
