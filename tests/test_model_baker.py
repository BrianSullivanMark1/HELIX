"""ModelBaker tests — a declarative model.json bakes into a valid GLB + viewer; failures degrade gently.

These exercise the real geometry kernel (trimesh) end to end; they need no network (the viewer references
a CDN, but nothing here loads it)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import trimesh

from helix.services.model_baker import GLB_REL, SPEC_FILE, VIEWER_FILE, ModelBaker


def _spec(tmp: Path, spec: dict, name: str = "Test Model") -> Path:
    (tmp / ".helixbuild.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (tmp / SPEC_FILE).write_text(json.dumps(spec), encoding="utf-8")
    return tmp


def test_bakes_a_valid_glb_and_viewer(tmp_path: Path):
    _spec(tmp_path, {
        "title": "Bolt",
        "parts": [
            {"name": "head", "shape": "cylinder", "radius": 1.0, "height": 0.5,
             "color": "#c0c0c0", "metalness": 0.9, "roughness": 0.3},
            {"name": "shaft", "shape": "cylinder", "radius": 0.4, "height": 2.0, "position": [0, -1.2, 0]},
        ],
    })
    ModelBaker().bake(tmp_path)

    glb = tmp_path / GLB_REL
    assert glb.exists() and glb.stat().st_size > 0
    scene = trimesh.load(io.BytesIO(glb.read_bytes()), file_type="glb")
    assert len(scene.geometry) == 2  # both parts made it into the mesh

    html = (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")
    assert "GLTFLoader" in html and GLB_REL in html
    assert "Bolt" in html  # title threaded through


def test_viewer_supports_animation_and_creases_only_missing_normals(tmp_path: Path):
    _spec(tmp_path, {"parts": [{"shape": "box", "size": [1, 1, 1]}]})
    ModelBaker().bake(tmp_path)
    html = (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")
    assert "AnimationMixer" in html            # animated/rigged GLBs play
    assert "!o.geometry.attributes.normal" in html  # don't re-crease meshes that already have normals


def test_static_to_animated_conversion_skips_baking_and_drops_the_stale_spec(tmp_path: Path):
    # The coder converted a static model to animated: it wrote its OWN index.html (no generated-viewer
    # sentinel, no GLB reference). bake() must respect that page and delete the now-stale model.json so a
    # re-bake never overwrites the animation with the old static mesh.
    _spec(tmp_path, {"parts": [{"shape": "box", "size": [1, 1, 1]}]})
    hand = "<!doctype html><html><body><script>/* animated three.js, builds geometry inline */</script></body></html>"
    (tmp_path / VIEWER_FILE).write_text(hand, encoding="utf-8")
    ModelBaker().bake(tmp_path)
    assert not (tmp_path / SPEC_FILE).exists()  # stale spec dropped
    assert (tmp_path / VIEWER_FILE).read_text(encoding="utf-8") == hand  # animated page untouched
    assert not (tmp_path / GLB_REL).exists()  # no static mesh baked


def test_error_page_does_not_block_a_later_rebake(tmp_path: Path):
    # A transient bake failure writes HELIX's error page; a later iteration must RE-BAKE, not mistake the
    # error page for a hand-authored animated page and delete the (now valid) spec.
    _spec(tmp_path, {"engine": "neural", "prompt": "a dragon"})  # neural with no backend → error page
    ModelBaker().bake(tmp_path)
    assert (tmp_path / VIEWER_FILE).exists()
    assert (tmp_path / SPEC_FILE).exists()  # spec kept — the error page is recognized as ours
    # the user fixes the spec to a parametric one and iterates
    (tmp_path / SPEC_FILE).write_text(
        json.dumps({"parts": [{"shape": "box", "size": [1, 1, 1]}]}), encoding="utf-8"
    )
    ModelBaker().bake(tmp_path)
    assert (tmp_path / GLB_REL).exists()  # re-baked successfully
    assert (tmp_path / SPEC_FILE).exists()  # spec NOT deleted


def test_regenerates_when_the_existing_viewer_is_ours(tmp_path: Path):
    # A normal static iteration: our generated viewer is present → re-bake (don't mistake it for handmade).
    _spec(tmp_path, {"parts": [{"shape": "box", "size": [1, 1, 1]}]})
    ModelBaker().bake(tmp_path)  # first bake writes our sentinel viewer + GLB
    assert (tmp_path / SPEC_FILE).exists() and (tmp_path / GLB_REL).exists()
    ModelBaker().bake(tmp_path)  # second bake (an iteration) must still re-bake, not drop the spec
    assert (tmp_path / SPEC_FILE).exists() and (tmp_path / GLB_REL).exists()


def test_subtract_carves_a_hole(tmp_path: Path):
    """A boolean cutaway must actually remove volume (the eye-slit / hollow case)."""
    solid = {"parts": [{"name": "block", "shape": "box", "size": [2, 2, 2]}]}
    carved = {"parts": [{"name": "block", "shape": "box", "size": [2, 2, 2],
                         "subtract": [{"shape": "cylinder", "radius": 0.5, "height": 3}]}]}
    vols = []
    for i, spec in enumerate((solid, carved)):
        d = tmp_path / f"v{i}"
        d.mkdir()
        _spec(d, spec)
        ModelBaker().bake(d)
        scene = trimesh.load(io.BytesIO((d / GLB_REL).read_bytes()), file_type="glb")
        vols.append(sum(g.volume for g in scene.geometry.values()))
    assert vols[1] < vols[0] - 0.1  # carved version has clearly less volume


def test_is_y_up(tmp_path: Path):
    """A tall part must read as tall in Y in the exported mesh (what Three.js sees)."""
    _spec(tmp_path, {"parts": [{"shape": "cylinder", "radius": 0.3, "height": 5.0}]})
    ModelBaker().bake(tmp_path)
    scene = trimesh.load(io.BytesIO((tmp_path / GLB_REL).read_bytes()), file_type="glb")
    size = scene.bounds[1] - scene.bounds[0]
    assert size[1] > size[0] and size[1] > size[2]  # Y is the long axis


def test_empty_parts_writes_error_page(tmp_path: Path):
    _spec(tmp_path, {"parts": []})
    ModelBaker().bake(tmp_path)
    assert not (tmp_path / GLB_REL).exists()
    html = (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")
    assert "didn't build" in html  # friendly message, not a blank page


def test_malformed_json_writes_error_page(tmp_path: Path):
    (tmp_path / SPEC_FILE).write_text("{not valid json", encoding="utf-8")
    ModelBaker().bake(tmp_path)
    assert "didn't build" in (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")


def test_animated_index_html_is_left_untouched(tmp_path: Path):
    """No model.json + a hand-authored index.html (the ANIMATED path) must be preserved verbatim."""
    original = "<html><body>hand-written animated three.js</body></html>"
    (tmp_path / VIEWER_FILE).write_text(original, encoding="utf-8")
    ModelBaker().bake(tmp_path)  # no SPEC_FILE present
    assert (tmp_path / VIEWER_FILE).read_text(encoding="utf-8") == original


def test_nothing_produced_writes_error_page(tmp_path: Path):
    ModelBaker().bake(tmp_path)  # no model.json, no index.html
    assert (tmp_path / VIEWER_FILE).read_text(encoding="utf-8").strip() != ""
    assert "build" in (tmp_path / VIEWER_FILE).read_text(encoding="utf-8").lower()


def test_neural_engine_without_backend_is_a_friendly_error(tmp_path: Path):
    _spec(tmp_path, {"engine": "neural", "prompt": "an iron man suit"})
    ModelBaker().bake(tmp_path)  # no neural backend wired
    assert not (tmp_path / GLB_REL).exists()
    assert "didn't build" in (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")


def test_neural_backend_is_used_when_present(tmp_path: Path):
    sentinel = b"glTF-fake-bytes-from-backend"
    calls = []

    def backend(prompt: str, image: Path | None) -> bytes:
        calls.append(prompt)
        return sentinel

    _spec(tmp_path, {"engine": "neural", "prompt": "an iron man suit"})
    ModelBaker(neural_backend=backend).bake(tmp_path)
    assert calls == ["an iron man suit"]
    assert (tmp_path / GLB_REL).read_bytes() == sentinel  # backend GLB written straight through
    assert "GLTFLoader" in (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")


def _scene(tmp_path: Path, spec: dict):
    _spec(tmp_path, spec)
    ModelBaker().bake(tmp_path)
    return trimesh.load(io.BytesIO((tmp_path / GLB_REL).read_bytes()), file_type="glb")


def test_mirror_adds_a_reflected_copy(tmp_path: Path):
    scene = _scene(tmp_path, {"parts": [
        {"name": "eye", "shape": "box", "size": [0.3, 0.2, 0.2], "position": [1, 0, 0], "mirror": "x"},
    ]})
    assert len(scene.geometry) == 2  # one part → original + mirror
    xs = sorted(g.bounds.mean(axis=0)[0] for g in scene.geometry.values())
    assert xs[0] < 0 < xs[1]  # the copy sits on the opposite side of x=0


def test_array_repeats_the_part(tmp_path: Path):
    scene = _scene(tmp_path, {"parts": [
        {"name": "rivet", "shape": "box", "size": [0.1, 0.1, 0.1],
         "array": {"count": 5, "offset": [0.3, 0, 0]}},
    ]})
    assert len(scene.geometry) == 5


def test_smooth_subdivides_geometry(tmp_path: Path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    va = sum(len(g.vertices) for g in _scene(a, {"parts": [{"shape": "box", "size": [1, 1, 1]}]}).geometry.values())
    vb = sum(len(g.vertices) for g in _scene(b, {"parts": [{"shape": "box", "size": [1, 1, 1], "smooth": 2}]}).geometry.values())
    assert vb > va * 4  # subdivision multiplies the vertex count


def test_union_fuses_into_one_watertight_solid(tmp_path: Path):
    scene = _scene(tmp_path, {"parts": [
        {"name": "blob", "shape": "box", "size": [1, 1, 1],
         "union": [{"shape": "sphere", "radius": 0.7}]},
    ]})
    assert len(scene.geometry) == 1
    g = next(iter(scene.geometry.values()))
    assert g.is_watertight


def test_lathe_requires_a_profile(tmp_path: Path):
    _spec(tmp_path, {"parts": [{"shape": "lathe"}]})  # missing 'profile'
    ModelBaker().bake(tmp_path)
    assert "didn't build" in (tmp_path / VIEWER_FILE).read_text(encoding="utf-8")
