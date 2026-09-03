"""Printability analysis pins — the geometry rules that keep holograms out of Bambu's
'floating regions' warning and inside the P1S's bed. Pure numpy over synthetic triangles;
no kernel needed."""
from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from helix.cad.runner import _WARN_MIN_Z_MM, _print_warnings, overhang_report, plate_contact_cm2
from helix.services.model_baker import _overhang_cm2


def tri(z, size=20.0, up=False):
    """One horizontal triangle at height z, ~size²/2 mm² — facing DOWN unless up=True."""
    a, b, c = [0, 0, z], [size, 0, z], [0, size, z]
    return [a, b, c] if up else [a, c, b]  # winding flips the normal


def test_flat_plate_faces_are_never_overhang():
    v = np.array([tri(0.0), tri(0.2), tri(_WARN_MIN_Z_MM)], dtype=np.float64)
    report = overhang_report(v)
    assert report["overhang_cm2"] == 0.0 and report["lowest_mm"] is None


def test_a_hung_ceiling_is_reported_with_its_height():
    # A 20x20 downward face at 5 mm — the IronEye rear-lid class of failure.
    v = np.array([tri(5.0)], dtype=np.float64)
    report = overhang_report(v)
    assert report["overhang_cm2"] == 2.0  # 200 mm² = 2 cm²
    assert report["lowest_mm"] == 5.0


def test_upward_faces_never_count():
    v = np.array([tri(5.0, up=True), tri(9.0, up=True)], dtype=np.float64)
    assert overhang_report(v)["overhang_cm2"] == 0.0


def test_near_plate_recess_ceilings_are_tolerated():
    # A debossed label / lens counterbore ceiling at 0.7 mm is a millimetre bridge, not a failure.
    v = np.array([tri(0.7)], dtype=np.float64)
    assert overhang_report(v)["overhang_cm2"] == 0.0


def test_vertical_walls_never_count():
    wall = np.array([[[0, 0, 0], [0, 20, 0], [0, 0, 20]]], dtype=np.float64)
    assert overhang_report(wall)["overhang_cm2"] == 0.0


def test_the_bakers_severity_parse_reads_the_area():
    line = "OVERHANG: ≈76.1 cm² of faces steeper than 45° downward (lowest at 4.75 mm) — …"
    assert _overhang_cm2(line) == 76.1
    assert _overhang_cm2("no digits here") == 0.0


# ----------------------------------------------------------------------------------------------------
# The P1S checks: plate contact, bed fit
# ----------------------------------------------------------------------------------------------------

def test_plate_contact_counts_only_down_faces_on_the_plate():
    v = np.array([tri(0.0), tri(5.0), tri(0.0, up=True)], dtype=np.float64)
    assert plate_contact_cm2(v) == 2.0  # the raised and the upward faces glue nothing to the bed


def _write_binary_stl(path: Path, tris) -> Path:
    out = b"\0" * 80 + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for vx in t:
            out += struct.pack("<3f", *vx)
        out += b"\0\0"
    path.write_bytes(out)
    return path


class _FakePart:
    """A stand-in build123d shape: just a bounding box (one solid, so the floating scan skips it)."""

    def __init__(self, x, y, z):
        self._bb = SimpleNamespace(size=SimpleNamespace(X=x, Y=y, Z=z), min=SimpleNamespace(Z=0.0))

    def bounding_box(self):
        return self._bb

    def solids(self):
        return []


def test_a_part_bigger_than_the_p1s_bed_is_named(tmp_path):
    parts = [("body", _FakePart(300.0, 40.0, 40.0)), ("lid", _FakePart(100.0, 40.0, 10.0))]
    warns = _print_warnings(parts, tmp_path / "missing.stl")  # no STL: only the bed-fit scan runs
    assert len(warns) == 1 and warns[0].startswith("TOO BIG") and "'body'" in warns[0]
    assert "256" in warns[0]  # the message teaches the P1S bed size


def test_a_tall_print_on_a_sliver_of_contact_warns(tmp_path):
    tall_thin = [tri(0.0, size=5.0), tri(30.0, size=5.0, up=True)]   # 0.125 cm² holding 30 mm
    stl = _write_binary_stl(tmp_path / "thin.stl", tall_thin)
    warns = _print_warnings([], stl)
    assert len(warns) == 1 and warns[0].startswith("SMALL CONTACT")

    wide_base = [tri(0.0, size=20.0), tri(30.0, size=20.0, up=True)]  # 2 cm² base: fine
    stl2 = _write_binary_stl(tmp_path / "wide.stl", wide_base)
    assert _print_warnings([], stl2) == []


def test_a_short_print_never_triggers_the_contact_warning(tmp_path):
    short = [tri(0.0, size=5.0), tri(10.0, size=5.0, up=True)]  # 10 mm tall — nothing to tip
    stl = _write_binary_stl(tmp_path / "short.stl", short)
    assert _print_warnings([], stl) == []
