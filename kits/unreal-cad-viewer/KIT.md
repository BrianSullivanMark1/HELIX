# UNREAL-STYLE CAD VIEWER KIT — portable, drop-in spec

**What this is:** a self-contained recipe for recreating a browser-based, Unreal-Engine-feeling
3D/CAD viewer in ANY project: free-fly navigation, Unreal control dialect (W/E/R gizmo,
ALT-drag duplicate, Ctrl+click multiselect), a shape/locator Workshop, groups & merge,
undo/redo, a command palette, and a **generic "linked records" layer** whose storage backend
(Google Sheet, database, JSON file — anything) is the adopting project's choice.

**How to use it:** drop this file into a project and point an AI agent (or a developer) at it.
It contains behavioral specs for everything plus verbatim code for the hard-won parts —
the fixes that took real debugging (log depth buffer, eased zoom, fly mode pivot trick,
scale-fold system, pose serialization). Copy those verbatim; build the rest from the specs.

**Architecture:** one static `index.html` (all CSS/JS inline, three.js from CDN) + a thin
HTTP server exposing a handful of JSON endpoints. No build step, no framework. The viewer
is à-la-carte: modules are tiered so you can stop at "just a viewer" or go all the way to
sheet-backed record linking.

---

## 🛑 MANDATORY INTAKE — READ THIS FIRST, AGENT

> **If you are an AI agent reading this file to build the viewer in a project: STOP.
> Do not copy features, do not write code yet.** This is a large à-la-carte system.
> Blindly building all of it bloats the project and buries the user's actual use case.
> You are REQUIRED to run the intake below with your multiple-choice question tool
> (not prose), present recommended defaults, record the answers at the bottom of this
> file under **CHOSEN FEATURES**, and build only what was chosen. If the user says
> "just do it all," confirm once, then proceed.

### The intake questions (ask in roughly this order)

1. **Model source & scale** — What 3D format arrives (STEP / OBJ / GLB), how big, and does
   the assembly tree (part names) matter? → decides converter scripts, client caching,
   the PARTS panel, and record auto-matching.
2. **Who uses it** — people doing discovery/placement work (full Workshop), or viewers just
   looking (viewer-only build: modules 1–5)?
3. **Data backbone** — does app data (shape placements, linked records, coordinates) need to
   persist beyond the browser? To what: Google Sheet, database, JSON file on the server,
   or localStorage only? Which fields are the contract? → decides the Storage Adapter
   (module 8) and the server write endpoints.
4. **Placement tooling depth** — pick a tier: (a) view-only, (b) shapes + copy coordinates,
   (c) + groups/merge/custom shapes, (d) + linked records with backend write-back.
   Each tier includes the previous.
5. **Controls dialect** — Unreal keys as default? Rebindable keys needed? Numpad shortcuts?
6. **Command layer** — Ctrl+K palette + command dock: yes/no?
7. **Delight layer** — themed loading bay, guided flashes, LEARN popups: include, or
   strictly business?
8. **Deployment target** — where does the thin server run, and which secrets already exist?

---

## MODULE CATALOG

Each module: what it does · key contracts · verbatim code where it matters.
Tiers build upward; module 1 is always required.

---

### 1. Rendering core (always required)

Single `<canvas>` in a wrapper div. three.js 0.160 via importmap in `<head>`; the bundle
is exposed globally behind a ready event so the (non-module) app script can await it:

```html
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
  import * as THREE from "three";
  import { OrbitControls } from "three/addons/controls/OrbitControls.js";
  import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
  import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
  import { TransformControls } from "three/addons/controls/TransformControls.js";
  import * as BufferGeometryUtils from "three/addons/utils/BufferGeometryUtils.js";
  window.THREE_BUNDLE = { THREE, OrbitControls, OBJLoader, GLTFLoader,
                          TransformControls, BufferGeometryUtils };
  window.dispatchEvent(new Event("three-ready"));
</script>
```

In the app script: `const B = await new Promise(res => { if (window.THREE_BUNDLE) return
res(window.THREE_BUNDLE); window.addEventListener("three-ready", () =>
res(window.THREE_BUNDLE), { once: true }); }); const THREE = B.THREE;`

**Scene setup — copy verbatim.** `logarithmicDepthBuffer` is THE fix for z-fighting/clipping
when framing a tiny part inside a huge assembly. Z-up matches CAD convention.

```js
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true,
                                           logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, 2, 1, 100000);
camera.up.set(0, 0, 1);                              // Z-up scene
const controls = new B.OrbitControls(camera, canvas);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xeaf2ff, 0x20262e, 1.35));
const dl1 = new THREE.DirectionalLight(0xffffff, 1.6); dl1.position.set(1, -1.2, 1.6); scene.add(dl1);
const dl2 = new THREE.DirectionalLight(0xbfd8ff, 0.7); dl2.position.set(-1.4, 1, 0.6); scene.add(dl2);
controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN,
                          RIGHT: THREE.MOUSE.PAN }; // middle AND right both pan
```

Materials: one `MeshStandardMaterial` per part (metalness .15, roughness .5,
`side: THREE.DoubleSide`).

**The `span` rule (critical):** `span` is a module-level number that ALWAYS tracks the
whole-model bounding-sphere size — never a sub-box. Camera near/far, zoom clamps, and fly
speed all derive from it. Framing a small part must not touch it.

```js
let span = 4000;                        // whole-model size; updated on model load only
function frame(view, box) {             // box = optional sub-box (frame one part)
  const b = box || fitBox;              // fitBox = Box3 of the whole loaded model
  if (b.isEmpty()) return;
  const c = b.getCenter(new THREE.Vector3());
  const s = b.getSize(new THREE.Vector3()).length();
  if (!box) span = s || span;           // span always tracks the WHOLE model
  const dist = s * 0.72;
  const at = {
    iso:   [c.x - dist * 0.7, c.y - dist * 0.7, c.z + dist * 0.5],
    front: [c.x - dist, c.y, c.z + s * 0.05],
    side:  [c.x, c.y - dist, c.z + s * 0.05],
    top:   [c.x + 1, c.y, c.z + dist],
  }[view || "iso"];
  camera.position.set(at[0], at[1], at[2]);
  camera.near = Math.max(span / 20000, 1e-4);   // near/far sized to the WHOLE model
  camera.far = span * 50;                       // (log depth handles the range)
  camera.updateProjectionMatrix();
  controls.target.copy(c); controls.update();
}
```

**Eased wheel zoom — copy verbatim.** OrbitControls' native zoom is coarse. Disable it and
dolly with exponential smoothing. The fly-through branch means zooming in never "hits a
wall": the orbit pivot is pushed forward instead.

```js
controls.enableZoom = false;
let zoomAcc = 1, zoomSpeed = 1;         // zoomSpeed = settings slider value / 100
canvas.addEventListener("wheel", ev => {
  ev.preventDefault();
  if (flyNav) { adjustFlySpeed(-Math.sign(ev.deltaY)); return; }  // fly: wheel = throttle
  zoomAcc *= Math.pow(0.9995, ev.deltaY * 2.2 * zoomSpeed);
}, { passive: false });
function applyZoom() {                  // called every frame
  if (Math.abs(zoomAcc - 1) < 0.0008) { zoomAcc = 1; return; }
  const step = Math.pow(zoomAcc, 0.16);
  zoomAcc = Math.pow(zoomAcc, 0.84);
  const dir = camera.position.clone().sub(controls.target);
  let len = dir.length() / step;
  const minLen = span * 0.0008;
  if (len < minLen) {
    // fly-through: push the orbit pivot forward so zooming in NEVER hits a wall
    controls.target.add(dir.clone().normalize().multiplyScalar(-(minLen - len)));
    len = minLen;
  }
  len = Math.min(span * 6, len);
  camera.position.copy(controls.target).add(dir.normalize().multiplyScalar(len));
}
```

**Orbit glide** (settings slider `g`, 0–100, default 35 ≈ subtle drift):
`controls.dampingFactor = 0.03 + Math.pow(1 - g / 100, 2) * 0.97;`

**Render loop — order matters, and skip the first frame** (the loop starts before later
script blocks declare state; `loopWarm` avoids referencing not-yet-declared `let`s):

```js
let loopWarm = false;
(function loop() {
  requestAnimationFrame(loop);
  if (!loopWarm) { loopWarm = true; return; }
  applyZoom();
  updateFlyNav();
  updatePulse();                 // selection pop animation (module 3)
  animateMarkers(performance.now());  // shape animations + multiselect glow (modules 6–7)
  controls.update();
  renderer.render(scene, camera);
  if (renderAxisWidget) renderAxisWidget();   // axis triad, if built
})();
```

Also build: an axis triad widget (corner, click-to-snap views, shows GLOBAL/LOCAL label),
a views bar top-right (ISO / FRONT / SIDE / TOP buttons calling `frame(view)` + a view-mode
dropdown), and a fullscreen toggle (`document.documentElement.requestFullscreen()`; on
`fullscreenchange` dispatch a window resize so the renderer refits).

---

### 2. FLY MODE (Unreal-style free flight) — copy verbatim

WASD moves, Q up / E down, SHIFT is faster, **wheel is the throttle** (speed multiplier
0.1×–8×, shown in a transient toast), drag to look around, ESC or clicking the fly button
lands. The trick: with OrbitControls, pull the orbit pivot right up to the camera's nose so
"orbiting" IS mouse-look; movement shifts camera and pivot together. On landing, the pivot
is pushed back out along the view so classic orbit feels normal with no view jump.

```js
let flyNav = false, flyLast = performance.now();
const flyKeys = { w: 0, a: 0, s: 0, d: 0, q: 0, e: 0, shift: 0 };
let flySpeed = 1, flySpeedTimer = null;
const flySpeedEl = document.createElement("div");   // small center-bottom toast
flySpeedEl.className = "flyspeed";                  // absolute; opacity fades via .show
wrap.appendChild(flySpeedEl);
function adjustFlySpeed(dir) {
  flySpeed = Math.min(8, Math.max(0.1, flySpeed * Math.pow(1.2, dir)));
  flySpeedEl.innerHTML = "SPEED " + (Math.round(flySpeed * 10) / 10) + "&times;";
  flySpeedEl.classList.add("show");
  clearTimeout(flySpeedTimer);
  flySpeedTimer = setTimeout(() => flySpeedEl.classList.remove("show"), 900);
}
function setFlyNav(on) {
  flyNav = !!on;
  Object.keys(flyKeys).forEach(k => { flyKeys[k] = 0; });
  flyLast = performance.now();
  const fwd = camera.getWorldDirection(new THREE.Vector3());
  if (flyNav) {   // pivot to the camera's nose: orbit drag = look around
    controls.target.copy(camera.position)
      .addScaledVector(fwd, Math.max(1e-4, span * 0.02));
    status("FLY MODE · WASD + MOUSE");            // + "?" opening the fly guide overlay
  } else {        // land: push pivot back out so orbiting feels normal, no view jump
    controls.target.copy(camera.position).addScaledVector(fwd, span * 0.5);
    status("LANDED · ORBIT NAVIGATION");
  }
}
function updateFlyNav() {                           // called every frame
  const now = performance.now();
  const dt = Math.min(0.05, (now - flyLast) / 1000);  // tab-switch safe
  flyLast = now;
  if (!flyNav) return;
  const x = flyKeys.d - flyKeys.a,
        y = flyKeys.w - flyKeys.s,
        z = flyKeys.q - flyKeys.e;                  // Q up, E down
  if (!x && !y && !z) return;
  const spd = (span || 200) * (flyKeys.shift ? 1.1 : 0.35) * flySpeed;
  const fwd = camera.getWorldDirection(new THREE.Vector3());
  const right = new THREE.Vector3().crossVectors(fwd, camera.up).normalize();
  const mv = new THREE.Vector3()
    .addScaledVector(fwd, y)
    .addScaledVector(right, x)
    .addScaledVector(new THREE.Vector3(0, 0, 1), z);  // world up (Z-up scene)
  if (!mv.lengthSq()) return;
  mv.normalize().multiplyScalar(spd * dt);
  camera.position.add(mv);
  controls.target.add(mv);                          // pivot rides along
}
window.addEventListener("keyup", ev => {
  const k = ev.key.toLowerCase();
  if (k in flyKeys) flyKeys[k] = 0;
});
window.addEventListener("blur", () =>               // alt-tab mid-flight
  Object.keys(flyKeys).forEach(k => { flyKeys[k] = 0; }));
```

**Key routing while flying** — inside the global keydown handler, BEFORE gizmo hotkeys
(W/E must move, not switch gizmo mode; ESC lands instead of peeling modes):

```js
if (flyNav && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
  const fk = ev.key.toLowerCase();
  if (fk === "escape") { setFlyNav(false); return; }
  if (fk in flyKeys) { flyKeys[fk] = 1; ev.preventDefault(); return; }
}
```

UI: a winged-camera toggle button in the viewport toolbar (`title` explains all controls),
plus a FLY MODE guide overlay styled like the numpad LEARN popup — animated WASD keycaps
with MOVE / UP-DOWN / LOOK / SPEED rows, and a note that W/E don't reach the gizmo while
airborne.

---

### 3. Model pipeline

- **Server:** `GET /api/cad/list` → `{models: [names], meta: {name: {size, mtime}}}`;
  `GET /api/cad/model/<file>` serves GLB/OBJ from a models directory.
- **Client cache (Cache API):** cache key `/api/cad/model/<name>?v=<size>-<mtime>` — a
  changed file invalidates automatically; quota failures fall back to network silently.
  Use one named cache bucket (e.g. `app-cad-v1`).
- **Converters** (run on a workstation, not the server): STEP → GLB keeping the assembly
  tree/part names (e.g. Python `cascadio` + `trimesh`; a file-based OCCT/OCP fallback for
  files that exceed cascadio's ~2 GiB buffer — `STEPCAFControl_Reader` → `BRepMesh` →
  `RWGltf_CafWriter` streams to disk). Refuse GLBs > 150 MB by convention; provide a
  merge script for combining GLBs; if a multi-GB STEP exceeds workstation RAM, convert
  subsystem exports separately and merge (or ship as separate models — often better).
  Hard-won converter lessons:
  - **OCP API drift:** OCCT 8.x builds dropped legacy typedefs (`TDF_LabelSequence`,
    `TColStd_IndexedDataMapOfStringString`). Feature-detect: fall back to
    `TDF_ChildIterator` for label walks, auto-hunt the file-info map class for
    `Perform`, and print the binding's `__doc__` signature on failure. Never gate on a
    bare `import OCP` — it false-passes on broken installs; deep-import what you use.
  - **Raw OCCT GLBs are draw-call bombs** (one primitive per B-rep face): a 1.4 GB
    export carried 1.37M primitives; a numpy optimizer (bake transforms → merge
    per-part → weld+decimate on a mm grid → int8 normals) took it to 29 MB / 5.4k
    draws with `--cell 4 --keep-parts` (+`--zup` when the route exports Y-up).
  - **One-double-click prep script** that self-installs its deps into its OWN
    interpreter (`python -m pip`, never bare `pip` — users' terminals resolve a
    different Python), preflights via the converter's own `--check` mode (so the test
    can't drift from the real imports), and skips already-finished steps.
- **Scene weight budgets** — two independent tab-killers, checked before load:

```js
const TRI_BUDGET = 9e6, DRAW_BUDGET = 25000;  // draw calls kill raw B-REP exports
// (OCCT exports can emit one primitive PER FACE — hundreds of thousands of draws;
// run a mesh-merge optimizer on any model that trips these)
```

- **Loading bay:** full-screen themed loading overlay with real progress (`bayShow(name)` /
  `bayProg(pct)` / `bayHide()`), a "first load caches for next time" note, and rotating
  quips. Theme it to the project — it's a self-contained SVG scene, swap freely.
- **Default model:** ★ button per model + localStorage key `app_default_model`; auto-load
  on entry.

---

### 4. Parts system

**Part grouping — copy verbatim.** Meshes group by nearest NAMED ancestor in the scene
graph; STEP→GLB keeps assembly names, so these are real part names:

```js
function collectParts(root) {
  const byName = new Map();
  let anon = 0;
  root.traverse(ch => {
    if (!ch.isMesh) return;
    let label = "", p = ch;
    while (p && p !== root) {
      if (p.name && p.name.trim()) { label = p.name.trim(); break; }
      p = p.parent;
    }
    if (!label) label = "UNNAMED BODY " + (++anon);
    const idx = ch.geometry.getIndex();
    const tris = Math.floor((idx ? idx.count : ch.geometry.getAttribute("position").count) / 3);
    if (!byName.has(label)) byName.set(label, { name: label, meshes: [], tris: 0, visible: true });
    const part = byName.get(label);
    part.meshes.push(ch);
    part.tris += tris;
  });
  return Array.from(byName.values());
}
```

**Color contract:** settings expose body / hover / selected / shape-selected / linked-record
colors (each a persisted color input). `baseColorOf(part)` is the SINGLE restore point that
answers "what color should this part be right now?" (selected > record-lit > body). Every
feature that tints parts extends `baseColorOf` — never bypasses it.

**Selection pulse:** on select, spawn a ghost shell of the part's geometry that scales up
~5% and fades, damped (a "Mario pop"). `startPulse(part)` + `updatePulse()` in the loop.

**PARTS panel** (dock tab, closed by default): filter box (exactly 1 match auto-highlights),
per-part 👁 hide toggle, click-to-frame (`frame(null, partBox)`), CSV download + TSV copy.

**View modes:** `setViewMode("solid"|"wireframe"|"xray")` — wireframe flips
`material.wireframe`; x-ray drops opacity on non-selected parts. Dropdown top-right; numpad
5/0 toggle them. **Isolate** = numpad * (`applyIsolation()` hides all but selection,
respecting per-part 👁 states).

---

### 5. Picking, selection & modes

- One raycaster. Pick order: shapes/markers first (respecting `pickable !== false`), then
  parts. Hover tooltips throttled to ~90 ms (raycasts on big meshes are pricey).
- **Selection exclusivity:** selecting a part drops the shape gizmo, and vice versa.
- **pointerup rules — copy the guards:** only `button === 0` selects; a >6 px move is a
  drag, not a click; ignore while the gizmo is dragging or an axis is hot:

```js
canvas.addEventListener("pointerup", ev => {
  pointerDown = false;
  if (ev.button !== 0) return;        // right/middle never change selection
  if (!downAt) return;
  const moved = Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]);
  downAt = null;
  if (moved > 6) return;              // was a drag, not a click
  if (tc.dragging || tc.axis) return; // interacting with the gizmo
  /* ...pickMarker → pickPart... */
});
```

- **ESC ladder** — ESC peels ONE mode per press, in priority order. Adapt the rungs to the
  modules you build (this is the full-build ladder):

```js
function escStep() {
  if (document.fullscreenElement) return;  // browser exits fullscreen — don't also peel
  if (pickShape) { /* cancel shape-pick */ return; }
  if (pickFor)   { /* cancel part-pick  */ return; }
  if (linkMode)  { /* cancel link mode  */ return; }
  if (msSel.size) { clearMs(); return; }               // multiselect
  if (selMarker) { selMarker = null; tc.detach(); /* rerender */ return; }
  if (selected)  { clearSelection(); return; }         // part selection
  if (isolated)  { isolated = false; applyIsolation(); return; }
  if (xray)      { xray = false; applyXray(); return; }
  if (activeRecord) { closeRecord(); return; }
  /* close any open panel */
}
```

  The cursor/select tool (`exitModes()`) clears EVERYTHING at once.
- **Mode pill** (top-center): names the active mode with an ✕ (✕ = one `escStep()`).
  It answers "what mode am I in?" — update it whenever you add a mode. **Rule: every new
  mode gets a pill entry and an ESC-ladder rung.**
- Right-click = context menu (sectioned NAVIGATE / BUILD / VIEW / EXPORT, hotkey chips,
  primary action pulses, condition-aware). Right-drag = pan, never selection.

---

### 6. Gizmo & keys (the Unreal dialect)

TransformControls (`tc`). Defaults, rebindable in settings (click key chip → "PRESS KEY…" →
next keydown captures; persisted):

```js
const KEY_DEFAULTS = { focus: "f", translate: "w", rotate: "e", scale: "r",
                       group: "g", deselect: "escape" };
let KEYS = Object.assign({}, KEY_DEFAULTS);
try { Object.assign(KEYS, JSON.parse(localStorage.getItem("app_keys") || "{}")); } catch (e) {}
```

**Global keydown handler — order of checks (copy the skeleton):**
1. key-capture mode (rebinding) → 2. Ctrl+S (save open record) → 3. Ctrl+K (palette) →
4. bail if target is INPUT/TEXTAREA/SELECT → 5. bail/ESC-close if an overlay is open →
6. **fly-mode routing** (module 2 snippet) → 7. numpad map → 8. Ctrl+Z / Ctrl+Y (+Ctrl+
Shift+Z) undo/redo → 9. DEL = confirm-delete selected shape → 10. G = group (if 2+
multiselected) → 11. F focus / ESC `escStep()` / W‑E‑R set gizmo mode (only with a shape
selected).

**Numpad map** (with a click-to-try animated LEARN overlay; use `ev.code` `Numpad*` so the
top row stays free):

```js
const NP_ACTIONS = {
  "7": ["TOP VIEW", () => frame("top")],
  "8": ["ORBIT UP", () => orbitNudge(0, 0.26)],
  "9": ["ISO VIEW", () => frame("iso")],
  "4": ["ORBIT LEFT", () => orbitNudge(0.26, 0)],
  "5": ["WIREFRAME", () => setViewMode(viewMode === "wireframe" ? "solid" : "wireframe")],
  "6": ["ORBIT RIGHT", () => orbitNudge(-0.26, 0)],
  "1": ["FRONT VIEW", () => frame("front")],
  "2": ["ORBIT DOWN", () => orbitNudge(0, -0.26)],
  "3": ["SIDE VIEW", () => frame("side")],
  "0": ["X-RAY MODE", () => setViewMode(viewMode === "xray" ? "solid" : "xray")],
  ".": ["FOCUS SELECTED", focusSelection],
  "*": ["ISOLATE SELECTED", () => { isolated = !isolated; applyIsolation(); }],
  "+": ["ZOOM IN", () => { zoomAcc *= 1.35; }],
  "-": ["ZOOM OUT", () => { zoomAcc /= 1.35; }],
};
```

**ALT+drag duplicates** (UE semantics — the COPY stays behind, you drag the original away).
In `pointerdown`, when ALT is held over an active gizmo axis:

```js
if (ev.altKey && selMarker && !selMarker.isGroup && tc.axis) {
  const s = selMarker;
  pushUndo();
  addMarker(s.type, { params: Object.assign({}, s.params), color: s.color,
    name: s.name + "-COPY",
    pos: [s.mesh.position.x, s.mesh.position.y, s.mesh.position.z],
    rot: [s.mesh.rotation.x, s.mesh.rotation.y, s.mesh.rotation.z] }, true);
  status("ALT-DUPLICATED · " + s.name);
}
```

- **GLOBAL/LOCAL gizmo space:** `tc.setSpace("world"|"local")`, persisted
  (`app_gizmo_space`); toggle via globe button + label above the axis triad.
- **Snap increments** (move/rotate/scale) live in the views bar: click toggles, right-click
  cycles presets; wire to `tc.setTranslationSnap / setRotationSnap / setScaleSnap`;
  persist (`app_snap`).
- Viewport icon toolbar top-left: cursor (exitModes) / move / rotate / scale / space / fly.

**Undo/redo — snapshot pattern, copy verbatim.** Whole-scene JSON snapshots (cap 50).
**Capture BEFORE mutations:** add / remove / drag-start / first edit after field focus /
group / merge / reparent.

```js
const undoStack = [], redoStack = [];
function pushUndo() {
  undoStack.push(JSON.stringify(serializeMarkers()));
  if (undoStack.length > 50) undoStack.shift();
  redoStack.length = 0;
}
function undo() {
  if (!undoStack.length) { status("NOTHING TO UNDO"); return; }
  redoStack.push(JSON.stringify(serializeMarkers()));
  restoreMarkers(undoStack.pop());
}
function redo() {
  if (!redoStack.length) { status("NOTHING TO REDO"); return; }
  undoStack.push(JSON.stringify(serializeMarkers()));
  restoreMarkers(redoStack.pop());
}
```

`restoreMarkers` disposes non-record-tagged shapes/groups, keeps record-tagged ones, and
rebuilds from the JSON. Fresh history per model load.

---

### 7. Shapes & the Workshop

**SHAPES registry — the contract.** Per entry: `label`, `params` (list of
`[key, label, default-multiplier-of-unit]`), `geo(P) → BufferGeometry`, and
`pts(P) → {points, dirs, scalars}` in LOCAL space (named key points for coordinate export).
Default sizes scale off a unit derived from model span × a settings percentage.

```js
const SHAPES = {
  sphere:   { label: "Sphere", params: [["r", "RADIUS", 1]],
    geo: P => new THREE.SphereGeometry(P.r, 28, 20),
    pts: P => ({ points: { CENTER: [0, 0, 0] }, scalars: { RADIUS: P.r } }) },
  cube:     { label: "Cube", params: [["s", "SIZE", 2]],
    geo: P => new THREE.BoxGeometry(P.s, P.s, P.s),
    pts: P => ({ points: boxCorners(P.s, P.s, P.s), scalars: {} }) },
  box:      { label: "Box (rect)", params: [["sx","SIZE X",3],["sy","SIZE Y",2],["sz","SIZE Z",1]],
    geo: P => new THREE.BoxGeometry(P.sx, P.sy, P.sz),
    pts: P => ({ points: boxCorners(P.sx, P.sy, P.sz), scalars: {} }) },
  cylinder: { label: "Cylinder", params: [["r","RADIUS",1],["h","HEIGHT",3]],
    geo: P => new THREE.CylinderGeometry(P.r, P.r, P.h, 28),
    pts: P => ({ points: { TOP_CENTER: [0, P.h/2, 0], BOTTOM_CENTER: [0, -P.h/2, 0] },
                 scalars: { RADIUS: P.r, HEIGHT: P.h } }) },
  cone:     { label: "Cone", params: [["r","RADIUS",1],["h","HEIGHT",3]],
    geo: P => new THREE.ConeGeometry(P.r, P.h, 28),
    pts: P => ({ points: { APEX: [0, P.h/2, 0], BASE_CENTER: [0, -P.h/2, 0] },
                 scalars: { BASE_RADIUS: P.r, HEIGHT: P.h } }) },
  capsule:  { label: "Capsule", params: [["r","RADIUS",1],["h","CYL LENGTH",3]],
    geo: P => new THREE.CapsuleGeometry(P.r, P.h, 6, 14),
    pts: P => ({ points: { END_A: [0, P.h/2, 0], END_B: [0, -P.h/2, 0] },
                 scalars: { RADIUS: P.r, CYL_LENGTH: P.h } }) },
  torus:    { label: "Torus (ring)", params: [["R","RING RADIUS",2],["r","TUBE RADIUS",0.4]],
    geo: P => new THREE.TorusGeometry(P.R, P.r, 14, 36),
    pts: P => ({ points: { CENTER: [0,0,0] }, dirs: { NORMAL: [0,0,1] },
                 scalars: { RING_RADIUS: P.R, TUBE_RADIUS: P.r } }) },
  plane:    { label: "Rectangle (plane)", params: [["w","WIDTH",3],["h","HEIGHT",2]],
    geo: P => new THREE.PlaneGeometry(P.w, P.h),
    pts: P => ({ points: { C1: [-P.w/2,-P.h/2,0], C2: [P.w/2,-P.h/2,0],
                           C3: [P.w/2,P.h/2,0], C4: [-P.w/2,P.h/2,0] },
                 dirs: { NORMAL: [0,0,1] }, scalars: {} }) },
  disc:     { label: "Disc", params: [["r","RADIUS",1.5]],
    geo: P => new THREE.CircleGeometry(P.r, 32),
    pts: P => ({ points: { CENTER: [0,0,0] }, dirs: { NORMAL: [0,0,1] },
                 scalars: { RADIUS: P.r } }) },
  arrow:    { label: "Arrow", params: [["h","LENGTH",4],["r","THICKNESS",0.3]],
    geo: P => arrowGeometry(P.h, P.r),
    pts: P => ({ points: { TAIL: [0,-P.h/2,0], TIP: [0,P.h/2,0] },
                 dirs: { AXIS: [0,1,0] }, scalars: { LENGTH: P.h, THICKNESS: P.r } }) },
  pyramid:  { label: "Pyramid", params: [["s","BASE SIZE",2],["h","HEIGHT",2.5]],
    geo: P => new THREE.ConeGeometry(P.s/Math.SQRT2, P.h, 4, 1, false, Math.PI/4),
    pts: P => ({ points: { APEX: [0,P.h/2,0],
                           B1: [-P.s/2,-P.h/2,-P.s/2], B2: [P.s/2,-P.h/2,-P.s/2],
                           B3: [P.s/2,-P.h/2,P.s/2],  B4: [-P.s/2,-P.h/2,P.s/2] },
                 scalars: {} }) },
};
function arrowGeometry(h, r) {          // merged-geometry compound shape example
  const headLen = Math.min(h * 0.35, r * 7);
  const shaft = new THREE.CylinderGeometry(r, r, h - headLen, 20).translate(0, -headLen/2, 0);
  const head = new THREE.ConeGeometry(r * 2.6, headLen, 20).translate(0, h/2 - headLen/2, 0);
  return B.BufferGeometryUtils.mergeGeometries([shaft, head]);
}
function boxCorners(sx, sy, sz) {
  const o = {}; let i = 1;
  for (const zs of [-1, 1]) for (const ys of [-1, 1]) for (const xs of [-1, 1])
    o["C" + (i++)] = [xs*sx/2, ys*sy/2, zs*sz/2];
  return o;
}
```

**Param-fold system — copy verbatim; this is the invariant that keeps readouts truthful.**
Gizmo scaling folds into params on release so `mesh.scale` is ALWAYS (1,1,1). Every shape
gets a fold branch; the uniform-average fallback means a future shape can never snap back:

```js
function foldScale(m) {                 // m = marker {type, params, mesh}
  const s = m.mesh.scale;
  if (Math.abs(s.x-1) + Math.abs(s.y-1) + Math.abs(s.z-1) < 1e-6) return;
  const P = m.params, t = m.type, avg = (a, b) => (a + b) / 2;
  if (t === "box") { P.sx *= s.x; P.sy *= s.y; P.sz *= s.z; }
  else if (t === "cube") { P.s *= (s.x + s.y + s.z) / 3; }
  else if (t === "sphere") { P.r *= (s.x + s.y + s.z) / 3; }
  else if (t === "cylinder" || t === "cone" || t === "capsule") { P.r *= avg(s.x, s.z); P.h *= s.y; }
  else if (t === "torus") { P.R *= avg(s.x, s.y); P.r *= avg(s.x, s.y); }
  else if (t === "plane") { P.w *= s.x; P.h *= s.y; }
  else if (t === "disc") { P.r *= avg(s.x, s.y); }
  else if (t === "pyramid") { P.s *= avg(s.x, s.z); P.h *= s.y; }
  else if (t === "arrow") { P.r *= avg(s.x, s.z); P.h *= s.y; }
  else if (t.startsWith("custom_")) { P.s = (P.s || 1) * (s.x + s.y + s.z) / 3; }
  else {                                // any future shape: uniform fold, never snap back
    const u = (s.x + s.y + s.z) / 3;
    Object.keys(P).forEach(k => { P[k] *= u; });
  }
  m.mesh.scale.set(1, 1, 1);
  m.mesh.geometry.dispose();
  m.mesh.geometry = SHAPES[t].geo(P);
}
```

**Pose serialization — copy verbatim.** World coords for readouts come from `matrixWorld`
(correct inside groups). Persistence stores LOCAL pose vs the REAL parent — correct even
mid-multi-transform when the mesh is temporarily under the selection pivot:

```js
function poseOf(e) {           // local pose vs REAL parent, even mid multi-transform
  const parent = (e.parentG && findGroup(e.parentG)) ? findGroup(e.parentG).mesh : markersGroup;
  if (e.mesh.parent === parent || !e.mesh.parent) {
    return { pos: [e.mesh.position.x, e.mesh.position.y, e.mesh.position.z],
             rot: [e.mesh.rotation.x, e.mesh.rotation.y, e.mesh.rotation.z],
             scl: [e.mesh.scale.x, e.mesh.scale.y, e.mesh.scale.z] };
  }
  parent.updateMatrixWorld(true);
  e.mesh.updateMatrixWorld(true);
  const rel = new THREE.Matrix4().copy(parent.matrixWorld).invert()
    .multiply(e.mesh.matrixWorld);
  const p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
  rel.decompose(p, q, s);
  const eu = new THREE.Euler().setFromQuaternion(q, "XYZ");
  return { pos: [p.x, p.y, p.z], rot: [eu.x, eu.y, eu.z], scl: [s.x, s.y, s.z] };
}
```

`serializeMarkers()` returns `{g: [...groups], m: [...markers]}` with per-entity
`{name, type, color, params, parentG, owner, pickable, hidden, alpha, anim, pos, rot, scl}`
(poses via `poseOf`). Persist per model: `app_markers::<model>`.

**More shape behaviors:**
- **Animations** per shape: pulse / spin X/Y/Z / bob + speed. Capture a base pose
  (`animBase`) so persistence/export ignore animation offsets; pause while gizmo-dragged
  or multi-selected.
- `pickable: false` = click-through in the viewport but still selectable in the Outliner.
- **Copy/export:** TSV row per shape (header matching your data contract), JSON, copy-all.
- **Workshop panel** (left side): collapsible icon sections (Shapes / Custom Shapes /
  Linked Records), search that auto-expands, collapse-to-vertical-strip, CREATE button
  with guided flash (model select first if none loaded), a LEARN popup.
- **Scene Outliner + Details panels** (right dock rail): tree view with 📁 groups,
  drag-row-onto-group to parent / onto header to unparent, both collapsible, resizable
  (drag left edge, shared persisted width). Details: transform fields, params, color,
  alpha, animation, pickable, ASSIGN (module 8).

---

### 8. Groups, multiselect & merge

- **Ctrl+click multiselect** (modifier configurable, persisted `app_ms_mod`): resolve to
  the TOP entity (outermost group). Glow = per-frame emissive maintenance inside
  `animateMarkers` using the shape-selected color — never stateful toggles (they leak).
- **Multi-transform:** 2+ selected → create a temp pivot at the combined bbox center,
  attach the selection, gizmo drives the pivot. On release, re-parent with world pose
  preserved and fold scale per shape:

```js
function releaseMsNode() {
  if (!msNode) return;
  msSel.forEach(e => {
    if (e.mesh.parent !== msNode) return;
    const dest = (e.parentG && findGroup(e.parentG)) ? findGroup(e.parentG).mesh : markersGroup;
    dest.attach(e.mesh);                    // world pose preserved
    if (!e.isGroup) {
      const s = e.mesh.scale;
      if (Math.abs(s.x-1) + Math.abs(s.y-1) + Math.abs(s.z-1) > 1e-6) foldScale(e);
    }
  });
  if (msNode.parent) msNode.parent.remove(msNode);
  msNode = null;
}
```

  **Never serialize while the pivot is formed without `poseOf`.** Re-form for each drag.
- **Groups:** `groupSelected()` — pivot at children's center; nested groups OK; groups get
  their own Details (transform + uniform scale); UNGROUP keeps world poses.
- **Merge → custom shape:** recipe-based. A recipe = list of `{type, params, color, pos,
  rot, scl}` relative to a base matrix (group matrix, or bbox center for a loose
  multiselect). Store in `app_custom_shapes` (localStorage), register into `SHAPES` as
  `custom_<id>` whose `geo(P)` rebuilds via `BufferGeometryUtils.mergeGeometries` with a
  single SCALE param. Custom shapes are reusable from the Workshop and deletable.

---

### 9. Linked records — the generic data layer (pluggable backend)

*(This generalizes a "part interfaces" system that bound sheet rows to CAD parts and
placed shapes. Rewritten here so ANY record type and ANY backend works.)*

**Concept.** A **record** is a named row of business data that can reference model parts
and carry placed shapes/coordinates. Records live in a backend of the adopting project's
choice; the viewer reads them all at boot and writes back explicitly on SAVE.

**Record schema (the contract — adapt field names to the project):**

| Field | Meaning |
|---|---|
| `MODEL` / `MODEL_FILENAME` | which model the record belongs to |
| `RECORD_ID` / `NAME` | unique record identity |
| `LINKED_PARTS` | ordered part names, `\|\|\|\|`-separated |
| `SHAPE` | human-readable shape summary (each shape's text joined with `"; "`) |
| `COORDINATES` | JSON blob: `shapes: [...]` array (per shape: `center/rotation_deg/params/points/dirs/color`, or `group:true + scale + children`) plus record-LEVEL keys at the top: `part_map` (manual match overrides), `hide_on_open`, `camera` |
| `CREATED_BY` | author attribution |
| *(any extra columns)* | passed through untouched — the viewer edits only its own fields |

**Storage Adapter — the pluggable part.** The client speaks ONLY these endpoints; implement
them against any backend (Google Sheet via a service account, SQLite/Postgres, a JSON file
on disk, an internal API):

```
GET  /api/records            → {records: [...]}        (all records, all models)
POST /api/records/save       → {ok} (update one record's viewer-owned fields)
POST /api/records/create     → {ok, record}
POST /api/records/delete     → {ok} (server may protect built-ins from deletion)
POST /api/records/reset      → {ok} (restore a record's shape/coords to empty)
```

Server-side rules regardless of backend: the adapter owns column/field mapping in ONE
module; unknown fields round-trip untouched; deletes of protected/built-in records are
refused server-side, not just hidden in UI. If there's no backend at all, the same five
routes can read/write a local JSON file — the client never knows the difference.

**Viewer behaviors:**
- **Part auto-match:** normalized substring match from `LINKED_PARTS` names to loaded part
  names; manual override per part via a ⌖ pick mode, stored as `part_map` inside
  `COORDINATES`. Unmatched entries show a blinking red "● NOT IN MODEL".
- **Multiple shapes per record** (soft cap ~12): alternative click-handles onto the SAME
  record, never independent part scopes — clicking ANY of them opens the record and
  lights the whole linked-part set. The card lists one row per shape (own gizmo attach,
  own frame-to, own ✕); ADD SHAPE appends; the shape picker never replaces implicitly.
  Per-row delete is a client-side edit written by the ordinary save — no delete-one
  endpoint; footer CLEAR ALL SHAPES calls `reset`. Names are positional and RE-DERIVED
  after add/delete (`RECORD · NAME #1`, `#2` — never stored). Hovering one shape glows
  its siblings (per-frame emissive, same rule as multiselect). `hide_on_open` stays ONE
  flag per record, hiding all its shapes.
- **Legacy-compatible persistence:** a pre-list blob (single shape or group at the top
  level) is READ as a list of one and only rewritten when someone actually saves that
  record; a one-shape row round-trips byte-identical. No new backend fields, ever.
  Per-shape `color` must ride in the blob — dropping it makes spawns repaint with the
  viewer's local preference, so color edits silently revert on reload for everyone else.
- **Shape assignment paths (all equivalent):** PLACE NEW (drops at matched-parts center,
  each extra one stepped aside so it isn't buried in the last), USE SCENE SHAPE pick
  mode, right-click → Assign (picker modal), Details ASSIGN button. Groups assign as
  composite JSON (group + children recipe) and count as one list entry.
- **Ambient shapes:** every saved record's shapes spawn on model load, slot-matched on
  their index so respawn is idempotent (one missing shape doesn't duplicate siblings),
  and act as click handles lighting linked parts via `baseColorOf`. Unsaved shapes are
  removed when the record closes; Ctrl+Z restores. Record shapes live at ROOT (they may
  not join user groups) and are EXCLUDED from the undo stack — they're backend-backed,
  not undo-backed.
- **Record tree + camera:** a dock tab lists every record for the loaded model —
  click = open & fly there; a record can persist a saved `camera` pose (top-level key)
  so opening it flies to a curated view.
- **Explicit SAVE** (Ctrl+S) with a dirty-dot indicator; nothing writes to the backend
  implicitly. Guard concurrent edits: the save carries the row's expected identity and
  the server returns 409 on mismatch (someone re-ordered/edited underneath you).
- **Optional write guard:** if some records are more sensitive than others, tier them —
  e.g. free-edit records save frictionlessly, protected ones require an author name prompt
  and append to an audit log (and the save ABORTS if the audit write fails).

---

### 10. Command layer

- **Ctrl+K palette:** one fuzzy list over records (with status lights + rich tooltips),
  parts, shapes, and actions. Substring scoring, full keyboard nav. Optional dashed
  "ask the AI" row if the project wires an assistant.
- **Command dock** (bottom-center pill, animated gradient ring): opens the palette, shows a
  CTRL K chip, hideable (persisted). Carries **status lights** — one dot per record: red
  flashing = needs work (tooltip says exactly what's missing), green = ready, plus an
  "n/N DONE · m PARTIAL" counter; click a dot to open that record. Centralize the logic
  in one `recordNeeds(record) → string|null` function, and measure progress on parts
  actually LINKED to the loaded model, not on fields being non-empty — a record whose
  parts all fail to resolve is red ("wrong model loaded or wrong names"), not amber.
- Size the dock by MEASURING its children against available width (never a hand-tuned
  pixel breakpoint) and re-measure after any content change.

---

### 11. Delight & guidance (optional, genericize freely)

- **Global themed tooltips:** intercept every `title` attribute → styled card, multi-line
  via `\n`. New UI needs only a `title` to get themed tips.
- **Confirm modals** for every destructive action (delete / clear-scene with per-object
  checkboxes).
- **LEARN popups:** animated numpad map, Workshop guide, fly-mode guide (module 2),
  Outliner "?" — all the same overlay pattern.
- **Guided flashes:** first-run pulse sequences steering users to the CREATE button etc.
- **Themed loading bay** (module 3) and optional easter eggs — theme to the project.
- **App-wide preferences** (hamburger ☰): theme accents, DAY/NIGHT, font scale (body
  zoom). Rule: any chart/table reads the font-scale at draw time and exposes a per-chart
  ⚙ — build nothing hardcoded.

---

### 12. Persistence registry (localStorage — pick a project prefix)

`<app>_keys, <app>_ms_mod, <app>_glide, <app>_zoomspeed, <app>_shape_size,
<app>_gizmo_space, <app>_snap, <app>_bg_color, <app>_body_color, <app>_hover_color,
<app>_selected_color, <app>_shape_sel_color, <app>_record_color, <app>_markers::<model>,
<app>_custom_shapes, <app>_default_model, <app>_panel_w, <app>_ws_min, <app>_cmdbar,
<app>_ol_open, <app>_dt_open` + one Cache API bucket `<app>-cad-v1`.

### 13. Server endpoint registry (thin server, any language)

`/health · /api/cad/list · /api/cad/model/<f> · /api/records (+save/create/delete/reset
POST) · /static/*`. Secrets stay server-side (backend credentials, e.g. a Google service
account JSON with the sheet shared to it as EDITOR — never in the client).

---

## PORTING RULES OF THUMB (hard-won — do not skip)

- Copy the rendering core + fly mode + zoom verbatim; they encode real fixes
  (log depth, the span rule, eased zoom, pivot trick, first-frame skip, fold system).
- **Never let a mesh keep non-unit scale** — fold into params on every release.
- Every destructive action: confirm modal + undo capture BEFORE the mutation.
- Every mode: a mode-pill entry + an ESC-ladder rung.
- Every new button: a `title` (auto-themed tooltip); primary actions pulse.
- Selection glow via per-frame emissive maintenance, not stateful toggles.
- Boot order: declare all state `let`s before any `await` in the boot function — the
  render loop and event handlers start early and must not hit TDZ.
- Measure UI to fit; never hand-tune pixel breakpoints.
- Keep the whole client in ONE html file — trivial deploys, no build step — and keep a
  syntax-gate script (extract the inline `<script>` blocks, run them through a JS
  parser) so a bad edit to the big file is caught before deploy, not in the browser.
- Positional labels (shape #1/#2, numbering) are RE-DERIVED after every add/delete,
  never stored — stored indices go stale.
- When a record/entity gains a list where a scalar lived, keep reads legacy-compatible
  (old blob = list of one) and only rewrite on an actual user save.

---

## ✍ CHOSEN FEATURES (the agent records intake answers here)

| Question | Answer | Date |
|---|---|---|
| Model source & scale | *(pending intake)* | |
| Audience | | |
| Data backbone / storage adapter | | |
| Placement tier (a–d) | | |
| Controls dialect | | |
| Command layer | | |
| Delight layer | | |
| Deployment | | |
