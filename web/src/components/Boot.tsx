// THE BOOT — the curtain that rises while HELIX wakes: a helix assembling itself from dust, rungs
// firing up the strand, the word decoding from noise, a ring of light that closes when the first
// read lands. Shown on every launch (the Console's first read takes a moment anyway). Pure canvas.
import { useEffect, useRef, useState } from "react";
import "./boot.css";

export default function Boot({ ready, minMs = 2200 }: { ready: boolean; minMs?: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const [gone, setGone] = useState(false);
  const [fading, setFading] = useState(false);
  const started = useRef(performance.now());
  const readyRef = useRef(ready); readyRef.current = ready;
  useEffect(() => {
    if (!ready) return;
    const wait = Math.max(0, minMs - (performance.now() - started.current));
    const t1 = window.setTimeout(() => setFading(true), wait);
    const t2 = window.setTimeout(() => setGone(true), wait + 900);
    return () => { window.clearTimeout(t1); window.clearTimeout(t2); };
  }, [ready, minMs]);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    let raf = 0, t = 0, last = performance.now(), w = 0, h = 0;
    const dpr = Math.min(1.5, window.devicePixelRatio || 1);
    const size = () => { w = window.innerWidth; h = window.innerHeight; c.width = w * dpr; c.height = h * dpr; };
    size(); window.addEventListener("resize", size);
    const N = 160;
    const dust = Array.from({ length: 260 }, () => ({ x: Math.random(), y: Math.random(), v: 0.02 + Math.random() * 0.05, s: Math.random() }));
    const WORD = "HELIX";
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.fillStyle = "#06090d"; g.fillRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const build = Math.min(1, t / 1.6);                     // the helix assembles over 1.6 s
      const ease = 1 - Math.pow(1 - build, 3);
      // dust drifting in toward the axis
      g.globalCompositeOperation = "lighter";
      for (const d of dust) {
        d.y = (d.y + d.v * dt) % 1;
        const px = cx + (d.x - 0.5) * w * (1 - ease * 0.6), py = d.y * h;
        g.fillStyle = `hsla(${185 + d.s * 40}, 90%, 70%, ${0.15 + 0.25 * d.s})`;
        g.beginPath(); g.arc(px, py, 0.8 + d.s * 1.4, 0, Math.PI * 2); g.fill();
      }
      // the helix: two strands of points along a vertical axis, turning
      const H = Math.min(h * 0.5, 420), R = Math.min(w, h) * 0.1, turns = 2.6, ringR = H / 2 + 46;
      const spin = t * 1.1;
      for (let s = 0; s < 2; s++) {
        for (let i = 0; i < N; i++) {
          const k = i / (N - 1);
          if (k > ease) break;
          const a = k * turns * Math.PI * 2 + s * Math.PI + spin;
          const x = cx + Math.cos(a) * R, y = cy - H / 2 + k * H, z = Math.sin(a);
          const front = (z + 1) / 2;
          const hue = s === 0 ? 185 : 215;
          const gold = i % 23 === 0;
          g.fillStyle = gold ? `hsla(42, 95%, 65%, ${0.5 + 0.5 * front})` : `hsla(${hue}, 90%, ${55 + front * 30}%, ${0.35 + 0.65 * front})`;
          g.beginPath(); g.arc(x, y, 1.6 + front * 2.4, 0, Math.PI * 2); g.fill();
          // rungs, every 8th, lit by a wave climbing the strand
          if (s === 0 && i % 8 === 0) {
            const a2 = a + Math.PI; const x2 = cx + Math.cos(a2) * R;
            const wave = Math.max(0, Math.sin(k * 9 - t * 3.2));
            g.strokeStyle = `hsla(190, 90%, 70%, ${0.08 + 0.7 * wave * wave})`; g.lineWidth = 1 + wave * 1.5;
            g.beginPath(); g.moveTo(x, y); g.lineTo(x2, y); g.stroke();
          }
        }
      }
      // the halo
      const halo = g.createRadialGradient(cx, cy, 0, cx, cy, ringR);
      halo.addColorStop(0, `hsla(190, 90%, 60%, ${0.12 * ease})`); halo.addColorStop(1, "transparent");
      g.fillStyle = halo; g.fillRect(0, 0, w, h);
      // the ring that closes as it gets ready
      const isReady = readyRef.current;
      const prog = isReady ? 1 : Math.min(0.86, t / 4);
      g.strokeStyle = `hsla(190, 90%, 70%, 0.9)`; g.lineWidth = 2; g.lineCap = "round";
      g.shadowColor = "hsla(190, 100%, 70%, 0.9)"; g.shadowBlur = 12;
      g.beginPath(); g.arc(cx, cy, ringR, -Math.PI / 2, -Math.PI / 2 + prog * Math.PI * 2); g.stroke();
      g.shadowBlur = 0;
      g.strokeStyle = `hsla(190, 90%, 70%, 0.15)`; g.beginPath(); g.arc(cx, cy, ringR, 0, Math.PI * 2); g.stroke();
      // a spark rides the ring's tip
      const tipA = -Math.PI / 2 + prog * Math.PI * 2;
      g.fillStyle = "#fff"; g.beginPath(); g.arc(cx + Math.cos(tipA) * ringR, cy + Math.sin(tipA) * ringR, 3, 0, Math.PI * 2); g.fill();
      g.globalCompositeOperation = "source-over";
      // the word, decoding from noise
      const reveal = Math.min(1, Math.max(0, (t - 0.6) / 1.4));
      g.font = `800 ${Math.round(Math.min(w, h) * 0.075)}px ui-sans-serif, system-ui`; g.textAlign = "center"; g.textBaseline = "middle";
      let word = "";
      for (let i = 0; i < WORD.length; i++) word += i / WORD.length < reveal ? WORD[i] : "01#/\\|"[Math.floor(Math.random() * 6)];
      g.letterSpacing = "10px";
      g.shadowColor = "hsla(190, 100%, 70%, 0.9)"; g.shadowBlur = 24;
      g.fillStyle = `hsla(190, 90%, 78%, ${0.35 + 0.65 * reveal})`; g.fillText(word, cx, cy + ringR + 54);
      g.shadowBlur = 0;
      g.font = `500 11px ui-sans-serif, system-ui`; g.letterSpacing = "4px"; g.fillStyle = "hsla(190, 40%, 70%, 0.6)";
      g.fillText(isReady ? "READY" : "WAKING UP - READING THE FLEET", cx, cy + ringR + 86);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", size); };
  }, []);
  if (gone) return null;
  return <canvas ref={ref} className={`boot${fading ? " fading" : ""}`} aria-hidden="true" />;
}
