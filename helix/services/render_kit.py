"""The HELIX render kit for the ANIMATED 3D path.

The animated path is the coder hand-writing a Three.js index.html — historically a blank-file regression to
flat sphere-blobs. This module ships a ready-made, high-quality stage (the SAME render rig as the baked
viewer: shadows, AO, bloom, IBL, auto-frame) plus a timeline + HUD, as a self-contained ES module HELIX
copies into every animated build. The coder then only builds the model objects and defines the steps — so an
animated model inherits the good render automatically instead of re-deriving it badly.

Embedded as a Python string (no on-disk asset to bundle/resolve), mirroring _VIEWER_HTML in model_baker.
"""
from __future__ import annotations

KIT_FILE = "helix3d.js"

HELIX3D_JS = """\
// HELIX render kit — import { createStage, Timeline, THREE } from "./helix3d.js"
// A ready stage (lighting, soft shadows, AO, bloom, IBL, orbit, auto-frame, HUD) + a timeline, so an
// animated model only builds its objects and defines steps. three is resolved from the page's importmap.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { GTAOPass } from "three/addons/postprocessing/GTAOPass.js";

export { THREE };

const HUD_CSS = `
  :root { --accent: #3fe0e0; }
  html,body { margin:0; height:100%; overflow:hidden; background:#080b0f;
    font-family:-apple-system,"Segoe UI",system-ui,sans-serif; color:#cfeff0; }
  #helix-title { position:fixed; top:14px; left:16px; font-size:14px; font-weight:600; letter-spacing:.04em;
    color:var(--accent); opacity:.85; pointer-events:none; text-shadow:0 1px 8px rgba(0,0,0,.6); z-index:3; }
  #helix-hud { position:fixed; inset:0; pointer-events:none; z-index:1; }
  #helix-hud .v { position:absolute; inset:0; background:radial-gradient(ellipse at center,transparent 56%,rgba(0,0,0,.55) 100%); }
  #helix-hud i { position:absolute; width:26px; height:26px; border:1.5px solid rgba(63,224,224,.5); }
  #helix-hud .tl{top:16px;left:16px;border-right:none;border-bottom:none}
  #helix-hud .tr{top:16px;right:16px;border-left:none;border-bottom:none}
  #helix-hud .bl{bottom:16px;left:16px;border-right:none;border-top:none}
  #helix-hud .br{bottom:16px;right:16px;border-left:none;border-top:none}
  #helix-bar { position:fixed; bottom:14px; left:50%; transform:translateX(-50%); display:flex; gap:8px;
    align-items:center; background:rgba(8,11,15,.7); border:1px solid rgba(63,224,224,.25); border-radius:12px;
    padding:8px 12px; backdrop-filter:blur(6px); z-index:3; }
  #helix-bar button { background:transparent; color:#bfe9ea; border:1px solid rgba(63,224,224,.3);
    border-radius:8px; padding:6px 12px; font-size:13px; cursor:pointer; }
  #helix-bar button:hover { border-color:var(--accent); color:var(--accent); }
  #helix-bar input[type=range]{ width:240px; accent-color:var(--accent); }
  #helix-cap { position:fixed; bottom:64px; left:50%; transform:translateX(-50%); max-width:70%; text-align:center;
    font-size:13px; color:#cfeff0; background:rgba(8,11,15,.6); border-radius:8px; padding:5px 12px; z-index:3;
    opacity:0; transition:opacity .3s; }
`;

export function createStage(opts = {}) {
  const bg = opts.background || "#080b0f";
  const accent = opts.accent || "#3fe0e0";
  const style = document.createElement("style"); style.textContent = HUD_CSS; document.head.appendChild(style);
  document.documentElement.style.setProperty("--accent", accent);
  const hud = document.createElement("div"); hud.id = "helix-hud";
  hud.innerHTML = '<div class="v"></div><i class="tl"></i><i class="tr"></i><i class="bl"></i><i class="br"></i>';
  document.body.appendChild(hud);
  if (opts.title) { const t = document.createElement("div"); t.id = "helix-title"; t.textContent = opts.title; document.body.appendChild(t); }

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  document.body.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(bg);
  const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 5000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08;

  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  const key = new THREE.DirectionalLight(0xffffff, 2.6); key.position.set(3, 5, 4);
  key.castShadow = true; key.shadow.mapSize.set(2048, 2048); key.shadow.bias = -0.0005; key.shadow.normalBias = 0.02;
  scene.add(key); scene.add(key.target);
  const fill = new THREE.DirectionalLight(0x88ccff, 0.8); fill.position.set(-4, 2, -3); scene.add(fill);
  scene.add(new THREE.HemisphereLight(0xbfe9ea, 0x0a0e12, 0.5));

  let composer = null, gtao = null;
  try {
    composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    // AO between render and bloom — static import (not lazy) so it exists before the perf guard runs.
    try {
      gtao = new GTAOPass(scene, camera, window.innerWidth, window.innerHeight);
      gtao.blendIntensity = 0.5; composer.addPass(gtao);
    } catch (e) { gtao = null; }
    composer.addPass(new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.5, 0.4, 0.85));
    composer.addPass(new OutputPass());
  } catch (e) { composer = null; }

  let grid = null, ground = null, radius = 1, framed = false;
  function frame(object) {
    const box = new THREE.Box3().setFromObject(object); const size = box.getSize(new THREE.Vector3());
    const c = box.getCenter(new THREE.Vector3()); radius = Math.max(size.x, size.y, size.z, 1e-3) * 0.5;
    const dist = radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 1.3;
    camera.near = radius / 100; camera.far = radius * 100; camera.updateProjectionMatrix();
    camera.position.set(c.x + dist * 0.7, c.y + dist * 0.45, c.z + dist); controls.target.copy(c); controls.update();
    key.position.set(c.x + radius * 2.2, c.y + radius * 3.4, c.z + radius * 2.0);
    key.target.position.copy(c); key.target.updateMatrixWorld();
    const sc = key.shadow.camera; sc.left = -radius * 1.7; sc.right = radius * 1.7; sc.top = radius * 1.7;
    sc.bottom = -radius * 1.7; sc.near = radius * 0.05; sc.far = radius * 14; sc.updateProjectionMatrix();
    object.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    if (ground) scene.remove(ground);
    ground = new THREE.Mesh(new THREE.CircleGeometry(radius * 9, 64),
      new THREE.MeshStandardMaterial({ color: 0x0c1319, roughness: 1, metalness: 0 }));
    ground.rotation.x = -Math.PI / 2; ground.position.y = box.min.y - radius * 0.003; ground.receiveShadow = true;
    scene.add(ground);
    if (grid) scene.remove(grid);
    grid = new THREE.GridHelper(radius * 6, 24, 0x224a4a, 0x132a2a); grid.position.y = box.min.y;
    grid.material.opacity = 0.16; grid.material.transparent = true; scene.add(grid);
    framed = true;  // only start measuring perf once the real (heavy) model is in the scene
  }

  addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
    if (composer) composer.setSize(window.innerWidth, window.innerHeight);
    if (gtao && gtao.setSize) gtao.setSize(window.innerWidth, window.innerHeight);
  });

  const clock = new THREE.Clock(); let cb = null, pf = 0, pacc = 0, checked = false;
  function start(onFrame) {
    cb = onFrame;
    (function loop() {
      requestAnimationFrame(loop);
      const dt = clock.getDelta();
      if (!checked && framed) { pf++; pacc += dt; if (pf >= 45) { checked = true;
        if (pacc / pf > 0.033) {
          renderer.shadowMap.enabled = false;  // recompile so shadows drop cleanly, not freeze
          scene.traverse((o) => { if (o.isMesh && o.material) o.material.needsUpdate = true; });
          if (gtao) gtao.enabled = false;
        } } }
      if (cb) { try { cb(dt); } catch (e) {} }
      controls.update();
      if (composer) composer.render(dt); else renderer.render(scene, camera);
    })();
  }
  return { THREE, scene, camera, renderer, controls, frame, start };
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
