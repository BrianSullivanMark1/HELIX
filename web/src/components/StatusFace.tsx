// THE STATUS FACE — a small HELIX head, 2D canvas (no WebGL context spent), coloured and acting out
// a status on loop: running = calm, easy blink, slow nod; struggling = worried brows, glance about;
// down = eyes shut, flat mouth, a slow shake; not deployed = a sketch, dotted, a shrug; unknown =
// a raised brow, looking around; reading = eyes darting, mouth "hmm"; queued = a patient sway.
// Click it for the words. Hover lifts it.
import { useEffect, useRef } from "react";
import { loop, whenVisible } from "../lib/perf";

export type FaceStatus = "ok" | "degraded" | "down" | "absent" | "unknown" | "reading" | "queued" | "prototype";
const HUE: Record<FaceStatus, number> = { ok: 150, degraded: 40, down: 0, absent: 210, unknown: 200, reading: 190, queued: 220, prototype: 46 };
const LABEL: Record<FaceStatus, string> = { ok: "running", degraded: "struggling", down: "down", absent: "not deployed", unknown: "not read yet", reading: "reading", queued: "queued", prototype: "prototype" };

export default function StatusFace({ status, size = 34, title, onClick, attention }: { status: FaceStatus; size?: number; title?: string; onClick?: () => void; attention?: boolean }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    c.width = size * dpr; c.height = size * dpr;
    let t = Math.random() * 10, blink = 0, nextBlink = 2 + Math.random() * 3;
    const hue = HUE[status];
    // A page can hold twenty of these: each runs on the governor's 2D budget (30 fps when lean) and
    // stops entirely while it is scrolled out of view.
    let stop: (() => void) | null = null;
    const step = (dt: number) => {
      t += dt;
      nextBlink -= dt; if (nextBlink <= 0) { blink = 1; nextBlink = 2 + Math.random() * 4; }
      blink = Math.max(0, blink - dt * 6);
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, size, size);
      const R = size * 0.42, cx = size / 2, cy = size / 2;
      // the act
      let dx = 0, dy = 0, rot = 0, browL = 0, browR = 0, eyeOpen = 1, mouthCurve = 0.3, mouthOpen = 0, lookX = 0, lookY = 0, dotted = false, glowK = 0.5;
      const sway = Math.sin(t * 1.4);
      switch (status) {
        case "ok": dy = Math.sin(t * 1.6) * 0.6; rot = Math.sin(t * 0.7) * 0.05; mouthCurve = 0.6; lookX = Math.sin(t * 0.5) * 0.3; glowK = 0.75 + 0.25 * Math.sin(t * 2); break;
        case "degraded": browL = -0.5; browR = 0.5; mouthCurve = -0.15; lookX = Math.sin(t * 3) * 0.7; dx = Math.sin(t * 5) * 0.4; glowK = 0.6 + 0.4 * Math.abs(Math.sin(t * 3)); break;
        case "down": eyeOpen = 0.08; mouthCurve = -0.6; rot = Math.sin(t * 1.2) * 0.12; dy = 1; glowK = 0.35 + 0.2 * Math.sin(t * 1.2); break;
        case "absent": dotted = true; rot = Math.sin(t * 0.9) * 0.08; mouthCurve = 0; browL = browR = 0.3; glowK = 0.3; break;
        case "unknown": browL = 0.6; lookX = Math.sin(t * 0.9) * 0.9; lookY = Math.cos(t * 0.7) * 0.4; mouthCurve = 0.05; glowK = 0.45; break;
        case "reading": lookX = Math.sin(t * 6) * 0.8; lookY = Math.sin(t * 4.3) * 0.5; mouthOpen = 0.25 + 0.2 * Math.sin(t * 8); browL = browR = 0.4; glowK = 0.7 + 0.3 * Math.sin(t * 6); break;
        case "queued": dx = sway * 1.2; rot = sway * 0.1; mouthCurve = 0.2; lookX = sway * 0.4; glowK = 0.5; break;
        case "prototype": dy = Math.sin(t * 2) * 0.8; mouthCurve = 0.5; browL = browR = 0.5; lookX = Math.sin(t * 1.1) * 0.5; glowK = 0.6 + 0.3 * Math.sin(t * 2.5); break;
      }
      // glow
      const gl = g.createRadialGradient(cx, cy, R * 0.4, cx, cy, R * 1.5);
      gl.addColorStop(0, `hsla(${hue}, 95%, 60%, ${0.35 * glowK})`); gl.addColorStop(1, "transparent");
      g.fillStyle = gl; g.fillRect(0, 0, size, size);
      g.save(); g.translate(cx + dx, cy + dy); g.rotate(rot);
      // the head
      const skin = g.createRadialGradient(-R * 0.3, -R * 0.3, R * 0.1, 0, 0, R);
      skin.addColorStop(0, `hsla(${hue}, 60%, 32%, ${dotted ? 0.4 : 1})`); skin.addColorStop(1, `hsla(${hue}, 55%, 12%, ${dotted ? 0.4 : 1})`);
      g.fillStyle = skin; g.beginPath(); g.arc(0, 0, R, 0, Math.PI * 2); g.fill();
      g.setLineDash(dotted ? [2, 3] : []); g.strokeStyle = `hsl(${hue}, 90%, 65%)`; g.lineWidth = 1.2; g.stroke(); g.setLineDash([]);
      // brows
      g.strokeStyle = `hsl(${hue}, 95%, 78%)`; g.lineWidth = Math.max(1.2, R * 0.12); g.lineCap = "round";
      const by = -R * 0.38;
      g.beginPath(); g.moveTo(-R * 0.55, by + browL * R * 0.12 + R * 0.05); g.quadraticCurveTo(-R * 0.32, by - R * 0.08 - browL * R * 0.1, -R * 0.12, by + R * 0.02 - browL * R * 0.05); g.stroke();
      g.beginPath(); g.moveTo(R * 0.55, by + browR * R * 0.12 + R * 0.05); g.quadraticCurveTo(R * 0.32, by - R * 0.08 - browR * R * 0.1, R * 0.12, by + R * 0.02 - browR * R * 0.05); g.stroke();
      // eyes
      const eh = R * 0.16 * eyeOpen * (1 - blink * 0.9);
      for (const sx of [-1, 1]) {
        const ex = sx * R * 0.32 + lookX * R * 0.06, ey = -R * 0.1 + lookY * R * 0.05;
        g.fillStyle = "#04070a"; g.beginPath(); g.ellipse(ex, ey, R * 0.2, Math.max(R * 0.02, eh), 0, 0, Math.PI * 2); g.fill();
        g.strokeStyle = `hsl(${hue}, 95%, 75%)`; g.lineWidth = 1; g.stroke();
        if (eh > R * 0.05) { g.fillStyle = `hsl(${hue}, 95%, 72%)`; g.beginPath(); g.arc(ex + lookX * R * 0.05, ey, Math.min(eh * 0.8, R * 0.09), 0, Math.PI * 2); g.fill(); g.fillStyle = "#000"; g.beginPath(); g.arc(ex + lookX * R * 0.05, ey, R * 0.035, 0, Math.PI * 2); g.fill(); }
      }
      // mouth
      const my = R * 0.36, mw = R * 0.38;
      g.strokeStyle = `hsl(${hue}, 95%, 75%)`; g.lineWidth = Math.max(1.3, R * 0.1);
      g.beginPath(); g.moveTo(-mw, my); g.quadraticCurveTo(0, my + mouthCurve * R * 0.35 + mouthOpen * R * 0.2, mw, my); g.stroke();
      if (mouthOpen > 0.1) { g.fillStyle = "#04070a"; g.beginPath(); g.moveTo(-mw * 0.7, my + R * 0.02); g.quadraticCurveTo(0, my + mouthOpen * R * 0.5, mw * 0.7, my + R * 0.02); g.fill(); }
      g.restore();
    };
    const unwatch = whenVisible(c, (vis) => { if (vis && !stop) stop = loop(step); else if (!vis && stop) { stop(); stop = null; } });
    return () => { unwatch(); if (stop) stop(); };
  }, [status, size]);
  // ATTENTION (Brian, 2026-09-22): a face that needs a look pops toward you - a slow scale beat,
  // never enough to overflow its row
  return <canvas ref={ref} className={`status-face${attention ? " attn" : ""}`} style={{ width: size, height: size }} data-tip={title || LABEL[status]} onClick={onClick} role={onClick ? "button" : undefined} />;
}

export const STATUS_LABEL = LABEL;
