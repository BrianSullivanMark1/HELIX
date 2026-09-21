// THE HELIX MARK - the one logo, drawn on a canvas at device pixels (crisp while it moves): a
// double helix turning about its own long axis, rungs lit as they face you, a spark orbiting.
// `radio` puts a headset on it - band over the top, cups that pulse with the music - and turns
// the spark into rising notes. Used by the wordmark (top-left) and the radio's header.
import { useEffect, useRef } from "react";
import { radioBeat } from "./Radio";
import { glows, loop } from "../lib/perf";

// a pre-rendered glow dot per strand: one drawImage instead of a shadowBlur fill per point (the
// blur was the mark's whole cost - 128 blurred fills a frame)
const SPRITES: Record<string, HTMLCanvasElement> = {};
function sprite(hue: number): HTMLCanvasElement {
  const k = String(hue);
  if (SPRITES[k]) return SPRITES[k];
  const c = document.createElement("canvas"); c.width = c.height = 32;
  const g = c.getContext("2d")!;
  const gr = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  gr.addColorStop(0, `hsla(${hue}, 100%, 92%, 1)`); gr.addColorStop(0.25, `hsla(${hue}, 95%, 70%, 0.9)`); gr.addColorStop(0.6, `hsla(${hue}, 95%, 60%, 0.25)`); gr.addColorStop(1, `hsla(${hue}, 95%, 60%, 0)`);
  g.fillStyle = gr; g.fillRect(0, 0, 32, 32);
  SPRITES[k] = c;
  return c;
}

export function drawHelixMark(g: CanvasRenderingContext2D, S: number, t: number, level: number, bins: Uint8Array | null, radio: boolean) {
  const N = 64, TURNS = 1.35, R = S * 0.21, H = S * 0.75, cx = S / 2, cy = S / 2;
  g.clearRect(0, 0, S, S);
  const halo = g.createRadialGradient(cx, cy, 2, cx, cy, S * 0.52);
  halo.addColorStop(0, `rgba(63,224,224,${(0.22 + 0.12 * Math.sin(t * 1.7) + 0.25 * level).toFixed(3)})`); halo.addColorStop(1, "rgba(63,224,224,0)");
  g.fillStyle = halo; g.fillRect(0, 0, S, S);
  for (const [r, sp, dash, a] of [[S * 0.49, 0.35, 3, 0.55], [S * 0.4, -0.7, 1.5, 0.35]] as [number, number, number, number][]) {
    g.save(); g.translate(cx, cy); g.rotate(t * sp); g.setLineDash([dash, dash * 1.8]); g.strokeStyle = `rgba(160,245,255,${a})`; g.lineWidth = 0.8;
    g.beginPath(); g.arc(0, 0, r, 0, Math.PI * 2); g.stroke(); g.restore();
  }
  const spin = t * 2.2, lean = 0.18;
  const pts: { x: number; y: number; z: number; s: number; k: number }[] = [];
  for (let s = 0; s < 2; s++) for (let i = 0; i < N; i++) {
    const k = i / (N - 1), a = k * TURNS * Math.PI * 2 + s * Math.PI + spin;
    const y = cy - H / 2 + k * H, x = cx + Math.cos(a) * R + (y - cy) * lean, z = Math.sin(a);
    pts.push({ x, y, z, s, k });
  }
  g.lineCap = "round";
  for (let i = 4; i < N - 4; i += 9) {
    const A = pts[i], B = pts[N + i];
    const front = (A.z + 1) / 2, lit = 0.25 + 0.75 * Math.abs(Math.cos(i / (N - 1) * TURNS * Math.PI * 2 + spin));
    g.strokeStyle = `rgba(223,251,255,${(0.25 + 0.6 * lit * (0.5 + 0.5 * front)).toFixed(3)})`; g.lineWidth = (1 + 0.9 * lit) * (S / 40);
    g.beginPath(); g.moveTo(A.x, A.y); g.lineTo(B.x, B.y); g.stroke();
  }
  pts.sort((p1, p2) => p1.z - p2.z);
  const glow = glows();
  for (const pt of pts) {
    const front = (pt.z + 1) / 2, edge = Math.min(1, pt.k / 0.12, (1 - pt.k) / 0.12);
    const hue = pt.s === 0 ? 187 : 212;
    const r = (1.15 + 1.35 * front) * (S / 40);
    g.globalAlpha = (0.35 + 0.65 * front) * edge;
    const d = r * (glow ? 4.2 : 2.6);
    g.drawImage(sprite(hue), pt.x - d / 2, pt.y - d / 2, d, d);
  }
  g.globalAlpha = 1;
  if (radio) {
    // THE HEADSET: a band over the crown, two cups on the sides; the cups swell with the low bands
    const bandR = S * 0.44;
    g.strokeStyle = "rgba(223,251,255,0.9)"; g.lineWidth = 2.4 * (S / 40); g.lineCap = "round";
    if (glow) { g.shadowColor = "#3fe0e0"; g.shadowBlur = 6 * (S / 40); }
    g.beginPath(); g.arc(cx, cy + S * 0.02, bandR, Math.PI * 1.08, Math.PI * 1.92); g.stroke();
    const lo = bins ? (bins[2] + bins[4] + bins[6]) / (3 * 255) : level;
    const hi = bins ? (bins[12] + bins[16]) / (2 * 255) : level * 0.6;
    for (const side of [-1, 1]) {
      const cxp = cx + side * bandR * 0.98, cyp = cy + S * 0.06;
      const w = S * 0.11 * (1 + 0.35 * (side < 0 ? lo : hi)), h = S * 0.2 * (1 + 0.2 * (side < 0 ? lo : hi));
      g.fillStyle = `rgba(223,251,255,${(0.75 + 0.25 * (side < 0 ? lo : hi)).toFixed(3)})`;
      g.beginPath(); g.roundRect(cxp - w / 2, cyp - h / 2, w, h, w / 2); g.fill();
    }
    g.shadowBlur = 0;
    // notes rising on the right when it plays
    if (level > 0.02) for (let i = 0; i < 3; i++) {
      const ph = (t * (0.6 + i * 0.2) + i * 0.37) % 1;
      const nx = cx + S * (0.36 + 0.06 * Math.sin(ph * 6 + i)), ny = cy - S * 0.1 - ph * S * 0.42;
      g.fillStyle = `rgba(223,251,255,${((1 - ph) * (0.5 + level)).toFixed(3)})`;
      g.beginPath(); g.arc(nx, ny, (1.1 + i * 0.3) * (S / 40), 0, Math.PI * 2); g.fill();
      g.fillRect(nx + (1.0 + i * 0.3) * (S / 40) - 0.5, ny - (4 + i) * (S / 40), 1, (4 + i) * (S / 40));
    }
  } else {
    const oa = t * 1.15, or = S * 0.52; const ox = cx + Math.cos(oa) * or, oy = cy + Math.sin(oa) * or;
    g.fillStyle = "rgba(63,224,224,0.4)"; g.beginPath(); g.arc(ox, oy, 3 * (S / 40), 0, Math.PI * 2); g.fill();
    g.fillStyle = "#fff"; g.beginPath(); g.arc(ox, oy, 1.5 * (S / 40), 0, Math.PI * 2); g.fill();
  }
}

export default function HelixMark({ size = 40, radio = false, onBeat }: { size?: number; radio?: boolean; onBeat?: (level: number, kick: boolean) => void }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    let t = 0;
    const c = canvas.current; const g = c?.getContext("2d");
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (c) { c.width = size * dpr; c.height = size * dpr; }
    return loop((dt) => {
      const b = radioBeat();
      t += dt * (1 + b.level * 1.5);
      onBeat?.(b.level, b.kick);
      if (g && c) { g.setTransform(dpr, 0, 0, dpr, 0, 0); drawHelixMark(g, size, t, b.level, b.bins, radio); }
    });
  }, [size, radio, onBeat]);
  return <canvas ref={canvas} style={{ width: size, height: size, display: "block" }} aria-hidden="true" />;
}
