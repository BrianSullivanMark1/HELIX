"""ModelBaker — turn a declarative model.json into a real polygon mesh (assets/model.glb) + a viewer.

This is the heart of HELIX's high-detail 3D channel. The coder no longer hand-writes Three.js geometry
(which capped at "a pile of labeled primitives"); instead it writes a small, declarative `model.json`
spec, and HELIX's OWN Python bakes that into a real GLB mesh with proper normals, PBR materials, and
boolean cutaways — then drops a single, fixed GLTFLoader viewer (`index.html`) that loads it. The coder
stays shell-less: it only Writes a JSON file; the geometry is built here, in-process.

Design notes:
  - Spec is authored Y-UP (Y is up, X right, Z toward the viewer) — matches the Three.js mental model.
    trimesh's axis-aligned primitives (cylinder/cone/capsule/lathe/extrude) are Z-aligned natively, so
    they are rotated to Y-up here. Boxes/spheres are symmetric and authored directly.
  - Every primitive is centred at the origin, so a part's `position` places its centre.
  - A static mesh (`model.json`) is baked here; an ANIMATED model is a hand-authored Three.js
    `index.html` and is left untouched (the coder still writes those directly).
  - Any failure writes a friendly in-viewer message instead of leaving a blank page or crashing the build.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import trimesh
from trimesh.visual.material import PBRMaterial

from helix.services.builds import MANIFEST  # single source of truth for the manifest filename

# Optional hosted text/image-to-3D backend (Phase 2). Given (prompt, image_path|None) -> GLB bytes.
NeuralBackend = Callable[[str, Path | None], bytes]

SPEC_FILE = "model.json"
GLB_REL = "assets/model.glb"
VIEWER_FILE = "index.html"

DEFAULT_BG = "#080b0f"
DEFAULT_ACCENT = "#3fe0e0"

_AXIS_SHAPES = {"cylinder", "cone", "capsule", "lathe", "extrude"}


class SpecError(Exception):
    """The model.json was missing, malformed, or described nothing we can build."""


class ModelBaker:
    def __init__(self, neural_backend: NeuralBackend | None = None) -> None:
        # neural_backend is the hosted "turbo" path (Phase 2). None = parametric only.
        self._neural = neural_backend

    # ----- public entry point -----
    def bake(self, workspace: Path) -> None:
        """Read the workspace's model spec and (re)bake assets/model.glb + the viewer in place.

        Called by ForgeService AFTER the coder runs and the escape guard passes, BEFORE finalize — so the
        baked index.html is what _detect_entry finds and what gets committed. Never raises: a bad spec
        becomes a friendly error page so the build still completes and the user sees a clear message."""
        spec_path = workspace / SPEC_FILE
        if not spec_path.exists():
            # No spec: this is the hand-authored ANIMATED path (the coder wrote index.html itself), or a
            # build that produced nothing. Leave a real page alone; otherwise explain.
            if not (workspace / VIEWER_FILE).exists():
                self._write_error(workspace, "The model build produced no model.json and no page.")
            return
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise SpecError("model.json must be a JSON object.")
            glb = self._make_glb(spec, workspace)
            (workspace / "assets").mkdir(parents=True, exist_ok=True)
            (workspace / GLB_REL).write_bytes(glb)
            self._write_viewer(workspace, spec)
        except SpecError as exc:
            self._write_error(workspace, str(exc))
        except Exception as exc:  # never let a baking bug fail the whole build
            self._write_error(workspace, f"Couldn't build the model: {exc}")

    # ----- glb construction -----
    def _make_glb(self, spec: dict, workspace: Path) -> bytes:
        """Pick the engine and produce GLB bytes.

        engine "neural" → hosted high-detail (the recognizable-hero path). "parametric" → local primitive
        mesh (clean mechanical/diagram shapes). "auto" (default) → neural when it's enabled and there's a
        prompt, otherwise parametric. A neural attempt that fails falls back to parts when they exist, so
        a missing key / spent credits degrades to the local mesh instead of nothing."""
        engine = str(spec.get("engine", "auto")).lower()
        parts = spec.get("parts")
        parts = parts if isinstance(parts, list) and parts else []
        prompt = str(spec.get("prompt") or spec.get("title") or "").strip()

        if engine == "neural" and self._neural is None:
            raise SpecError("High-detail (neural) modeling isn't enabled — set a TRIPO_API_KEY.")
        want_neural = self._neural is not None and (engine == "neural" or (engine == "auto" and prompt))
        if want_neural:
            try:
                return self._neural_glb(spec, workspace, prompt)
            except SpecError:
                if not parts:
                    raise
            except Exception as exc:
                if not parts:
                    raise SpecError(f"High-detail modeling failed: {exc}")
            # a neural attempt failed but parts exist — fall through to the local mesh.

        if not parts:
            raise SpecError("model.json needs a 'parts' list (or an enabled neural 'prompt').")
        return self._parametric_glb(parts)

    def _neural_glb(self, spec: dict, workspace: Path, prompt: str) -> bytes:
        if not prompt:
            raise SpecError("A high-detail model needs a 'prompt' describing the subject.")
        image = spec.get("image")
        image_path = (workspace / image) if isinstance(image, str) and image else None
        glb = self._neural(prompt, image_path)  # type: ignore[misc]
        if not glb:
            raise SpecError("the high-detail service returned nothing.")
        return glb

    def _parametric_glb(self, parts: list) -> bytes:
        geometry: dict[str, trimesh.Trimesh] = {}
        for i, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            name = str(part.get("name") or f"part_{i}")
            for j, mesh in enumerate(self._build_part(part)):  # one part can yield many (mirror/array)
                if mesh is None or mesh.is_empty:
                    continue
                key, n = (name if j == 0 else f"{name}_{j}"), 1
                while key in geometry:  # names must be unique in the scene
                    key, n = f"{name}_{j}_{n}", n + 1
                geometry[key] = mesh
        if not geometry:
            raise SpecError("None of the parts produced any geometry.")
        scene = trimesh.Scene(geometry)
        glb = scene.export(file_type="glb")
        return glb if isinstance(glb, (bytes, bytearray)) else bytes(glb)

    def _build_part(self, part: dict) -> list[trimesh.Trimesh]:
        """A placed part, expanded by its modifiers into one or more world-space meshes."""
        solid = self._solid(part)
        if solid is None or solid.is_empty:
            return []
        solid.visual = trimesh.visual.TextureVisuals(material=_material(part))
        solid.apply_transform(self._matrix(part))            # place the primary instance
        instances = [solid]
        for axis in _as_list(part.get("mirror")):            # bilateral symmetry, for free
            refl = _reflect_matrix(axis)
            if refl is not None:
                instances += [_transformed_copy(i, refl) for i in list(instances)]
        arr = part.get("array")
        if isinstance(arr, dict):                            # repeated detail (rivets, ribs, vents)
            instances = _expand_array(instances, arr)
        return instances

    def _solid(self, part: dict) -> trimesh.Trimesh | None:
        """One primitive + its boolean children + optional smoothing — Y-up, centred, untransformed.

        A 'modifier stack' done locally: subtract (cutaways/holes), union (fuse into one form),
        intersect (clip to a boundary), then smooth (subdivide + Taubin) to round a blocky base into a
        sculpted, bevelled-looking surface."""
        base = self._mesh_for(part)
        if base is None:
            return None
        for op, key in ((_difference, "subtract"), (_union, "union"), (_intersection, "intersect")):
            for child in part.get(key, []) or []:
                if not isinstance(child, dict):
                    continue
                other = self._mesh_for(child)
                if other is None or other.is_empty:
                    continue
                other.apply_transform(self._matrix(child))   # child placed in the parent's local space
                try:
                    base = op(base, other)
                except Exception:
                    pass  # a failed boolean just leaves the part as-is — better than no model
                if base is None or base.is_empty:
                    return None
        passes = int(_f(part.get("smooth"), 0))
        if passes > 0:
            try:
                base = _smooth(base, passes)
            except Exception:
                pass  # smoothing is a nicety; never fail the part over it
        return base

    def _mesh_for(self, part: dict) -> trimesh.Trimesh | None:
        """Build one primitive, Y-up and centred at the origin (no part transform applied yet)."""
        shape = str(part.get("shape", "box")).lower()
        sections = int(part.get("sections", 64) or 64)
        sections = max(8, min(256, sections))
        m = self._primitive(shape, part, sections)
        if m is None or m.is_empty:
            return None
        m.apply_translation(-m.bounds.mean(axis=0))  # centre every shape at the origin
        if shape in _AXIS_SHAPES:
            # native trimesh axis is Z; rotate -90° about X so the principal axis runs +Y (up).
            m.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, (1, 0, 0)))
        return m

    def _primitive(self, shape: str, part: dict, sections: int) -> trimesh.Trimesh | None:
        if shape == "box":
            return trimesh.creation.box(extents=_vec3(part.get("size"), 1.0))
        if shape == "sphere":
            r = _f(part.get("radius"), 0.5)
            return trimesh.creation.icosphere(subdivisions=min(5, max(2, sections // 16)), radius=r)
        if shape == "cylinder":
            rt = part.get("radius_top")
            rb = part.get("radius_bottom")
            r = _f(part.get("radius"), 0.5)
            h = _f(part.get("height"), 1.0)
            if rt is not None or rb is not None:  # frustum
                return self._revolve_profile(
                    [[0, 0], [_f(rb, r), 0], [_f(rt, r), h], [0, h]], sections
                )
            return trimesh.creation.cylinder(radius=r, height=h, sections=sections)
        if shape == "cone":
            return trimesh.creation.cone(radius=_f(part.get("radius"), 0.5),
                                         height=_f(part.get("height"), 1.0), sections=sections)
        if shape == "capsule":
            return trimesh.creation.capsule(radius=_f(part.get("radius"), 0.3),
                                            height=_f(part.get("height"), 1.0), count=[16, 16])
        if shape == "torus":
            R = _f(part.get("radius"), 0.5)
            r = _f(part.get("tube"), max(0.05, R * 0.25))
            try:
                t = trimesh.creation.torus(major_radius=R, minor_radius=r,
                                           major_sections=sections, minor_sections=max(12, sections // 4))
            except TypeError:
                t = trimesh.creation.torus(R, r)
            # trimesh torus lies in the XY plane (hole axis Z); make it lie flat (hole axis Y) so it reads
            # as a ring on the ground unless the author rotates it.
            t.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, (1, 0, 0)))
            return t
        if shape in ("lathe", "revolve"):
            profile = part.get("profile")
            if not isinstance(profile, list) or len(profile) < 2:
                raise SpecError("a 'lathe' part needs a 'profile' of at least two [radius, height] points.")
            return self._revolve_profile(profile, sections)
        if shape == "extrude":
            poly = part.get("polygon")
            if not isinstance(poly, list) or len(poly) < 3:
                raise SpecError("an 'extrude' part needs a 'polygon' of at least three [x, y] points.")
            return self._extrude_polygon(poly, _f(part.get("height"), 1.0))
        raise SpecError(f"unknown shape '{shape}'.")

    @staticmethod
    def _revolve_profile(profile: Sequence[Sequence[float]], sections: int) -> trimesh.Trimesh:
        pts = np.array([[abs(_f(p[0], 0.0)), _f(p[1], 0.0)] for p in profile], dtype=float)
        return trimesh.creation.revolve(pts, sections=sections)

    @staticmethod
    def _extrude_polygon(polygon: Sequence[Sequence[float]], height: float) -> trimesh.Trimesh:
        from shapely.geometry import Polygon

        ring = [(_f(p[0], 0.0), _f(p[1], 0.0)) for p in polygon]
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)  # repair self-intersections / winding
        return trimesh.creation.extrude_polygon(poly, height=max(1e-3, height))

    @staticmethod
    def _matrix(part: dict) -> np.ndarray:
        pos = _vec3(part.get("position"), 0.0)
        rot = _vec3(part.get("rotation"), 0.0)
        scale = part.get("scale", 1.0)
        sx, sy, sz = _vec3(scale, 1.0) if isinstance(scale, (list, tuple)) else (
            _f(scale, 1.0), _f(scale, 1.0), _f(scale, 1.0)
        )
        T = trimesh.transformations.translation_matrix(pos)
        R = trimesh.transformations.euler_matrix(
            math.radians(rot[0]), math.radians(rot[1]), math.radians(rot[2]), axes="sxyz"
        )
        S = np.diag([sx or 1.0, sy or 1.0, sz or 1.0, 1.0])
        return T @ R @ S

    # ----- viewer + error page -----
    def _write_viewer(self, workspace: Path, spec: dict) -> None:
        title = self._title(workspace, spec)
        bg = _hex_str(spec.get("background"), DEFAULT_BG)
        accent = _hex_str(spec.get("accent"), DEFAULT_ACCENT)
        (workspace / VIEWER_FILE).write_text(_VIEWER_HTML
                                             .replace("__TITLE__", _esc(title))
                                             .replace("__BG__", bg)
                                             .replace("__ACCENT__", accent)
                                             .replace("__GLB__", GLB_REL), encoding="utf-8")

    def _write_error(self, workspace: Path, message: str) -> None:
        (workspace / VIEWER_FILE).write_text(
            _ERROR_HTML.replace("__BG__", DEFAULT_BG).replace("__ACCENT__", DEFAULT_ACCENT)
            .replace("__MSG__", _esc(message)), encoding="utf-8")

    @staticmethod
    def _title(workspace: Path, spec: dict) -> str:
        t = spec.get("title")
        if isinstance(t, str) and t.strip():
            return t.strip()
        try:
            man = json.loads((workspace / MANIFEST).read_text(encoding="utf-8"))
            if isinstance(man.get("name"), str):
                return man["name"]
        except Exception:
            pass
        return "Model"


# ----- small value helpers -----
def _f(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _vec3(value: Any, default: float) -> tuple[float, float, float]:
    if isinstance(value, (int, float)):
        return (float(value), float(value), float(value))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (_f(value[0], default), _f(value[1], default), _f(value[2], default))
    return (default, default, default)


def _hex_str(value: Any, default: str) -> str:
    if isinstance(value, str):
        s = value.strip()
        if len(s) == 7 and s.startswith("#"):
            try:
                int(s[1:], 16)
                return s.lower()
            except ValueError:
                pass
    return default


def _rgb01(value: Any, default: tuple[float, float, float]) -> tuple[float, float, float]:
    s = _hex_str(value, "")
    if not s:
        return default
    return (int(s[1:3], 16) / 255.0, int(s[3:5], 16) / 255.0, int(s[5:7], 16) / 255.0)


def _material(part: dict) -> PBRMaterial:
    r, g, b = _rgb01(part.get("color"), (0.8, 0.8, 0.82))
    opacity = _f(part.get("opacity"), 1.0)
    opacity = min(1.0, max(0.0, opacity))
    mat = PBRMaterial(
        baseColorFactor=[r, g, b, opacity],
        metallicFactor=min(1.0, max(0.0, _f(part.get("metalness"), 0.0))),
        roughnessFactor=min(1.0, max(0.0, _f(part.get("roughness"), 0.6))),
    )
    if part.get("emissive"):
        er, eg, eb = _rgb01(part.get("emissive"), (0.0, 0.0, 0.0))
        strength = min(1.0, max(0.0, _f(part.get("emissive_strength"), 1.0)))
        mat.emissiveFactor = [er * strength, eg * strength, eb * strength]
    if opacity < 1.0:
        mat.alphaMode = "BLEND"
    return mat


def _difference(a: trimesh.Trimesh, b: trimesh.Trimesh) -> trimesh.Trimesh:
    try:
        return trimesh.boolean.difference([a, b], engine="manifold")
    except TypeError:  # older/newer trimesh signature without the engine kw
        return trimesh.boolean.difference([a, b])


def _union(a: trimesh.Trimesh, b: trimesh.Trimesh) -> trimesh.Trimesh:
    try:
        return trimesh.boolean.union([a, b], engine="manifold")
    except TypeError:
        return trimesh.boolean.union([a, b])


def _intersection(a: trimesh.Trimesh, b: trimesh.Trimesh) -> trimesh.Trimesh:
    try:
        return trimesh.boolean.intersection([a, b], engine="manifold")
    except TypeError:
        return trimesh.boolean.intersection([a, b])


def _smooth(mesh: trimesh.Trimesh, passes: int) -> trimesh.Trimesh:
    """Subdivide then Taubin-smooth — rounds a blocky base into a sculpted, soft-edged surface."""
    for _ in range(max(1, min(3, passes))):
        mesh = mesh.subdivide()
    trimesh.smoothing.filter_taubin(mesh, iterations=10)
    return mesh


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _reflect_matrix(axis: str) -> np.ndarray | None:
    idx = {"x": 0, "y": 1, "z": 2}.get(str(axis).lower())
    if idx is None:
        return None
    diag = [1.0, 1.0, 1.0]
    diag[idx] = -1.0
    return np.diag([*diag, 1.0])


def _transformed_copy(mesh: trimesh.Trimesh, matrix: np.ndarray) -> trimesh.Trimesh:
    # trimesh.apply_transform auto-flips winding on a negative-determinant (mirror) transform, so the
    # reflected copy keeps outward-facing normals — no manual invert needed.
    c = mesh.copy()
    c.apply_transform(matrix)
    return c


def _expand_array(instances: list[trimesh.Trimesh], arr: dict) -> list[trimesh.Trimesh]:
    count = max(1, min(64, int(_f(arr.get("count"), 1))))
    offset = np.array(_vec3(arr.get("offset"), 0.0))
    rot = _vec3(arr.get("rotation"), 0.0)
    out: list[trimesh.Trimesh] = []
    for inst in instances:
        for i in range(count):
            if i == 0:
                out.append(inst)
                continue
            matrix = trimesh.transformations.translation_matrix(offset * i) @ \
                trimesh.transformations.euler_matrix(
                    math.radians(rot[0] * i), math.radians(rot[1] * i), math.radians(rot[2] * i),
                    axes="sxyz")
            out.append(_transformed_copy(inst, matrix))
    return out


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# A single fixed viewer template, written verbatim by HELIX (never authored by the model). It loads the
# baked GLB, frames the whole model, and gives orbit/zoom with the HELIX look + a studio environment so
# metallic/PBR surfaces read well.
_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root { --accent: __ACCENT__; }
  html, body { margin: 0; height: 100%; background: __BG__; overflow: hidden;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; color: #cfeff0; }
  #app { position: fixed; inset: 0; }
  #title { position: fixed; top: 14px; left: 16px; font-size: 14px; font-weight: 600;
    letter-spacing: .04em; color: var(--accent); opacity: .85; pointer-events: none;
    text-shadow: 0 1px 8px rgba(0,0,0,.6); }
  #panel { position: fixed; bottom: 14px; right: 14px; display: flex; gap: 6px;
    background: rgba(8,11,15,.55); border: 1px solid rgba(63,224,224,.25); border-radius: 10px;
    padding: 6px; backdrop-filter: blur(6px); }
  #panel button { background: transparent; color: #bfe9ea; border: 1px solid rgba(63,224,224,.25);
    border-radius: 7px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  #panel button:hover { border-color: var(--accent); color: var(--accent); }
  #panel button.on { color: var(--accent); border-color: var(--accent); }
  #msg { position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
    text-align: center; padding: 24px; font-size: 15px; color: #9fc7c8; }
  /* Futuristic HUD frame — pure CSS over the canvas, never intercepts pointer events (so the
     OrbitControls + buttons keep working) and never touches the WebGL render. */
  #hud { position: fixed; inset: 0; pointer-events: none; z-index: 1; }
  #hud .vignette { position: absolute; inset: 0;
    background: radial-gradient(ellipse at center, transparent 56%, rgba(0,0,0,.55) 100%); }
  #hud .scan { position: absolute; inset: 0; opacity: .05; mix-blend-mode: screen;
    background: repeating-linear-gradient(0deg, #3fe0e0 0, #3fe0e0 1px, transparent 1px, transparent 3px); }
  #hud .corner { position: absolute; width: 26px; height: 26px; border: 1.5px solid rgba(63,224,224,.5); }
  #hud .tl { top: 16px; left: 16px; border-right: none; border-bottom: none; }
  #hud .tr { top: 16px; right: 16px; border-left: none; border-bottom: none; }
  #hud .bl { bottom: 16px; left: 16px; border-right: none; border-top: none; }
  #hud .br { bottom: 16px; right: 16px; border-left: none; border-top: none; }
  @keyframes hud-in { from { opacity: 0; } to { opacity: 1; } }
  #app, #hud, #title, #panel { animation: hud-in .6s ease both; }
</style>
</head>
<body>
  <div id="app"></div>
  <div id="hud"><div class="vignette"></div><div class="scan"></div>
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i></div>
  <div id="title">__TITLE__</div>
  <div id="panel">
    <button id="play" style="display:none">Pause</button>
    <button id="spin">Auto-rotate</button>
    <button id="wire">Wireframe</button>
    <button id="reset">Reset view</button>
  </div>
  <div id="msg"></div>
  <script type="importmap">
  { "imports": {
      "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
      "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
  } }
  </script>
  <script type="module">
  import * as THREE from "three";
  import { OrbitControls } from "three/addons/controls/OrbitControls.js";
  import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
  import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
  import { toCreasedNormals } from "three/addons/utils/BufferGeometryUtils.js";

  const fail = (m) => { const e = document.getElementById("msg");
    e.textContent = m; e.style.display = "flex"; };

  try {
    const app = document.getElementById("app");
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;
    app.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("__BG__");

    const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 5000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

    const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(3, 5, 4); scene.add(key);
    const fill = new THREE.DirectionalLight(0x88ccff, 0.8); fill.position.set(-4, 2, -3); scene.add(fill);
    scene.add(new THREE.HemisphereLight(0xbfe9ea, 0x0a0e12, 0.5));

    let model = null, grid = null, radius = 1, mixer = null, playing = true;
    const clock = new THREE.Clock();
    const home = { pos: new THREE.Vector3(), target: new THREE.Vector3() };

    const frame = () => {
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      model.position.sub(center);                       // recentre at the origin
      const box2 = new THREE.Box3().setFromObject(model);
      const c2 = box2.getCenter(new THREE.Vector3());
      radius = Math.max(size.x, size.y, size.z, 1e-3) * 0.5;
      const dist = radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 1.25;
      camera.near = radius / 100; camera.far = radius * 100; camera.updateProjectionMatrix();
      camera.position.set(c2.x + dist * 0.7, c2.y + dist * 0.45, c2.z + dist);
      controls.target.copy(c2); controls.update();
      home.pos.copy(camera.position); home.target.copy(c2);
      if (grid) scene.remove(grid);
      grid = new THREE.GridHelper(radius * 6, 24, 0x224a4a, 0x132a2a);
      grid.position.y = box2.min.y; grid.material.opacity = 0.35; grid.material.transparent = true;
      scene.add(grid);
    };

    new GLTFLoader().load("__GLB__", (gltf) => {
      model = gltf.scene;
      // Derive creased normals ONLY when a mesh ships without them (the baked parametric GLB) — so flat
      // faces stay crisp and curves read smooth. Skip meshes that already have normals (rigged/animated
      // models do), since re-indexing would disturb skinning.
      model.traverse((o) => { if (o.isMesh && o.geometry && !o.geometry.attributes.normal) {
        o.geometry = toCreasedNormals(o.geometry, Math.PI / 3);
      } });
      scene.add(model); frame();
      if (gltf.animations && gltf.animations.length) {   // an animated/rigged model — play it
        mixer = new THREE.AnimationMixer(model);
        gltf.animations.forEach((clip) => mixer.clipAction(clip).play());
        const play = document.getElementById("play");
        play.style.display = "";
        play.onclick = () => { playing = !playing; play.textContent = playing ? "Pause" : "Play";
          play.classList.toggle("on", !playing); };
      }
    }, undefined, () => fail("Couldn't load the model mesh."));

    addEventListener("resize", () => {
      camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    });

    const spinBtn = document.getElementById("spin");
    spinBtn.onclick = () => { controls.autoRotate = !controls.autoRotate;
      controls.autoRotateSpeed = 1.4; spinBtn.classList.toggle("on", controls.autoRotate); };
    const wireBtn = document.getElementById("wire");
    let wire = false;
    wireBtn.onclick = () => { wire = !wire; wireBtn.classList.toggle("on", wire);
      if (model) model.traverse((o) => { if (o.isMesh) o.material.wireframe = wire; }); };
    document.getElementById("reset").onclick = () => {
      camera.position.copy(home.pos); controls.target.copy(home.target); controls.update(); };

    (function loop() {
      requestAnimationFrame(loop);
      const dt = clock.getDelta();
      if (mixer && playing) mixer.update(dt);
      controls.update();
      renderer.render(scene, camera);
    })();
  } catch (err) {
    fail("Couldn't start the 3D view: " + (err && err.message ? err.message : err));
  }
  </script>
</body>
</html>
"""

_ERROR_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Model</title>
<style>
  html, body { margin: 0; height: 100%; background: __BG__; color: #9fc7c8;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center; text-align: center; }
  .card { max-width: 460px; padding: 28px; }
  h1 { color: __ACCENT__; font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  p { font-size: 14px; line-height: 1.5; opacity: .85; }
</style></head>
<body><div class="card"><h1>This model didn't build</h1><p>__MSG__</p>
<p>Try describing it again, or ask for a small change.</p></div></body></html>
"""
