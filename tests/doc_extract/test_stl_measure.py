"""stl_measure — bounding boxes read straight from STL vertices, so "does it fit my printer bed?" is
answered by the mesh instead of by hunting a release page's prose. Fixture archives are built in-test
with KNOWN extents; the assertions are exact because every coordinate used is float32-representable."""
from __future__ import annotations

import struct
import tarfile
import zipfile

from helix.services import stl_measure


def _binary_stl(triangles: list[tuple[tuple[float, float, float], ...]]) -> bytes:
    """A well-formed binary STL: 80-byte header, uint32 count, then 50 bytes per triangle."""
    out = bytearray(b"HELIX test mesh".ljust(80, b"\0"))
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        out += struct.pack("<3f", 0.0, 0.0, 1.0)  # normal (ignored by the measurer)
        for vx, vy, vz in tri:
            out += struct.pack("<3f", vx, vy, vz)
        out += struct.pack("<H", 0)
    return bytes(out)


def _ascii_stl(triangles: list[tuple[tuple[float, float, float], ...]]) -> bytes:
    lines = ["solid helix_test"]
    for tri in triangles:
        lines.append("  facet normal 0 0 1")
        lines.append("    outer loop")
        for vx, vy, vz in tri:
            lines.append(f"      vertex {vx} {vy} {vz}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid helix_test")
    return "\n".join(lines).encode("ascii")


# Two triangles that touch every corner-extreme of a 40 × 25 × 10 box offset from the origin —
# min (5, 5, 0), max (45, 30, 10). All values are exact in float32, so extents compare with ==.
_BOX_TRIS = [
    ((5.0, 5.0, 0.0), (45.0, 5.0, 0.0), (45.0, 30.0, 0.0)),
    ((5.0, 30.0, 10.0), (45.0, 30.0, 10.0), (5.0, 5.0, 10.0)),
]
# A second, smaller part: a 12 × 8 × 3 wedge sitting at the origin.
_WEDGE_TRIS = [
    ((0.0, 0.0, 0.0), (12.0, 0.0, 0.0), (12.0, 8.0, 3.0)),
]


def test_zip_release_archive_reports_exact_extents(tmp_path):
    archive = tmp_path / "widget-v1.0.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("widget/body.stl", _binary_stl(_BOX_TRIS))
    boxes = stl_measure.measure(archive)
    assert len(boxes) == 1
    box = boxes[0]
    assert box.name == "widget/body.stl"
    assert box.triangles == 2
    assert box.min_xyz == (5.0, 5.0, 0.0)
    assert box.max_xyz == (45.0, 30.0, 10.0)
    assert box.size == (40.0, 25.0, 10.0)  # the exact X/Y/Z extents, straight from the vertices


def test_archive_with_multiple_stls_reports_per_file_boxes(tmp_path):
    # One binary, one ASCII, plus a README the measurer must ignore — per-file boxes, name-sorted.
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("parts/body.stl", _binary_stl(_BOX_TRIS))
        zf.writestr("parts/wedge.STL", _ascii_stl(_WEDGE_TRIS))
        zf.writestr("README.md", "Dimensions: who knows! Measure the mesh.")
    boxes = stl_measure.measure(archive)
    assert [b.name for b in boxes] == ["parts/body.stl", "parts/wedge.STL"]
    assert boxes[0].size == (40.0, 25.0, 10.0)
    assert boxes[1].size == (12.0, 8.0, 3.0)
    assert boxes[1].triangles == 1


def test_tar_gz_archive_is_measured_too(tmp_path):
    stl_path = tmp_path / "wedge.stl"
    stl_path.write_bytes(_binary_stl(_WEDGE_TRIS))
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stl_path, arcname="models/wedge.stl")
    boxes = stl_measure.measure(archive)
    assert len(boxes) == 1
    assert boxes[0].name == "models/wedge.stl"
    assert boxes[0].size == (12.0, 8.0, 3.0)


def test_bare_stl_files_binary_and_ascii(tmp_path):
    bin_path = tmp_path / "body.stl"
    bin_path.write_bytes(_binary_stl(_BOX_TRIS))
    asc_path = tmp_path / "wedge.stl"
    asc_path.write_bytes(_ascii_stl(_WEDGE_TRIS))
    (bin_box,) = stl_measure.measure(bin_path)
    (asc_box,) = stl_measure.measure(asc_path)
    assert bin_box.size == (40.0, 25.0, 10.0)
    assert asc_box.size == (12.0, 8.0, 3.0)


def test_binary_stl_with_solid_header_is_not_mistaken_for_ascii(tmp_path):
    # Some exporters write "solid ..." into the binary comment header; the size arithmetic must win.
    data = bytearray(_binary_stl(_BOX_TRIS))
    data[:20] = b"solid exported-part ".ljust(20, b" ")
    p = tmp_path / "sneaky.stl"
    p.write_bytes(bytes(data))
    (box,) = stl_measure.measure(p)
    assert box.size == (40.0, 25.0, 10.0)


def test_garbage_and_empty_yield_no_boxes_and_no_exception(tmp_path):
    junk = tmp_path / "broken.stl"
    junk.write_bytes(b"\x00\x01\x02 not a mesh at all")
    assert stl_measure.measure(junk) == []
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("notes.txt", "no meshes here")
    assert stl_measure.measure(empty_zip) == []
    assert stl_measure.measure(tmp_path / "missing.zip") == []
