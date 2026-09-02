"""Printability analysis pins — the geometry rules that keep holograms out of Bambu's
'floating regions' warning. Pure numpy over synthetic triangles; no kernel needed."""
from __future__ import annotations

import numpy as np

from helix.cad.runner import _WARN_MIN_Z_MM, overhang_report
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
