"""Measure STL meshes — a model's bounding box, read straight from its vertices.

"Does it fit my printer bed?" should be answered by the MESH, not by hunting a release page's prose
for dimensions nobody may have written down. This module opens a downloaded .stl — or a whole release
archive (.zip / .tar / .tar.gz) holding several — and reports each model's X/Y/Z extents by reading
the triangles themselves. Both STL dialects are handled: binary (80-byte header + packed triangles)
and ASCII ("solid … vertex x y z"). Everything is best-effort in the doc_extract spirit: a corrupt
mesh or archive yields no box (and a log line), never an exception into the caller's turn.

STL carries no unit, but the 3D-printing world overwhelmingly writes millimetres, so extents here are
mm by convention — a caller comparing against a printer bed is comparing like with like. Archives are
read entry-by-entry IN MEMORY (never extracted to disk), so a hostile path inside one can't escape.
"""
from __future__ import annotations

import gzip
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("stl_measure")

STL_EXT = ".stl"
_MAX_MESH_BYTES = 200 * 1024 * 1024  # one mesh's ceiling — past this it's not a printable part file
_BIN_HEADER = 84                     # 80-byte comment header + uint32 triangle count
_BIN_TRI = 50                        # 12 float32 (normal + 3 vertices) + uint16 attribute


@dataclass(frozen=True)
class MeshBox:
    """One STL's axis-aligned bounding box. `name` is the file name (or the entry path inside an
    archive); extents are in the mesh's own units — millimetres by 3D-printing convention."""
    name: str
    triangles: int
    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]

    @property
    def size(self) -> tuple[float, float, float]:
        """X/Y/Z extents — the numbers to hold against a printer bed."""
        return tuple(hi - lo for lo, hi in zip(self.min_xyz, self.max_xyz))  # type: ignore[return-value]


def is_stl(path) -> bool:
    """True for a bare .stl (or .stl.gz) file this module measures directly."""
    low = str(path).lower()
    return low.endswith(STL_EXT) or low.endswith(STL_EXT + ".gz")


def measure(path) -> list[MeshBox]:
    """Every STL bounding box found at `path` — a bare .stl (one box), or a release archive (one box
    per .stl entry, in name order). Unreadable files and corrupt meshes are skipped with a log line;
    the return is simply what could be measured. Never raises."""
    p = Path(path)
    try:
        if is_stl(p):
            box = _measure_stl_file(p)
            return [box] if box else []
        if zipfile.is_zipfile(p):
            entries = _zip_entries(p)
        elif tarfile.is_tarfile(p):
            entries = _tar_entries(p)
        else:
            _LOG.info("not an STL or a readable archive: %s", p)
            return []
        boxes = []
        for name, data in sorted(entries, key=lambda e: e[0].lower()):
            box = measure_bytes(data, name)
            if box:
                boxes.append(box)
        return boxes
    except Exception as exc:  # noqa: BLE001 - a bad download must never break the caller's turn
        _LOG.warning("could not measure %s: %s", path, exc)
        return []


def measure_bytes(data: bytes, name: str) -> MeshBox | None:
    """The bounding box of one STL held in memory, or None if it isn't a readable mesh."""
    try:
        if len(data) > _MAX_MESH_BYTES:
            _LOG.info("mesh too large to measure (%d bytes): %s", len(data), name)
            return None
        if _looks_binary(data):
            return _parse_binary(data, name)
        return _parse_ascii(data, name)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("could not parse mesh %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------- archives

def _zip_entries(path: Path):
    """(entry name, bytes) for each .stl inside a zip, read in memory with the size cap applied."""
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not is_stl(info.filename):
                continue
            with zf.open(info) as fh:
                data = fh.read(_MAX_MESH_BYTES + 1)  # trust the bytes read, not the header's claim
            if len(data) > _MAX_MESH_BYTES:
                _LOG.info("archive entry too large to measure: %s", info.filename)
                continue
            yield info.filename, _ungz(info.filename, data)


def _tar_entries(path: Path):
    """(entry name, bytes) for each .stl inside a tar / tar.gz, same in-memory discipline."""
    with tarfile.open(path, "r:*") as tf:
        for member in tf:
            if not member.isfile() or not is_stl(member.name):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            data = fh.read(_MAX_MESH_BYTES + 1)
            if len(data) > _MAX_MESH_BYTES:
                _LOG.info("archive entry too large to measure: %s", member.name)
                continue
            yield member.name, _ungz(member.name, data)


def _measure_stl_file(path: Path) -> MeshBox | None:
    if path.stat().st_size > _MAX_MESH_BYTES:
        _LOG.info("mesh too large to measure: %s", path)
        return None
    return measure_bytes(_ungz(path.name, path.read_bytes()), path.name)


def _ungz(name: str, data: bytes) -> bytes:
    return gzip.decompress(data) if str(name).lower().endswith(".gz") else data


# ---------------------------------------------------------------------------- the two STL dialects

def _looks_binary(data: bytes) -> bool:
    """Binary vs ASCII. The reliable tell is arithmetic — a binary STL's length is exactly (or, with
    some writers' trailing padding, at least) 84 + 50·count — because the "solid" keyword is NOT one:
    plenty of binary exporters put "solid …" in the 80-byte comment header too."""
    if len(data) >= _BIN_HEADER:
        (count,) = struct.unpack_from("<I", data, 80)
        if count > 0 and len(data) == _BIN_HEADER + _BIN_TRI * count:
            return True
    head = data[:512].lstrip()
    if head.startswith(b"solid"):
        return False
    return len(data) >= _BIN_HEADER


def _parse_binary(data: bytes, name: str) -> MeshBox | None:
    (count,) = struct.unpack_from("<I", data, 80)
    usable = min(count, (len(data) - _BIN_HEADER) // _BIN_TRI)  # a truncated tail measures what's there
    if usable <= 0:
        return None
    body = data[_BIN_HEADER:_BIN_HEADER + _BIN_TRI * usable]
    mins, maxs = [float("inf")] * 3, [float("-inf")] * 3
    for rec in struct.iter_unpack("<12fH", body):
        for i in (3, 6, 9):  # the three vertices; rec[0:3] is the normal, rec[12] the attribute word
            _fold(rec[i], rec[i + 1], rec[i + 2], mins, maxs)
    return _box(name, usable, mins, maxs)


def _parse_ascii(data: bytes, name: str) -> MeshBox | None:
    text = data.decode("ascii", errors="replace")
    mins, maxs = [float("inf")] * 3, [float("-inf")] * 3
    vertices = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].lower() == "vertex":
            try:
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
            except ValueError:
                continue
            vertices += 1
            _fold(x, y, z, mins, maxs)
    return _box(name, vertices // 3, mins, maxs)


def _fold(x: float, y: float, z: float, mins: list[float], maxs: list[float]) -> None:
    if not (isfinite(x) and isfinite(y) and isfinite(z)):  # a NaN vertex must not poison the box
        return
    for axis, v in enumerate((x, y, z)):
        if v < mins[axis]:
            mins[axis] = v
        if v > maxs[axis]:
            maxs[axis] = v


def _box(name: str, triangles: int, mins: list[float], maxs: list[float]) -> MeshBox | None:
    if triangles <= 0 or not all(isfinite(v) for v in (*mins, *maxs)):
        return None
    return MeshBox(name=name, triangles=triangles, min_xyz=tuple(mins), max_xyz=tuple(maxs))
