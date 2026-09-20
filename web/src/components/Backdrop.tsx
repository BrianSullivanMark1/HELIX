// THE BACKDROP — a picked scene behind the whole app that moves to the radio. Six generative
// scenes, each rolled with a random seed when it starts so no two runs look the same, all
// driven by the beat (bass swells them, a kick hits them) and tinted by the playing track's
// theme. "Surprise me" picks a different scene for every song. Pure canvas; nothing when the
// scene is "off" or motion is reduced.
import { useEffect, useRef } from "react";
import { radioBeat } from "./Radio";

export type Scene = "off" | "nebula" | "grid" | "rain" | "aurora" | "warp" | "pulse";
export const SCENES: { key: Scene; name: string; blurb: string }[] = [
  { key: "off", name: "Just the helix", blurb: "The pages as they are - the helix and the net still keep the beat." },
  { key: "nebula", name: "Nebula", blurb: "Soft clouds of color drifting; the bass swells them." },
  { key: "grid", name: "The grid", blurb: "A floor of light rushing past, horizon flaring on the kick." },
  { key: "rain", name: "Code rain", blurb: "Falling 0s and 1s, faster with the energy." },
  { key: "aurora", name: "Aurora", blurb: "Ribbons of light rolling with the music." },
  { key: "warp", name: "Warp", blurb: "Stars streaking toward you; a kick throws you forward." },
  { key: "pulse", name: "Pulse rings", blurb: "Rings ripple out from the center on every beat." },
];
export interface Look { scene: Scene; surprise: boolean; intensity: number; layout: "full" | "compact" }
const KEY = "helix_radio_look";
export function loadLook(): Look {
  try { const v = localStorage.getItem(KEY); if (v) return { scene: "nebula", surprise: false, intensity: 0.8, layout: "full", ...JSON.parse(v) }; } catch { /* no storage */ }
  return { scene: "nebula", surprise: false, intensity: 0.8, layout: "full" };
}
export function saveLook(l: Look) { try { localStorage.setItem(KEY, JSON.stringify(l)); } catch { /* no storage */ } window.dispatchEvent(new CustomEvent("helix-look", { detail: l })); }

const THEME_HUE: Record<string, [number, number]> = { chill: [185, 225], hype: [8, 42], dark: [265, 305], happy: [40, 75], focus: [195, 215], epic: [320, 350], "": [180, 230] };
const REDUCED = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function rng(seed: number) { let s = seed >>> 0 || 1; return () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return ((s >>> 0) % 100000) / 100000; }; }

export default function Backdrop() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || REDUCED) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let look = loadLook();
    let scene: Scene = look.scene;
    let seed = Math.floor(Math.random() * 1e9);
    let lastTheme = "";
    let w = 0, h = 0, raf = 0, last = performance.now(), alive = true, t = 0;
    let objs: Record<string, unknown> = {};
    const dpr = Math.min(1.5, window.devicePixelRatio || 1);
    const resize = () => { w = window.innerWidth; h = window.innerHeight; canvas.width = w * dpr; canvas.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); objs = {}; };
    resize();
    window.addEventListener("resize", resize);
    const onLook = (e: Event) => { look = (e as CustomEvent).detail as Look; if (!look.surprise) scene = look.scene; seed = Math.floor(Math.random() * 1e9); objs = {}; };
    window.addEventListener("helix-look", onLook);
    const pick = () => { const pool = SCENES.filter((s) => s.key !== "off"); scene = pool[Math.floor(Math.random() * pool.length)].key; seed = Math.floor(Math.random() * 1e9); objs = {}; };
    if (look.surprise) pick();

    const step = (now: number) => {
      if (!alive) return;
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      const beat = radioBeat();
      if (beat.theme !== lastTheme) { lastTheme = beat.theme; if (look.surprise && beat.playing) pick(); }
      // the palette drifts around the color wheel (a full turn every ~90 s) - the theme sets where it starts
      const [b0, b1] = THEME_HUE[beat.theme] || THEME_HUE[""];
      const drift = (t * 4) % 360;
      const h0 = b0 + drift, h1 = b1 + drift;
      const lvl = beat.level * look.intensity, kick = beat.kick;
      const amp = 0.25 + 0.75 * look.intensity;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (scene === "off") { ctx.clearRect(0, 0, w, h); raf = requestAnimationFrame(step); return; }
      const R = rng(seed);
      // ---------------------------------------------------------------- nebula
      if (scene === "nebula") {
        type Blob = { x: number; y: number; r: number; hue: number; vx: number; vy: number; ph: number };
        const blobs = (objs.blobs as Blob[]) || (objs.blobs = Array.from({ length: 7 + Math.floor(R() * 4) }, () => ({ x: R() * w, y: R() * h, r: (0.18 + R() * 0.22) * Math.max(w, h), hue: b0 + R() * (b1 - b0), vx: (R() - 0.5) * 18, vy: (R() - 0.5) * 12, ph: R() * 6 })));
        ctx.fillStyle = "rgba(8,11,15,0.35)"; ctx.fillRect(0, 0, w, h);
        ctx.globalCompositeOperation = "lighter";
        for (const b of blobs) {
          b.x += b.vx * dt * (1 + lvl * 2); b.y += b.vy * dt * (1 + lvl * 2);
          if (b.x < -b.r) b.x = w + b.r; if (b.x > w + b.r) b.x = -b.r; if (b.y < -b.r) b.y = h + b.r; if (b.y > h + b.r) b.y = -b.r;
          const r = b.r * (0.85 + 0.15 * Math.sin(t * 0.7 + b.ph) + lvl * 0.35 + (kick ? 0.12 : 0));
          const g = ctx.createRadialGradient(b.x, b.y, 0, b.x, b.y, r);
          g.addColorStop(0, `hsla(${b.hue + drift}, 85%, 55%, ${(0.10 + 0.16 * lvl) * amp})`); g.addColorStop(1, "transparent");
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(b.x, b.y, r, 0, Math.PI * 2); ctx.fill();
        }
        ctx.globalCompositeOperation = "source-over";
      }
      // ---------------------------------------------------------------- grid
      if (scene === "grid") {
        const st = (objs.g as { off: number; hue: number; tilt: number }) || (objs.g = { off: 0, hue: b0 + R() * (b1 - b0), tilt: 0.35 + R() * 0.25 });
        st.off = (st.off + dt * (0.35 + lvl * 1.6)) % 1;
        ctx.clearRect(0, 0, w, h);
        const horizon = h * (0.42 + 0.06 * Math.sin(t * 0.2));
        const glow = ctx.createLinearGradient(0, horizon - 120, 0, horizon + 40);
        glow.addColorStop(0, "transparent"); glow.addColorStop(1, `hsla(${st.hue + drift}, 90%, 60%, ${(0.10 + 0.5 * lvl + (kick ? 0.25 : 0)) * amp})`);
        ctx.fillStyle = glow; ctx.fillRect(0, horizon - 120, w, 160);
        ctx.strokeStyle = `hsla(${st.hue + drift}, 90%, 62%, ${(0.18 + 0.4 * lvl) * amp})`; ctx.lineWidth = 1;
        for (let i = 0; i < 18; i++) {
          const z = (i + st.off) / 18;
          const y = horizon + Math.pow(z, 2.2) * (h - horizon);
          ctx.globalAlpha = 0.15 + z * 0.85;
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
        }
        ctx.globalAlpha = 1;
        for (let i = -12; i <= 12; i++) {
          const x = w / 2 + i * w * 0.09;
          ctx.beginPath(); ctx.moveTo(w / 2 + i * 6, horizon); ctx.lineTo(x + i * w * st.tilt, h); ctx.stroke();
        }
      }
      // ---------------------------------------------------------------- rain
      if (scene === "rain") {
        type Col = { y: number; v: number; hue: number };
        const size = 15, n = Math.ceil(w / size);
        const cols = (objs.cols as Col[]) || (objs.cols = Array.from({ length: n }, () => ({ y: -R() * h, v: 120 + R() * 260, hue: b0 + R() * (b1 - b0) })));
        ctx.fillStyle = "rgba(8,11,15,0.28)"; ctx.fillRect(0, 0, w, h);
        ctx.font = `${size}px ui-monospace, Menlo, Consolas, monospace`;
        for (let i = 0; i < n; i++) {
          const c = cols[i];
          c.y += c.v * dt * (0.6 + lvl * 2.4 + (kick ? 1 : 0));
          if (c.y > h + 40) { c.y = -R() * 200; c.v = 120 + Math.random() * 260; }
          ctx.fillStyle = `hsla(${c.hue + drift}, 90%, ${70 + lvl * 25}%, ${(0.35 + lvl * 0.5) * amp})`;
          ctx.fillText(Math.random() < 0.5 ? "0" : "1", i * size, c.y);
        }
      }
      // ---------------------------------------------------------------- aurora
      if (scene === "aurora") {
        const ribs = (objs.ribs as { hue: number; ph: number; k: number; y: number }[]) || (objs.ribs = Array.from({ length: 4 }, (_, i) => ({ hue: b0 + R() * (b1 - b0), ph: R() * 6, k: 0.6 + R() * 1.2, y: 0.25 + i * 0.16 + R() * 0.08 })));
        ctx.fillStyle = "rgba(8,11,15,0.3)"; ctx.fillRect(0, 0, w, h);
        ctx.globalCompositeOperation = "lighter";
        for (const r of ribs) {
          ctx.beginPath();
          const A = h * (0.05 + 0.10 * lvl + (kick ? 0.03 : 0));
          for (let x = 0; x <= w; x += 12) {
            const y = h * r.y + Math.sin(x * 0.004 * r.k + t * 0.6 + r.ph) * A + Math.sin(x * 0.011 + t * 1.3 + r.ph) * A * 0.35;
            if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
          const g = ctx.createLinearGradient(0, h * r.y - 80, 0, h * r.y + 260);
          g.addColorStop(0, `hsla(${r.hue + drift}, 90%, 60%, ${(0.16 + 0.3 * lvl) * amp})`); g.addColorStop(1, "transparent");
          ctx.fillStyle = g; ctx.fill();
        }
        ctx.globalCompositeOperation = "source-over";
      }
      // ---------------------------------------------------------------- warp
      if (scene === "warp") {
        type Star = { x: number; y: number; z: number; hue: number };
        const stars = (objs.stars as Star[]) || (objs.stars = Array.from({ length: 260 }, () => ({ x: (R() - 0.5) * 2, y: (R() - 0.5) * 2, z: R(), hue: b0 + R() * (b1 - b0) })));
        const sp = (objs.sp as { v: number }) || (objs.sp = { v: 0.25 });
        sp.v += ((0.2 + lvl * 1.6 + (kick ? 1.2 : 0)) - sp.v) * 0.08;
        ctx.fillStyle = `rgba(8,11,15,${0.35 - lvl * 0.15})`; ctx.fillRect(0, 0, w, h);
        ctx.globalCompositeOperation = "lighter";
        for (const s of stars) {
          const pz = s.z; s.z -= sp.v * dt; if (s.z <= 0.02) { s.z = 1; s.x = (Math.random() - 0.5) * 2; s.y = (Math.random() - 0.5) * 2; }
          const x1 = w / 2 + (s.x / pz) * w * 0.5, y1 = h / 2 + (s.y / pz) * h * 0.5;
          const x2 = w / 2 + (s.x / s.z) * w * 0.5, y2 = h / 2 + (s.y / s.z) * h * 0.5;
          ctx.strokeStyle = `hsla(${s.hue + drift}, 80%, 75%, ${(0.25 + (1 - s.z) * 0.7) * amp})`; ctx.lineWidth = (1 - s.z) * 2.2 + 0.3;
          ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
        }
        ctx.globalCompositeOperation = "source-over";
      }
      // ---------------------------------------------------------------- pulse
      if (scene === "pulse") {
        const rings = (objs.rings as { r: number; hue: number; a: number }[]) || (objs.rings = []);
        const hue = h0 + ((objs.hh as number) ?? (objs.hh = R())) * (h1 - h0);   // rings are short-lived: born already drifted
        if (kick || (beat.playing && Math.random() < dt * 0.4)) rings.push({ r: 0, hue: hue + (Math.random() - 0.5) * 30, a: 0.7 + lvl * 0.3 });
        if (!beat.playing && Math.random() < dt * 0.25) rings.push({ r: 0, hue, a: 0.35 });
        ctx.fillStyle = "rgba(8,11,15,0.32)"; ctx.fillRect(0, 0, w, h);
        ctx.globalCompositeOperation = "lighter";
        const maxR = Math.hypot(w, h) * 0.6;
        for (const r of rings) {
          r.r += dt * (220 + lvl * 500);
          const k = 1 - r.r / maxR;
          if (k <= 0) continue;
          ctx.strokeStyle = `hsla(${r.hue}, 90%, 62%, ${r.a * k * amp})`; ctx.lineWidth = 2 + lvl * 4 + k * 2;
          ctx.beginPath(); ctx.arc(w / 2, h * 0.5, r.r, 0, Math.PI * 2); ctx.stroke();
        }
        objs.rings = rings.filter((r) => r.r < maxR);
        ctx.globalCompositeOperation = "source-over";
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => { alive = false; cancelAnimationFrame(raf); window.removeEventListener("resize", resize); window.removeEventListener("helix-look", onLook); };
  }, []);
  return <canvas ref={ref} className="fixed inset-0" style={{ zIndex: 0, pointerEvents: "none" }} aria-hidden="true" />;
}
