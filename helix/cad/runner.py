"""The hologram compile worker. Runs as a SUBPROCESS: `python -m helix.cad.runner <job.json>`
(dev) or `HELIX.exe cadworker <job.json>` (frozen — main.py routes the command here before Qt).

One job = one full artifact set. The job file says where everything goes:

    {"source": ".../model.py", "workspace": "...", "overrides": {"width": 100},
     "outputs": {"stl": "...", "step": "...", "mf": "...", "png": "...", "meta": "..."},
     "result": ".../result.json"}

The worker execs model.py (already AST-gated by domain.cadpy — re-gated here, defense in depth),
applies parameter overrides BETWEEN exec and build() (why top-level geometry is a lint), calls
build(), and exports. It writes `result` as {"ok", "problem", "detail", "seconds", "outputs"} and
exits 0 even on a design failure — a crash exit means the WORKER broke, not the design. stdout is
never the protocol (OCCT chats on it); the result file is.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from helix.domain import cadpy


def _fail(result_path: Path, problem: str, detail: str, seconds: float) -> int:
    result_path.write_text(json.dumps({
        "ok": False, "problem": problem, "detail": detail[-2000:], "seconds": round(seconds, 2),
    }), encoding="utf-8")
    return 0


def _norm_parts(built) -> list[tuple[str, object]]:
    """build() may return one shape, a dict of named shapes, or a list. Normalize to named parts."""
    if isinstance(built, dict):
        return [(str(k), v) for k, v in built.items() if v is not None]
    if isinstance(built, (list, tuple)):
        return [(f"part_{i + 1}", v) for i, v in enumerate(built) if v is not None]
    return [("part", built)] if built is not None else []


def _arrange(parts: list[tuple[str, object]]):
    """Multiple parts print (and read) best side by side: lay them along X on Z=0 with a gap,
    centered as a group. Single parts keep their authored position (libraries sit on Z=0)."""
    from build123d import Pos

    if len(parts) <= 1:
        return parts
    gap = 8.0
    placed: list[tuple[str, object]] = []
    x = 0.0
    total = 0.0
    boxes = []
    for name, p in parts:
        bb = p.bounding_box()
        boxes.append((name, p, bb))
        total += bb.size.X
    total += gap * (len(parts) - 1)
    x = -total / 2
    for name, p, bb in boxes:
        placed.append((name, Pos(x - bb.min.X, 0, -bb.min.Z) * p))
        x += bb.size.X + gap
    return placed


def _read_stl_tris(stl_path: Path):
    """Binary STL → (n, 3, 3) float64 triangle vertices, or None. Shared by the preview renderer
    and the printability analysis."""
    import numpy as np

    raw = stl_path.read_bytes()
    if len(raw) < 84:
        return None
    n = int.from_bytes(raw[80:84], "little")
    if len(raw) < 84 + n * 50:
        return None
    body = np.frombuffer(raw, dtype=np.uint8, offset=84)
    tris = body[: n * 50].reshape(n, 50)
    floats = np.frombuffer(tris[:, :48].tobytes(), dtype="<f4").reshape(n, 12)
    return floats[:, 3:12].reshape(n, 3, 3).astype(np.float64)


# Printability thresholds (FDM, a Bambu P1S at 0.2 mm layers in mind). Faces steeper DOWN than 45°
# want support; ones that begin above the first layers are the slicer's "floating regions". Tuned on
# the IronEye case: debossed labels, lens counterbores and port ceilings are millimetre bridges every
# enclosure has (printed clean, no support), so near-plate ceilings are ignored and the warning fires
# only at real support-job area — while a hung interior (tens of cm²) or a lifted floor still shouts.
_OVERHANG_NZ = -0.72          # unit face normal z below this ≈ steeper than 45° downward
_WARN_MIN_Z_MM = 1.2          # ceilings at/below this are near-plate cosmetic recesses — bridged fine
_WARN_AREA_CM2 = 4.0          # report overhang past this much area. Calibrated on the IronEye
                              # joint: a fully-jointed two-half shell carries ~3 cm² of benign
                              # micro-bridges (0.5 mm chamfer residue on the lip, port ceilings)
                              # that print clean — real support jobs measure far past this.
_ISLAND_GAP_MM = 0.6          # a solid whose lowest point is above this floats entirely


def overhang_report(v) -> dict:
    """Steep-downward-face analysis over placed triangles (Z-up, plate at Z=0): the area that would
    need support, and where it starts. Pure numpy — unit-tested with synthetic triangles."""
    import numpy as np

    if v is None or len(v) == 0:
        return {"overhang_cm2": 0.0, "lowest_mm": None}
    e1 = v[:, 1] - v[:, 0]
    e2 = v[:, 2] - v[:, 0]
    fn = np.cross(e1, e2)
    ln = np.linalg.norm(fn, axis=1)
    ok = ln > 1e-12
    v, fn, ln = v[ok], fn[ok], ln[ok]
    nz = fn[:, 2] / ln
    area = ln / 2.0
    face_low = v[:, :, 2].min(axis=1)
    bad = (nz < _OVERHANG_NZ) & (face_low > _WARN_MIN_Z_MM)
    total_cm2 = float(area[bad].sum() / 100.0)
    lowest = float(face_low[bad].min()) if bad.any() else None
    return {"overhang_cm2": round(total_cm2, 2), "lowest_mm": round(lowest, 2) if lowest else None}


def _print_warnings(parts, stl_path: Path) -> list[str]:
    """The printability verdicts for meta.json — what the studio shows and the baker's repair loop
    reads. Two classes: a FLOATING solid (always wrong — a piece of a part begins mid-air, the
    slicer's 'floating regions' warning), and steep OVERHANG area (needs supports, or a redesign
    with the flat face down). Never raises; an analysis hiccup just reports nothing."""
    warnings: list[str] = []
    try:
        for name, shape in parts:
            solids = list(getattr(shape, "solids", lambda: [])() or [])
            for solid in solids if len(solids) > 1 else []:
                gap = float(solid.bounding_box().min.Z)
                if gap > _ISLAND_GAP_MM:
                    warnings.append(
                        f"FLOATING: a piece of '{name}' starts {gap:.1f} mm above the plate — "
                        f"nothing below it to print on. Re-author the part so every piece rests "
                        f"on Z=0 or on material beneath it."
                    )
    except Exception:  # noqa: BLE001
        pass
    try:
        report = overhang_report(_read_stl_tris(stl_path))
        if report["overhang_cm2"] >= _WARN_AREA_CM2:
            warnings.append(
                f"OVERHANG: ≈{report['overhang_cm2']:.1f} cm² of faces steeper than 45° downward "
                f"(lowest at {report['lowest_mm']} mm) — the slicer will want supports. Prefer the "
                f"flat face on the plate, deboss instead of emboss, chamfer undersides."
            )
    except Exception:  # noqa: BLE001
        pass
    return warnings


def _render_preview(stl_path: Path, png_path: Path, size=(1280, 960)) -> bool:
    """A flat-shaded orthographic preview off the binary STL — what the vision critic looks at.
    Pure numpy + Pillow (no GL, works headless): isometric-ish view, painter's sort, matcap-flat
    grey on dark slate with a faint warm key light. Good enough to judge geometry, fast enough to
    never be the slow step."""
    import numpy as np
    from PIL import Image, ImageDraw

    v = _read_stl_tris(stl_path)
    if v is None:
        return False
    n = len(v)

    # View: rotate the model so we look from front-right-above (the classic drawing angle).
    def rot(axis, deg):
        a = np.radians(deg)
        c, s = np.cos(a), np.sin(a)
        if axis == "x":
            return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        if axis == "z":
            return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])

    m = rot("x", -62) @ rot("z", -32)
    pts = v.reshape(-1, 3) @ m.T
    pts = pts.reshape(n, 3, 3)
    # Face normals AFTER the view rotation — lighting is view-space.
    e1 = pts[:, 1] - pts[:, 0]
    e2 = pts[:, 2] - pts[:, 0]
    fn = np.cross(e1, e2)
    ln = np.linalg.norm(fn, axis=1, keepdims=True)
    ln[ln == 0] = 1
    fn = fn / ln
    # Cull back faces (normal pointing away from the camera at +Z).
    keep = fn[:, 2] > 0.0
    pts, fn = pts[keep], fn[keep]
    if len(pts) == 0:
        return False
    # Fit to frame (orthographic).
    xy = pts[:, :, :2].reshape(-1, 2)
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    span = np.maximum(hi - lo, 1e-6)
    w, h = size
    margin = 0.82
    scale = min(w * margin / span[0], h * margin / span[1])
    off = np.array([w / 2, h / 2]) - (lo + hi) / 2 * scale
    scr = pts[:, :, :2] * scale + off
    scr[:, :, 1] = h - scr[:, :, 1]  # raster Y grows downward
    depth = pts[:, :, 2].mean(axis=1)
    order = np.argsort(depth)  # painter's algorithm: far first
    light = np.array([0.35, 0.45, 0.83])
    light = light / np.linalg.norm(light)
    lum = np.clip(fn @ light, 0.0, 1.0) * 0.72 + 0.20
    img = Image.new("RGB", size, (16, 22, 28))
    draw = ImageDraw.Draw(img)
    edges = len(order) <= 60_000  # crease look on light meshes; plain shading on heavy ones
    base = np.array([171, 186, 199])
    for i in order:
        c = tuple(int(x) for x in np.clip(base * lum[i], 0, 255))
        poly = [tuple(p) for p in scr[i]]
        draw.polygon(poly, fill=c, outline=(52, 66, 78) if edges else None)
    img.save(png_path)
    return True


def run_job(job_path: str) -> int:
    t0 = time.time()
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    result_path = Path(job["result"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    src = Path(job["source"])
    workspace = Path(job.get("workspace") or src.parent)
    outputs = {k: Path(v) for k, v in (job.get("outputs") or {}).items()}
    overrides = job.get("overrides") or {}

    try:
        source = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _fail(result_path, "The design file couldn't be read.", "", time.time() - t0)
    lints = cadpy.inspect_source(source)
    if lints:
        return _fail(result_path, lints[0], " ".join(lints), time.time() - t0)

    # helix_parts (and only it) resolves from the workspace; the runner's own cwd stays out of it.
    sys.path.insert(0, str(workspace))
    try:
        from build123d import Compound, Mesher, export_step, export_stl
    except Exception:  # noqa: BLE001 — the engine itself is unusable
        return _fail(result_path, "The CAD engine isn't available in this environment.",
                     traceback.format_exc(), time.time() - t0)

    try:
        code = compile(source, str(src), "exec")
        ns: dict = {"__name__": "helix_model", "__file__": str(src)}
        exec(code, ns)  # noqa: S102 — AST-gated above; geometry only, subprocess besides
        params = {p.name for p in cadpy.parse_params(source)}
        for k, val in overrides.items():
            if k in params:
                ns[k] = val
        build = ns.get("build")
        if not callable(build):
            return _fail(result_path, "The design's build() didn't hand back a part.",
                         "model.py defines no build() function", time.time() - t0)
        built = build()
        parts = _norm_parts(built)
        if not parts:
            return _fail(result_path, "The design's build() didn't hand back a part.",
                         f"build() returned {type(built).__name__}", time.time() - t0)
        parts = _arrange(parts)
        shapes = [p for _, p in parts]
        whole = shapes[0] if len(shapes) == 1 else Compound(children=list(shapes))
    except MemoryError:
        return _fail(result_path, "The design ran out of room to compute — too much detail at "
                     "once.", traceback.format_exc(limit=3), time.time() - t0)
    except Exception:  # noqa: BLE001 — a design failure is an ordinary outcome
        problem, detail = cadpy.friendly_error(traceback.format_exc())
        return _fail(result_path, problem, detail, time.time() - t0)

    produced: dict[str, str] = {}
    problems: list[str] = []
    if "stl" in outputs:
        try:
            outputs["stl"].parent.mkdir(parents=True, exist_ok=True)
            export_stl(whole, str(outputs["stl"]))
            produced["stl"] = str(outputs["stl"])
        except Exception:  # noqa: BLE001
            problems.append("stl: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
    if "step" in outputs:
        try:
            outputs["step"].parent.mkdir(parents=True, exist_ok=True)
            export_step(whole, str(outputs["step"]))
            produced["step"] = str(outputs["step"])
        except Exception:  # noqa: BLE001
            problems.append("step: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
    if "mf" in outputs:
        try:
            outputs["mf"].parent.mkdir(parents=True, exist_ok=True)
            mesher = Mesher()
            for _, shape in parts:
                mesher.add_shape(shape)
            mesher.write(str(outputs["mf"]))
            produced["mf"] = str(outputs["mf"])
        except Exception:  # noqa: BLE001
            problems.append("3mf: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
    if "png" in outputs and "stl" in produced:
        try:
            if _render_preview(Path(produced["stl"]), outputs["png"]):
                produced["png"] = str(outputs["png"])
        except Exception:  # noqa: BLE001
            problems.append("png: " + traceback.format_exc(limit=1).strip().splitlines()[-1])
    if "stl" not in produced:
        detail = "; ".join(problems) or "no STL was produced"
        return _fail(result_path, "The design computed but couldn't be exported.", detail,
                     time.time() - t0)

    meta: dict = {"parts": [name for name, _ in parts], "problems": problems}
    try:
        bb = whole.bounding_box()
        meta["bbox_mm"] = [round(bb.size.X, 2), round(bb.size.Y, 2), round(bb.size.Z, 2)]
        vol_cm3 = sum(getattr(s, "volume", 0.0) for s in shapes) / 1000.0
        meta["volume_cm3"] = round(vol_cm3, 2)
        meta["solid_grams_pla"] = round(vol_cm3 * 1.24, 1)  # solid PLA; shells print near-solid
    except Exception:  # noqa: BLE001 — metadata is a nicety
        pass
    if "stl" in produced:
        meta["print_warnings"] = _print_warnings(parts, Path(produced["stl"]))
    if "meta" in outputs:
        try:
            outputs["meta"].parent.mkdir(parents=True, exist_ok=True)
            outputs["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
            produced["meta"] = str(outputs["meta"])
        except OSError:
            pass

    result_path.write_text(json.dumps({
        "ok": True, "problem": None, "detail": None, "seconds": round(time.time() - t0, 2),
        "outputs": produced, "meta": meta,
    }), encoding="utf-8")
    return 0


def serve() -> int:
    """Warm-worker mode for the studio's live sliders: the kernel imports ONCE, then job paths
    arrive one per stdin line and 'done <path>' answers each on stdout. A recompile that costs ~4s
    cold runs in about a second warm — the difference between a slider and a wait."""
    try:
        import build123d  # noqa: F401 — pay the kernel import up front, before the first job
    except Exception:  # noqa: BLE001
        print("engine-missing", flush=True)
        return 1
    print("ready", flush=True)
    for line in sys.stdin:
        job_path = line.strip()
        if not job_path:
            continue
        if job_path == "quit":
            break
        try:
            run_job(job_path)
        except Exception:  # noqa: BLE001 — one broken job must not kill the warm worker
            traceback.print_exc()
        print(f"done {job_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args == ["--serve"]:
        return serve()
    if len(args) != 1:
        print("usage: python -m helix.cad.runner <job.json> | --serve", file=sys.stderr)
        return 2
    try:
        return run_job(args[0])
    except Exception:  # noqa: BLE001 — a crash here means the WORKER broke; say so loudly
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
