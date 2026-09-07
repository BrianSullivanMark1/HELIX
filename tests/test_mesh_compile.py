"""Loaded meshes on the REAL kernel: the generated design (domain.meshes.model_source) compiled the
way helix/cad/runner.py compiles it — helix_parts seeded beside model.py, parts/ holding the STLs,
run_job writing the artifact set — and the layout, the fence, the scale slider and the meta the
studio and the print sheet read. A few seconds; kept few."""
from __future__ import annotations

import importlib
import json
import struct
import sys
from pathlib import Path

import pytest

build123d = pytest.importorskip("build123d")

from helix.cad import runner  # noqa: E402
from helix.domain import cadpy  # noqa: E402
from helix.domain import meshes as M  # noqa: E402

from build123d import Box, Align  # noqa: E402

CM = (Align.CENTER, Align.CENTER, Align.MIN)


def _box_tris(x0, y0, z0, x1, y1, z1):
    """The 12 triangles of a closed axis-aligned box, outward-wound — a watertight little mesh."""
    p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [(p[a], p[b], p[c]) for a, b, c in faces]


def _stl(path: Path, tris) -> Path:
    out = bytearray(b"\0" * 80 + struct.pack("<I", len(tris)))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for v in t:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "parts").mkdir()
    (tmp_path / cadpy.HELIX_LIB_FILE).write_text(cadpy.HELIX_LIB, encoding="utf-8")
    yield tmp_path
    sys.modules.pop("helix_parts", None)


def _lib(ws: Path):
    sys.modules.pop("helix_parts", None)
    sys.path.insert(0, str(ws))
    try:
        return importlib.import_module("helix_parts")
    finally:
        sys.path.remove(str(ws))


def _job(ws: Path, overrides=None) -> dict:
    job = {"source": str(ws / "model.py"), "workspace": str(ws), "overrides": overrides or {},
           "outputs": {k: str(ws / "assets" / f"model.{e}")
                       for k, e in (("stl", "stl"), ("step", "step"), ("mf", "3mf"), ("png", "png"), ("meta", "meta.json"))},
           "result": str(ws / "result.json")}
    (ws / "job.json").write_text(json.dumps(job), encoding="utf-8")
    assert runner.run_job(str(ws / "job.json")) == 0
    return json.loads((ws / "result.json").read_text(encoding="utf-8"))


def test_mesh_loads_centred_on_the_plate_scales_and_refuses_paths(ws):
    _stl(ws / "parts" / "box.stl", _box_tris(10, 20, 30, 50, 45, 40))     # 40 × 25 × 10 away from the origin
    hp = _lib(ws)
    a = hp.mesh("box.stl")
    bb = a.bounding_box()
    assert (round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)) == (40.0, 25.0, 10.0)
    assert abs(bb.min.X + 20) < 1e-6 and abs(bb.min.Y + 12.5) < 1e-6 and abs(bb.min.Z) < 1e-6
    half = hp.mesh("box.stl", 0.5).bounding_box()
    assert (round(half.size.X, 3), round(half.size.Y, 3), round(half.size.Z, 3)) == (20.0, 12.5, 5.0)
    assert abs(half.min.Z) < 1e-6
    for bad in ("../model.py", "C:/box.stl", "sub/box.stl", "missing.stl", "helix_parts.py", "", "box"):
        with pytest.raises(ValueError):
            hp.mesh(bad)
    with pytest.raises(ValueError):
        hp.mesh("box.stl", 0)
    # ASCII STL reads through the scaled path too
    (ws / "parts" / "tri.stl").write_bytes(b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\n"
                                           b"vertex 10 0 0\nvertex 0 10 0\nendloop\nendfacet\nendsolid t\n")
    assert round(hp.mesh("tri.stl", 2.0).bounding_box().size.X, 3) == 20.0


def test_the_generated_design_compiles_to_the_full_artifact_set_with_mesh_meta(ws):
    parts = []
    for i in range(5):
        _stl(ws / "parts" / f"p{i}.stl", _box_tris(0, 0, 0, 20, 10, 5))
        parts.append((f"p{i}.stl", (20.0, 10.0, 5.0)))
    (ws / "model.py").write_text(M.model_source("Five boxes", parts), encoding="utf-8")
    res = _job(ws)
    assert res["ok"], res
    meta = res["meta"]
    assert meta["parts"] == [f"p{i}" for i in range(5)] and meta["mesh_parts"] == meta["parts"]
    assert meta["plates"] == [[f"p{i}" for i in range(5)]]            # 5 × 20 + gaps = 132 < 244: one row
    for name in meta["parts"]:
        assert meta["parts_mm"][name] == [20.0, 10.0, 5.0]
    assert meta["bbox_mm"] == [132.0, 10.0, 5.0]
    assert abs(meta["volume_cm3"] - 5.0) < 0.01                        # 5 × (20·10·5 mm³) = 5 cm³, off the triangles
    assert meta["solid_grams_pla"] == 6.2
    assert meta["supports"] == [] and meta["print_warnings"] == []
    assert "stl" in res["outputs"] and "mf" in res["outputs"] and "png" in res["outputs"]
    assert "step" not in res["outputs"]                                 # a triangulated face has no STEP
    assert any(p.startswith("step: skipped") for p in meta["problems"])
    # the studio's slider: the scale override halves every part and the volume by eight
    res = _job(ws, {"scale": 0.5})
    assert res["ok"]
    assert res["meta"]["parts_mm"]["p0"] == [10.0, 5.0, 2.5]
    assert abs(res["meta"]["volume_cm3"] - 0.625) < 0.01


def test_a_set_of_loaded_meshes_wider_than_the_plate_is_packed_onto_plates_and_centred(ws):
    _stl(ws / "parts" / "w.stl", _box_tris(0, 0, 0, 100, 120, 5))
    hp = _lib(ws)
    parts = [(f"w{i}", hp.mesh("w.stl")) for i in range(6)]
    placed, plates = runner._layout(parts)
    assert plates == [["w0", "w1"], ["w2", "w3"], ["w4", "w5"]]
    xs = sorted(p.bounding_box().min.X for _, p in placed)
    ys = sorted(p.bounding_box().min.Y for _, p in placed)
    assert all(abs(p.bounding_box().min.Z) < 1e-6 for _, p in placed)
    assert abs(xs[0] + (xs[-1] + 100.0)) < 1e-6                         # centred as a group in X…
    assert abs(ys[0] + (ys[-1] + 120.0)) < 1e-6                         # …and in Y
    assert abs((xs[1] - xs[0]) - 108.0) < 1e-6                          # a part and a gap apart
    assert abs((xs[2] - xs[0]) - (M.PLATE_MM + M.PLATE_GAP_MM)) < 1e-6  # the next plate to the right
    # a short row keeps the old single-row layout to the millimetre (the enclosure ghosts count on it)
    two = [("base", Box(100.0, 40.0, 5.0, align=CM)), ("lid", Box(100.0, 40.0, 5.0, align=CM))]
    placed, plates = runner._layout(two)
    assert plates == [["base", "lid"]]
    assert abs(placed[0][1].bounding_box().min.X + 104.0) < 1e-6 and abs(placed[1][1].bounding_box().min.X - 4.0) < 1e-6
    assert abs(placed[0][1].bounding_box().min.Y + 20.0) < 1e-6         # authored Y kept
    assert runner._arrange(two)[0][0] == "base"
    # AUTHORED parts stay one row however wide it runs — the enclosure's print_origins and the AR
    # ghosts are computed on that row (a relay box with a DIN clip is 300 mm of it)
    wide = [(f"a{i}", Box(100.0, 120.0, 5.0, align=CM)) for i in range(4)]
    placed, plates = runner._layout(wide)
    assert plates == [["a0", "a1", "a2", "a3"]]
    assert abs(placed[0][1].bounding_box().min.X + 212.0) < 1e-6 and abs(placed[3][1].bounding_box().min.X - 112.0) < 1e-6


def test_a_loaded_mesh_with_steep_faces_is_a_supports_note_not_the_coders_overhang(ws):
    # a mushroom: a 4 × 4 post from the plate up to 20, a 30 × 30 × 2 cap on it (two closed boxes
    # in one file) — the cap's underside is 9 cm² of downward face hanging 20 mm up
    _stl(ws / "parts" / "cap.stl", _box_tris(-2, -2, 0, 2, 2, 20) + _box_tris(-15, -15, 20, 15, 15, 22))
    _stl(ws / "parts" / "flat.stl", _box_tris(0, 0, 0, 20, 10, 5))
    src = M.model_source("Mushroom", [("cap.stl", (30.0, 30.0, 22.0)), ("flat.stl", (20.0, 10.0, 5.0))])
    (ws / "model.py").write_text(src, encoding="utf-8")
    res = _job(ws)
    assert res["ok"], res
    warns = res["meta"]["print_warnings"]
    assert len(warns) == 1 and warns[0].startswith("SUPPORTS: 'cap' (a loaded mesh) measures ≈9.0 cm²")
    assert "supports on" in warns[0]
    assert res["meta"]["supports"] == ["cap"]
    assert not any(w.startswith("OVERHANG") for w in warns)
    # the same faces in an AUTHORED solid are the coder's OVERHANG, as ever
    (ws / "model.py").write_text(
        '"""Design: Mushroom — authored\nParts:\n- cap\n"""\nfrom helix_parts import *\n\n'
        "# --- Parameters ---\ncap = 30.0  # [20..40] cap, mm\n# --- End Parameters ---\n\n\n"
        "def build():\n    post = Box(4, 4, 20.01, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
        "    top = Pos(0, 0, 20) * Box(cap, cap, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
        "    return post + top\n", encoding="utf-8")
    res = _job(ws)
    assert res["ok"], res
    assert res["meta"]["mesh_parts"] == [] and res["meta"]["supports"] == []
    assert any(w.startswith("OVERHANG") for w in res["meta"]["print_warnings"])
    assert "step" in res["outputs"]
