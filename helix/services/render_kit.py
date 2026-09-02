"""The HELIX render kit for the ANIMATED 3D path.

The animated path is the coder hand-writing a Three.js index.html for a "how X works" hologram — historically
a blank-file regression to flat sphere-blobs. This module ships a ready-made stage plus a timeline + HUD, as a
self-contained ES module HELIX copies into every animated build (model_baker._write_render_kit and the forge
copy it by KIT_FILE / HELIX3D_JS). The coder then only builds the model objects and defines the steps.

The stage is a TECHNICAL ILLUSTRATION, matching the static (OpenSCAD) viewer: flat matcap shading drawn on a
canvas at load, crease-edge lines, a dark slate background, a unit grid + axes, auto-framing, the HELIX HUD.
It deliberately has NO image-based lighting, NO bloom, NO ambient occlusion and NO tone-mapping exposure
boost — the old product-shot rig (RoomEnvironment + a 2.6 key + UnrealBloom + ACES 1.05) washed everything
out ("way too bright") and hid the edges that make a mechanism readable. Lights sit at sane intensities
(key 1.0, hemisphere 0.6) purely for the optional "lit" shading mode.

Embedded as a Python string (no on-disk asset to bundle/resolve), mirroring _VIEWER_HTML in model_baker.
"""
from __future__ import annotations

KIT_FILE = "helix3d.js"

HELIX3D_JS = """\
// HELIX render kit — import { createStage, Timeline, THREE } from "./helix3d.js"
// A ready technical-illustration stage (matcap or flat-lit shading, crease edges, grid + axes, orbit,
// auto-frame, HUD) + a timeline, so an animated model only builds its objects and defines steps.
//
// three is resolved from the page's importmap (a CDN, as today for this hand-authored path — the static
// viewer is now CDN-free via the vendored three.min.js, and this path can follow later). Only the core
// module and the OrbitControls addon are imported: no post-processing, no environment maps.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export { THREE };

const HUD_CSS = `
  :root { --accent: #3fe0e0; }
  html,body { margin:0; height:100%; overflow:hidden; background:#10161c;
    font-family:-apple-system,"Segoe UI",system-ui,sans-serif; color:#cfeff0; }
  #helix-title { position:fixed; top:14px; left:16px; font-size:14px; font-weight:600; letter-spacing:.04em;
    color:var(--accent); opacity:.85; pointer-events:none; text-shadow:0 1px 8px rgba(0,0,0,.6); z-index:3; }
  #helix-hud { position:fixed; inset:0; pointer-events:none; z-index:1; }
  #helix-hud .v { position:absolute; inset:0; background:radial-gradient(ellipse at center,transparent 58%,rgba(0,0,0,.42) 100%); }
  #helix-hud i { position:absolute; width:26px; height:26px; border:1.5px solid rgba(63,224,224,.5); }
  #helix-hud .tl{top:16px;left:16px;border-right:none;border-bottom:none}
  #helix-hud .tr{top:16px;right:16px;border-left:none;border-bottom:none}
  #helix-hud .bl{bottom:16px;left:16px;border-right:none;border-top:none}
  #helix-hud .br{bottom:16px;right:16px;border-left:none;border-top:none}
  #helix-bar { position:fixed; bottom:14px; left:50%; transform:translateX(-50%); display:flex; gap:8px;
    align-items:center; background:rgba(16,22,28,.78); border:1px solid rgba(63,224,224,.25); border-radius:12px;
    padding:8px 12px; backdrop-filter:blur(6px); z-index:3; }
  #helix-bar button { background:transparent; color:#bfe9ea; border:1px solid rgba(63,224,224,.3);
    border-radius:8px; padding:6px 12px; font-size:13px; cursor:pointer; }
  #helix-bar button:hover { border-color:var(--accent); color:var(--accent); }
  #helix-bar input[type=range]{ width:240px; accent-color:var(--accent); }
  #helix-cap { position:fixed; bottom:64px; left:50%; transform:translateX(-50%); max-width:70%; text-align:center;
    font-size:13px; color:#cfeff0; background:rgba(16,22,28,.7); border-radius:8px; padding:5px 12px; z-index:3;
    opacity:0; transition:opacity .3s; }
`;

// The procedural matcap: a neutral grey sphere lit by one soft key from the top-left, drawn on a canvas at load
// so the kit needs no image asset. It is the SAME recipe as the static viewer's matcap so an animated and a
// static hologram read as one family. A matcap bakes the lighting into the lookup, which is exactly why the
// look cannot wash out: there is no exposure, no environment, nothing to over-brighten.
export function makeMatcap(size = 256) {
  const c = document.createElement("canvas"); c.width = c.height = size;
  const g = c.getContext("2d"); const r = size / 2;
  g.fillStyle = "#000"; g.fillRect(0, 0, size, size);
  const base = g.createRadialGradient(r * 0.72, r * 0.68, r * 0.05, r, r, r);
  base.addColorStop(0.0, "#f2f4f6");   // soft key highlight, top-left
  base.addColorStop(0.45, "#b3bbc2");  // mid grey body — light enough that a coloured part keeps its hue
  base.addColorStop(0.85, "#5a626a");  // terminator
  base.addColorStop(1.0, "#343b42");   // rim, never pure black so silhouettes stay legible on slate
  g.fillStyle = base; g.beginPath(); g.arc(r, r, r, 0, Math.PI * 2); g.fill();
  const rim = g.createRadialGradient(r * 1.22, r * 1.3, r * 0.55, r, r, r);
  rim.addColorStop(0.0, "rgba(120,150,165,0)"); rim.addColorStop(1.0, "rgba(120,150,165,0.28)");  // cool bounce
  g.fillStyle = rim; g.beginPath(); g.arc(r, r, r, 0, Math.PI * 2); g.fill();
  const tex = new THREE.CanvasTexture(c);
  if ("colorSpace" in tex) tex.colorSpace = THREE.SRGBColorSpace;  // r152+ names it this way; older builds ignore
  tex.needsUpdate = true;
  return tex;
}

export function createStage(opts = {}) {
  const bg = opts.background || "#10161c";
  const accent = opts.accent || "#3fe0e0";
  // "matcap" (default): every lit mesh added through stage.add() is re-skinned with a MeshMatcapMaterial that
  // keeps its colour — flat, even, CAD-like. "lit": the coder's own materials stay, lit by the hemisphere +
  // key below at sane intensities. Either way there is no bloom, no IBL and no exposure boost.
  const shading = opts.shading === "lit" ? "lit" : "matcap";
  const style = document.createElement("style"); style.textContent = HUD_CSS; document.head.appendChild(style);
  document.documentElement.style.setProperty("--accent", accent);
  const hud = document.createElement("div"); hud.id = "helix-hud";
  hud.innerHTML = '<div class="v"></div><i class="tl"></i><i class="tr"></i><i class="bl"></i><i class="br"></i>';
  document.body.appendChild(hud);
  if (opts.title) { const t = document.createElement("div"); t.id = "helix-title"; t.textContent = opts.title; document.body.appendChild(t); }

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  // No tone mapping and no shadow maps: a technical illustration is flat and exact, and the shadow pass was
  // the single biggest frame cost on a laptop GPU for zero readability gain.
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.shadowMap.enabled = false;
  document.body.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(bg);
  const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 5000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08;

  // Lighting only matters for "lit" shading (matcap ignores lights). One soft key + a hemisphere at sane
  // intensities — the old 2.6 key with IBL on top is what made everything look blown out.
  const key = new THREE.DirectionalLight(0xffffff, 1.0); key.position.set(3, 5, 4);
  scene.add(key); scene.add(key.target);
  scene.add(new THREE.HemisphereLight(0xdfe9ee, 0x1a2026, 0.6));

  const matcap = makeMatcap();
  const EDGE_COLOR = 0xc9d4dc;  // cool light grey crease lines, like a drawing — never the accent, that is chrome only
  const dressed = new WeakSet();

  // Re-skin a lit material as matcap while keeping what the coder chose: colour, map, opacity, side.
  // Emissive materials (glows, screens, reactors) and wireframe materials are deliberate choices a matcap
  // cannot express (MeshMatcapMaterial has no emissive and no wireframe), so they are left alone and simply
  // render under the lights. The polygon offset pushes the faces a hair back in depth so the crease lines
  // drawn at the same depth win the z-test: without it the LineSegments z-fight with the faces and every
  // edge renders as a stippled, broken dash instead of a clean drawn line.
  function toMatcap(m) {
    if (!m || !(m.isMeshStandardMaterial || m.isMeshPhysicalMaterial || m.isMeshPhongMaterial || m.isMeshLambertMaterial)) return m;
    if (m.emissive && m.emissive.getHex() !== 0 && (m.emissiveIntensity === undefined || m.emissiveIntensity > 0)) return m;
    if (m.wireframe) return m;
    const mm = new THREE.MeshMatcapMaterial({ matcap, color: m.color ? m.color.clone() : new THREE.Color(0xffffff),
      map: m.map || null, transparent: !!m.transparent, opacity: m.opacity === undefined ? 1 : m.opacity,
      side: m.side === undefined ? THREE.FrontSide : m.side, flatShading: true,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    mm.name = m.name; mm.userData.helixOriginal = m;
    return mm;
  }
  // The same depth nudge for materials that were NOT re-skinned — the "lit" mode keeps the coder's own
  // materials, and even in matcap mode toMatcap leaves emissive and wireframe ones alone — so crease lines
  // sit cleanly on those faces too instead of stippling. Idempotent, so a shared material is safe.
  function offsetFaces(m) {
    if (!m) return;
    if (m.polygonOffset && m.polygonOffsetFactor === 1 && m.polygonOffsetUnits === 1) return;
    m.polygonOffset = true; m.polygonOffsetFactor = 1; m.polygonOffsetUnits = 1; m.needsUpdate = true;
  }

  // stage.add(object, { edges: true, shading: undefined }) — the stage helper: adds the object to the scene and
  // dresses every Mesh inside it: matcap re-skin (per the stage's shading unless overridden per object) and
  // crease-edge lines (EdgesGeometry at 30°) parented to the mesh itself, so the lines follow the part when the
  // timeline moves it. Meshes are collected BEFORE any child is added, so the new line children are never
  // visited as meshes; a WeakSet keeps a second add() of the same group from dressing a part twice (so a
  // coder who grows the model and re-adds it only dresses the new parts).
  function dress(object, o = {}) {
    const mode = o.shading === "lit" || o.shading === "matcap" ? o.shading : shading;
    const edges = o.edges !== false;
    const meshes = [];
    object.traverse((n) => { if (n.isMesh && !n.userData.helixStage && !dressed.has(n)) meshes.push(n); });
    for (const mesh of meshes) {
      dressed.add(mesh);
      if (mode === "matcap") {
        mesh.material = Array.isArray(mesh.material) ? mesh.material.map(toMatcap) : toMatcap(mesh.material);
      }
      if (edges) {
        // Before the lines go on: every face material under them gets the polygon offset (the lit path
        // sets it here; the matcap re-skin already carries it and offsetFaces is a no-op there).
        if (Array.isArray(mesh.material)) mesh.material.forEach(offsetFaces); else offsetFaces(mesh.material);
      }
      if (edges && mesh.geometry) {
        try {
          const eg = new THREE.EdgesGeometry(mesh.geometry, 30);
          if (eg.attributes.position && eg.attributes.position.count > 0) {
            const lines = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ color: EDGE_COLOR, transparent: true, opacity: 0.85 }));
            lines.userData.helixEdges = true; lines.renderOrder = 1; lines.raycast = () => {};
            mesh.add(lines);
          } else { eg.dispose(); }
        } catch (e) { /* a geometry without an index/position still renders, just without crease lines */ }
      }
    }
    return object;
  }
  function add(object, o = {}) { dress(object, o); scene.add(object); return object; }

  let grid = null, axes = null, ground = null, radius = 1;
  // Auto-frame: places the camera, the key light, a shadow-free ground disc, a unit grid sized to the model and
  // an axes triad. Never hardcode the camera — the coder's model can be any size in any units.
  function frame(object) {
    const box = new THREE.Box3().setFromObject(object); const size = box.getSize(new THREE.Vector3());
    const c = box.getCenter(new THREE.Vector3()); radius = Math.max(size.x, size.y, size.z, 1e-3) * 0.5;
    const dist = radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 1.3;
    camera.near = radius / 100; camera.far = radius * 100; camera.updateProjectionMatrix();
    camera.position.set(c.x + dist * 0.7, c.y + dist * 0.45, c.z + dist); controls.target.copy(c); controls.update();
    key.position.set(c.x + radius * 2.2, c.y + radius * 3.4, c.z + radius * 2.0);
    key.target.position.copy(c); key.target.updateMatrixWorld();
    if (ground) scene.remove(ground);
    ground = new THREE.Mesh(new THREE.CircleGeometry(radius * 9, 64),
      new THREE.MeshBasicMaterial({ color: 0x0d1319 }));
    ground.rotation.x = -Math.PI / 2; ground.position.y = box.min.y - radius * 0.004; ground.userData.helixStage = true;
    scene.add(ground);
    if (grid) scene.remove(grid);
    // Grid spacing is a round power of ten that gives roughly 10-40 cells across the model, so the grid reads as
    // a scale reference rather than a texture; the minor/major colours are muted so the model stays the subject.
    const step = Math.pow(10, Math.floor(Math.log10(Math.max(radius * 2, 1e-6))) - 1);
    const span = Math.ceil((radius * 6) / (step * 10)) * step * 10;
    grid = new THREE.GridHelper(span, Math.round(span / step), 0x3a4c58, 0x22303a); grid.position.set(c.x, box.min.y, c.z);
    grid.material.opacity = 0.5; grid.material.transparent = true; scene.add(grid);
    if (axes) scene.remove(axes);
    // The triad sits just outside the model's front-left floor corner (the corner nearest the default camera),
    // so it is not buried behind the model and reads as a key, not a part.
    axes = new THREE.AxesHelper(radius * 0.5);
    axes.position.set(box.min.x - radius * 0.12, box.min.y, box.max.z + radius * 0.12); scene.add(axes);
  }

  addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  const clock = new THREE.Clock(); let cb = null;
  function start(onFrame) {
    cb = onFrame;
    (function loop() {
      requestAnimationFrame(loop);
      const dt = clock.getDelta();
      if (cb) { try { cb(dt); } catch (e) {} }
      controls.update();
      renderer.render(scene, camera);
    })();
  }
  return { THREE, scene, camera, renderer, controls, frame, start, add, dress, matcap, shading };
}

// Timeline: drives a normalized t in [0,1] and injects a play / restart / scrub bar + a caption. The coder
// passes duration (seconds for a full play), optional captions [{at, text}], and onUpdate(t) to animate.
export class Timeline {
  constructor(opts = {}) {
    this.duration = Math.max(0.5, opts.duration || 10);
    this.onUpdate = opts.onUpdate || (() => {});
    this.captions = (opts.captions || []).slice().sort((a, b) => a.at - b.at);
    this.t = 0; this.playing = true;
    const bar = document.createElement("div"); bar.id = "helix-bar";
    bar.innerHTML = '<button id="helix-play">Pause</button><button id="helix-restart">Restart</button>'
      + '<input id="helix-scrub" type="range" min="0" max="1000" value="0">';
    document.body.appendChild(bar);
    this.cap = document.createElement("div"); this.cap.id = "helix-cap"; document.body.appendChild(this.cap);
    this._play = bar.querySelector("#helix-play");
    this._scrub = bar.querySelector("#helix-scrub");
    this._play.onclick = () => { if (this.t >= 1) this.t = 0; this.playing = !this.playing; this._play.textContent = this.playing ? "Pause" : "Play"; };
    bar.querySelector("#helix-restart").onclick = () => { this.t = 0; this.playing = true; this._play.textContent = "Pause"; };
    this._scrub.oninput = () => { this.t = this._scrub.value / 1000; this.playing = false; this._play.textContent = "Play"; this._apply(); };
    this._apply();
  }
  update(dt) { if (this.playing) { this.t += dt / this.duration; if (this.t >= 1) { this.t = 1; this.playing = false; this._play.textContent = "Play"; } } this._apply(); }
  _apply() {
    try { this.onUpdate(this.t); } catch (e) {}
    if (this._scrub && document.activeElement !== this._scrub) this._scrub.value = Math.round(this.t * 1000);
    let text = "";
    for (const c of this.captions) { if (this.t >= c.at) text = c.text; }
    if (this.cap) { this.cap.textContent = text || ""; this.cap.style.opacity = text ? "1" : "0"; }
  }
}
"""
