"""Render-kit tests — the ANIMATED path's stage (helix3d.js) is a technical illustration, not a product shot.

The kit is a JS string HELIX copies beside every hand-authored animated index.html, so these pins read the
source text: the coder prompt's names must stay exported, the washed-out rig (IBL + bloom + AO + ACES boost)
must stay gone, and the light intensities must stay sane. A syntax check runs through node when it is on the
machine, so a stray brace in the embedded module is caught here and not in a QWebEngineView at 2am."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helix.services import render_kit
from helix.services.render_kit import HELIX3D_JS, KIT_FILE


def test_kit_file_name_is_stable():
    # model_baker._write_render_kit and the forge copy the kit by this name and the coder prompt imports
    # "./helix3d.js" — renaming it would orphan every existing animated hologram on disk.
    assert KIT_FILE == "helix3d.js"
    assert render_kit.KIT_FILE is KIT_FILE and isinstance(render_kit.HELIX3D_JS, str)


def test_exports_the_names_the_coder_prompt_imports():
    # prompts.py's animated skeleton: import { createStage, Timeline, THREE } from "./helix3d.js"
    assert "export function createStage" in HELIX3D_JS
    assert "export class Timeline" in HELIX3D_JS
    assert "export { THREE }" in HELIX3D_JS


def test_stage_surface_used_by_the_prompt_skeleton_is_intact():
    # The skeleton calls stage.scene.add / stage.frame(model) / stage.start(cb) and reads stage.THREE; the
    # returned object must keep every one of those names (plus camera/renderer/controls for the adventurous).
    m = re.search(r"return \{ THREE, scene, camera, renderer, controls, frame, start, add, dress, matcap, shading \};", HELIX3D_JS)
    assert m, "createStage's return object changed shape"
    # Timeline: duration / captions / onUpdate in, update(dt) to advance, a scrub bar + caption injected.
    assert "opts.duration" in HELIX3D_JS and "opts.captions" in HELIX3D_JS and "opts.onUpdate" in HELIX3D_JS
    assert "update(dt)" in HELIX3D_JS
    for el in ("helix-bar", "helix-play", "helix-restart", "helix-scrub", "helix-cap", "helix-title", "helix-hud"):
        assert el in HELIX3D_JS, el
    for corner in ("tl", "tr", "bl", "br"):
        assert f'<i class="{corner}">' in HELIX3D_JS


def test_product_shot_rig_is_gone():
    # Each of these was part of the "way too bright" rig. Any one coming back re-washes every animated hologram.
    for banned in ("Bloom", "RoomEnvironment", "GTAO", "toneMappingExposure = 1.05", "EffectComposer",
                   "RenderPass", "OutputPass", "PMREMGenerator", "ACESFilmicToneMapping", "scene.environment"):
        assert banned not in HELIX3D_JS, banned
    assert "toneMappingExposure" not in HELIX3D_JS  # no exposure boost of ANY value
    assert "THREE.NoToneMapping" in HELIX3D_JS
    assert "shadowMap.enabled = false" in HELIX3D_JS and "castShadow" not in HELIX3D_JS


def test_only_core_three_and_orbit_controls_are_imported():
    # The page importmap (a CDN, pre-existing for this hand-authored path) resolves "three" and
    # "three/addons/"; the kit must not drag in post-processing or environment addons again.
    imports = re.findall(r'^import .* from "([^"]+)";', HELIX3D_JS, flags=re.M)
    assert imports == ["three", "three/addons/controls/OrbitControls.js"], imports
    assert "postprocessing" not in HELIX3D_JS and "environments/" not in HELIX3D_JS


def test_light_intensities_are_sane():
    # The old key was 2.6 on top of IBL. One soft key ~1.0 and a hemisphere ~0.6 — nothing else.
    keys = re.findall(r"DirectionalLight\(0x[0-9a-fA-F]+,\s*([0-9.]+)\)", HELIX3D_JS)
    assert len(keys) == 1, keys  # exactly one key light, no fill
    assert float(keys[0]) <= 1.2
    hemi = re.findall(r"HemisphereLight\(0x[0-9a-fA-F]+,\s*0x[0-9a-fA-F]+,\s*([0-9.]+)\)", HELIX3D_JS)
    assert len(hemi) == 1 and float(hemi[0]) <= 1.0
    assert "AmbientLight" not in HELIX3D_JS and "PointLight" not in HELIX3D_JS and "SpotLight" not in HELIX3D_JS


def test_matcap_and_lit_shading_modes_exist_and_matcap_is_procedural():
    # opts.shading = "matcap" | "lit", default matcap — drawn on a canvas at load so no image asset is needed,
    # and MeshMatcapMaterial keeps the coder's colour when a lit mesh is re-skinned.
    assert 'opts.shading === "lit" ? "lit" : "matcap"' in HELIX3D_JS
    assert "export function makeMatcap" in HELIX3D_JS
    assert 'document.createElement("canvas")' in HELIX3D_JS and "createRadialGradient" in HELIX3D_JS
    assert "THREE.CanvasTexture" in HELIX3D_JS and "MeshMatcapMaterial" in HELIX3D_JS
    assert "color: m.color ? m.color.clone()" in HELIX3D_JS  # colour preserved on re-skin
    assert "flatShading: true" in HELIX3D_JS
    # No image asset anywhere in the kit: the importmap CDN is the ONLY external reference.
    assert not re.search(r"\.(png|jpg|jpeg|hdr|exr|webp)\b", HELIX3D_JS)


def test_emissive_materials_survive_the_matcap_reskin():
    # Glows (screens, reactors, flames) are deliberate and a matcap cannot express emission — they are left as
    # the coder's own material and simply render under the lights.
    assert "m.emissive && m.emissive.getHex() !== 0" in HELIX3D_JS


def test_stage_add_wraps_meshes_with_crease_edges_optionally():
    # stage.add(object, { edges }) — EdgesGeometry at 30° per Mesh child, parented to the mesh so the lines follow
    # animated parts; edges:false opts out per object. The edge colour is a cool grey, never the accent.
    assert "function add(object, o = {})" in HELIX3D_JS
    assert "new THREE.EdgesGeometry(mesh.geometry, 30)" in HELIX3D_JS
    assert "mesh.add(lines)" in HELIX3D_JS  # child of the mesh, not of the scene
    assert "const edges = o.edges !== false;" in HELIX3D_JS
    assert "LineSegments" in HELIX3D_JS
    edge = re.search(r"const EDGE_COLOR = 0x([0-9a-fA-F]{6});", HELIX3D_JS)
    assert edge and edge.group(1).lower() != "3fe0e0"
    # Meshes are collected before any edge child is added (so the traversal never sees its own additions),
    # and a WeakSet stops double-dressing when the same group is added twice.
    assert "const meshes = [];" in HELIX3D_JS and "new WeakSet()" in HELIX3D_JS


def test_crease_lines_get_a_polygon_offset_under_them_on_both_shading_paths():
    # LineSegments drawn at exactly the face depth z-fight and render as stippled dashes. The face material
    # must be pushed back a hair on BOTH paths: the matcap re-skin carries the offset in its constructor
    # options, and dress() applies the same three props to the materials it does NOT re-skin (the whole
    # "lit" mode, plus the emissive/wireframe survivors in matcap mode) before the lines are added.
    matcap_opts = re.search(r"new THREE\.MeshMatcapMaterial\(\{(.*?)\}\);", HELIX3D_JS, flags=re.S)
    assert matcap_opts, "toMatcap's MeshMatcapMaterial options changed shape"
    assert "polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1" in matcap_opts.group(1)
    # The non-re-skinned path: one helper that sets the three props, called from dress() when edges are on
    # and BEFORE mesh.add(lines) — the offset is a material flag, so order only matters for readability,
    # but the call must exist at all or the lit mode stipples.
    assert "function offsetFaces(m)" in HELIX3D_JS
    assert "m.polygonOffset = true; m.polygonOffsetFactor = 1; m.polygonOffsetUnits = 1;" in HELIX3D_JS
    dress = HELIX3D_JS[HELIX3D_JS.index("function dress(object, o = {})"):HELIX3D_JS.index("function add(object, o = {})")]
    assert "if (edges) {" in dress
    assert "mesh.material.forEach(offsetFaces); else offsetFaces(mesh.material);" in dress
    assert dress.index("offsetFaces(mesh.material)") < dress.index("mesh.add(lines)")


def test_background_and_stage_furniture_match_the_static_viewer():
    assert 'opts.background || "#10161c"' in HELIX3D_JS
    assert "background:#10161c" in HELIX3D_JS          # the page behind the canvas too, so no flash of another colour
    assert "radial-gradient" in HELIX3D_JS              # the subtle vignette stays
    assert "GridHelper" in HELIX3D_JS and "AxesHelper" in HELIX3D_JS
    assert "CircleGeometry" in HELIX3D_JS and "MeshBasicMaterial({ color: 0x0d1319 })" in HELIX3D_JS  # shadow-free ground
    assert "OrbitControls(camera, renderer.domElement)" in HELIX3D_JS
    assert "function frame(object)" in HELIX3D_JS and "function start(onFrame)" in HELIX3D_JS
    assert "renderer.render(scene, camera)" in HELIX3D_JS  # direct render, no composer fallback dance


def test_accent_is_chrome_only():
    # The HELIX accent colours the HUD/title/bar via --accent; it must never be used as a material/light colour.
    assert 'opts.accent || "#3fe0e0"' in HELIX3D_JS
    assert "0x3fe0e0" not in HELIX3D_JS.lower()


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="node is not on this machine; the syntax check runs where it is")
def test_kit_is_syntactically_valid_es_module(tmp_path: Path):
    # `node --check` parses without resolving imports, so the bare "three" specifiers are fine. A stray brace
    # in the embedded string would otherwise surface only as a blank QWebEngineView.
    mod = tmp_path / "helix3d.mjs"
    mod.write_text(HELIX3D_JS, encoding="utf-8")
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run([_node(), "--check", str(mod)], capture_output=True, text=True, timeout=60, **kw)
    assert proc.returncode == 0, proc.stderr
