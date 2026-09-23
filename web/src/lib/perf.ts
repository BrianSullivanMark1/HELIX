// THE GOVERNOR (Brian, 2026-09-22: "the app should run fast but it's slow as ass").
//
// One place that knows how fast the page is drawing and what level of art the machine can
// afford. It measures the real frame time (the browser's own rAF cadence), and in Auto mode it
// steps the level down when frames run long and back up when they stay short:
//
//   full     everything: 1.5x pixels on the 3D faces, the full lattice, glows, the backdrop
//   lean     1.25x pixels, a thinner lattice, no glow halos on the small canvases, 30 fps 2D
//   minimal  1x pixels, half the lattice, no backdrop scene, 2D faces at 20 fps
//
// The level is read per frame by the art (cheap), the mode is a setting (Settings > Voice & look >
// Performance) kept on this PC. `helix-perf` fires when the level changes.
export type Level = "full" | "lean" | "minimal";
export type Mode = "auto" | Level;

const KEY = "helix_perf";
const ORDER: Level[] = ["full", "lean", "minimal"];

interface Perf { mode: Mode; level: Level; fps: number; frameMs: number; long: number; short: number; started: boolean }
export const perf: Perf & { software: boolean; renderer: string } = { mode: "auto", level: "full", fps: 60, frameMs: 16, long: 0, short: 0, started: false, software: false, renderer: "" };
try { const m = localStorage.getItem(KEY); if (m === "auto" || m === "full" || m === "lean" || m === "minimal") perf.mode = m; } catch { /* fine */ }
if (perf.mode !== "auto") perf.level = perf.mode;

function setLevel(l: Level) {
  if (perf.level === l) return;
  perf.level = l;
  window.dispatchEvent(new CustomEvent("helix-perf", { detail: { level: l, mode: perf.mode } }));
}

export function setMode(m: Mode) {
  perf.mode = m;
  try { localStorage.setItem(KEY, m); } catch { /* fine */ }
  perf.long = 0; perf.short = 0;
  if (m !== "auto") setLevel(m);
  window.dispatchEvent(new CustomEvent("helix-perf", { detail: { level: perf.level, mode: perf.mode } }));
}

/** Is the browser drawing WebGL in SOFTWARE (no usable graphics card: a laptop on battery, a VM,
 *  a remote desktop)? Read once from the renderer string. Software rendering turns every 3D
 *  frame into CPU work, so Auto starts at Minimal there instead of finding out the slow way. */
export function probeGpu(): boolean {
  try {
    const c = document.createElement("canvas");
    const gl = (c.getContext("webgl2") || c.getContext("webgl")) as WebGLRenderingContext | null;
    if (!gl) { perf.software = true; perf.renderer = "no WebGL"; return true; }
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    const r = String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) || "");
    perf.renderer = r;
    perf.software = /swiftshader|llvmpipe|software|basic render|microsoft basic|mesa offscreen|warp/i.test(r);
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  } catch { perf.software = false; }
  return perf.software;
}

/** Start the measuring loop once (App does it). */
export function startGovernor() {
  if (perf.started) return;
  perf.started = true;
  if (probeGpu() && perf.mode === "auto") setLevel("minimal");
  let last = performance.now(), ema = 16, acc = 0, n = 0;
  const step = (now: number) => {
    const dt = now - last; last = now;
    if (dt < 250) {                                     // a tab that was hidden is not a slow frame
      ema += (dt - ema) * 0.08;
      acc += dt; n++;
      if (acc >= 500) { perf.fps = Math.round(1000 * n / acc); perf.frameMs = Math.round(ema * 10) / 10; acc = 0; n = 0; }
      if (perf.mode === "auto") {
        // long frames (> 24 ms, i.e. under 42 fps) for ~2 s: step down; short frames (< 13 ms) for ~20 s: step up
        if (ema > 24) { perf.long += dt; perf.short = 0; } else if (ema < 13) { perf.short += dt; perf.long = 0; } else { perf.long = Math.max(0, perf.long - dt * 0.5); }
        const i = ORDER.indexOf(perf.level);
        if (perf.long > 2000 && i < ORDER.length - 1) { setLevel(ORDER[i + 1]); perf.long = 0; perf.short = 0; }
        if (perf.short > 20000 && i > 0) { setLevel(ORDER[i - 1]); perf.short = 0; }
      }
    }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---- what the art asks
/** The device-pixel cap for a 3D canvas. */
export function dprCap(): number { return perf.level === "full" ? 1.5 : perf.level === "lean" ? 1.25 : 1; }
/** How much of a lattice / particle count to keep. */
export function density(): number { return perf.level === "full" ? 1 : perf.level === "lean" ? 0.65 : 0.4; }
/** Glow halos (canvas shadowBlur) on the small 2D pieces. */
export function glows(): boolean { return perf.level === "full"; }
/** The whole-app backdrop scene. */
export function backdropOn(): boolean { return perf.level !== "minimal"; }
/** Frame interval for 2D loops that need not run at 60: faces, marks, bars. */
export function frameMs2D(): number { return perf.level === "full" ? 1000 / 60 : perf.level === "lean" ? 1000 / 30 : 1000 / 20; }
/** Frame interval for the 3D canvases (the faces, the helices): 60 at full, 30 lean, 20 minimal.
 *  The page's own rAF still runs at the screen's rate; the GPU work does not. */
export function frameMs3D(): number { return perf.level === "full" ? 0 : perf.level === "lean" ? 1000 / 30 : 1000 / 20; }
/** Every n-th frame for the expensive per-node work (the helix bend projection). */
export function bendEvery(): number { return perf.level === "full" ? 1 : perf.level === "lean" ? 2 : 3; }

/** A rAF loop with a frame budget: `fn(dt)` runs at most every `every()` ms and never while the
 *  tab is hidden. Returns a stop function. */
export function loop(fn: (dt: number, now: number) => void, every: () => number = frameMs2D): () => void {
  let raf = 0, last = performance.now(), due = 0, alive = true;
  const step = (now: number) => {
    if (!alive) return;
    raf = requestAnimationFrame(step);
    if (document.hidden) { last = now; return; }
    if (now < due) return;
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    due = now + every() - 1;
    fn(dt, now);
  };
  raf = requestAnimationFrame(step);
  return () => { alive = false; cancelAnimationFrame(raf); };
}

/** True while the element is on screen (IntersectionObserver); the callback fires on change. */
export function whenVisible(el: Element, on: (visible: boolean) => void): () => void {
  if (typeof IntersectionObserver === "undefined") { on(true); return () => undefined; }
  const io = new IntersectionObserver((entries) => { for (const e of entries) on(e.isIntersecting); }, { threshold: 0 });
  io.observe(el);
  return () => io.disconnect();
}
