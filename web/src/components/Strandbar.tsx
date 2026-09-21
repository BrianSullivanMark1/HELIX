// THE STRANDBAR - HELIX's one progress bar, used everywhere work takes time.
//
// A helix being SEQUENCED: two strands wind along the bar; as far as the work has gone they are
// lit, with the rungs between them switched on and a bright bead riding the tip; beyond the tip
// the strands are dim ghost. The strands keep turning while the work runs. Unknown length = a
// bright reading window that sweeps along the ghost strands. Done = the whole strand turns GOLD in
// one sweep from the left, with a puff of sparks off the tip. Failed = the strand goes ember-red
// and breaks in three places; stopped = it fades to grey. Under (or beside) the bar a small word
// says what the sequencer is doing - transcribing, translating, folding... - cycling while it
// runs, then SEQUENCED / BROKEN / STOPPED.
//
// 2D canvas at device pixels (crisp at any zoom), no WebGL context spent, ~0.1 ms a frame, and it
// stops drawing once the settle animation is over. `progress` is 0..1 or null (unknown).
import { useEffect, useRef, useState } from "react";
import "./strandbar.css";
import { frameMs2D, glows } from "../lib/perf";

export type StrandState = "running" | "done" | "failed" | "cancelled";

const WORDS = ["sequencing", "transcribing", "translating", "folding", "annealing", "splicing", "replicating", "ligating", "proofreading"];
const HUE: Record<StrandState, [number, number]> = { running: [190, 215], done: [42, 48], failed: [12, 0], cancelled: [200, 200] };

export default function Strandbar({ progress, state, height = 22, words = true, note, compact }: {
  progress: number | null; state: StrandState; height?: number; words?: boolean; note?: string; compact?: boolean;
}) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const prog = useRef(progress ?? 0);
  const target = useRef(progress);
  const stateRef = useRef(state);
  const settled = useRef(0);        // when the state last changed (performance.now)
  const sparks = useRef<{ x: number; y: number; vx: number; vy: number; life: number }[]>([]);
  const [word, setWord] = useState(WORDS[Math.floor(Math.random() * WORDS.length)]);
  target.current = progress;
  useEffect(() => { if (stateRef.current !== state) { stateRef.current = state; settled.current = performance.now(); } }, [state]);
  useEffect(() => {
    if (state !== "running" || !words) return;
    const id = window.setInterval(() => setWord(WORDS[Math.floor(Math.random() * WORDS.length)]), 2400);
    return () => window.clearInterval(id);
  }, [state, words]);

  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    let raf = 0, t = Math.random() * 10, last = performance.now(), w = 0, h = height, due = 0;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const size = () => { const r = c.getBoundingClientRect(); w = Math.max(40, r.width); c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); };
    size();
    const ro = new ResizeObserver(size); ro.observe(c);
    let idleFrames = 0;
    const step = (now: number) => {
      const st = stateRef.current;
      const since = (now - settled.current) / 1000;
      // the governor's 2D budget, except the first two seconds after a state change (the sweep)
      if (now < due || document.hidden) { raf = requestAnimationFrame(step); return; }
      due = now + (since < 2 ? 0 : frameMs2D() - 1);
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      // ease the tip toward the reported progress
      const tp = target.current;
      if (tp !== null && tp !== undefined) prog.current += (tp - prog.current) * Math.min(1, dt * 6);
      if (st === "done") prog.current += (1 - prog.current) * Math.min(1, dt * 5);
      const p = Math.max(0, Math.min(1, prog.current));
      const unknown = st === "running" && (tp === null || tp === undefined);
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
      const cy = h / 2, A = h * 0.30, L = compact ? 34 : 46, pad = 6;
      const [h1, h2] = HUE[st];
      const spin = st === "running" ? t * 2.2 : t * 0.35;
      const tipX = pad + (w - pad * 2) * (unknown ? 1 : p);
      // the done sweep runs left to right over 0.9 s; the fail crack grows over 0.5 s
      const sweep = st === "done" ? Math.min(1, since / 0.9) : 1;
      const goldTo = pad + (w - pad * 2) * sweep;
      // a reading window for unknown length
      const win = ((t * 0.55) % 1.3) - 0.15, winW = 0.22;
      const lit = (x: number) => {
        const k = (x - pad) / (w - pad * 2);
        if (unknown) return Math.max(0, 1 - Math.abs(k - win) / winW);
        if (st === "done") return x <= goldTo ? 1 : x <= tipX ? 0.9 : 0;
        if (st === "failed") return tp === null || tp === undefined || p < 0.02 ? 0.6 : x <= tipX ? 1 : 0.35;   // ember along the whole strand
        return x <= tipX ? 1 : 0;
      };
      const cracks = st === "failed" ? [0.31, 0.58, 0.83].map((k) => pad + (w - pad * 2) * k) : [];
      const glow = glows();
      const cracked = (x: number) => cracks.some((cx) => Math.abs(x - cx) < 5 * Math.min(1, since / 0.5));
      // rungs
      const rungEvery = L / 4;
      for (let x = pad; x <= w - pad; x += rungEvery) {
        const a1 = (x / L) * Math.PI * 2 + spin, y1 = cy + Math.sin(a1) * A, y2 = cy - Math.sin(a1) * A;
        const k = lit(x);
        if (cracked(x)) continue;
        g.strokeStyle = `hsla(${h1}, 90%, ${60 + 20 * k}%, ${0.08 + 0.55 * k})`; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x, y1); g.lineTo(x, y2); g.stroke();
      }
      // strands: short segments so each carries its own light; the glow is ONE wide faint stroke
      // under the lit part per strand (a blurred stroke per segment was the bar's whole cost)
      const seg = 3;
      for (let s = 0; s < 2; s++) {
        const hue = s === 0 ? h1 : h2;
        let px = pad, pa = (pad / L) * Math.PI * 2 + spin + s * Math.PI, py = cy + Math.sin(pa) * A;
        const glowPath = glow && st !== "cancelled" ? new Path2D() : null;
        let glowOpen = false;
        for (let x = pad + seg; x <= w - pad + 0.01; x += seg) {
          const a = (x / L) * Math.PI * 2 + spin + s * Math.PI, y = cy + Math.sin(a) * A;
          const front = (Math.cos(a) + 1) / 2;         // the strand's near side is brighter and thicker
          const k = lit(x);
          const broken = cracked(x);
          if (!broken) {
            const alpha = st === "cancelled" ? 0.25 : 0.14 + 0.86 * k;
            g.strokeStyle = `hsla(${hue}, ${st === "cancelled" ? 10 : 92}%, ${52 + front * 30 + (st === "done" ? 6 : 0)}%, ${alpha * (0.55 + 0.45 * front)})`;
            g.lineWidth = (compact ? 1.2 : 1.5) + front * (compact ? 1.2 : 1.6) * (0.6 + 0.4 * k);
            g.beginPath(); g.moveTo(px, py); g.lineTo(x, y); g.stroke();
            if (glowPath && k > 0.5 && front > 0.35) { if (!glowOpen) { glowPath.moveTo(px, py); glowOpen = true; } glowPath.lineTo(x, y); } else glowOpen = false;
          } else glowOpen = false;
          px = x; py = y;
        }
        if (glowPath) { g.strokeStyle = `hsla(${hue}, 100%, 70%, 0.22)`; g.lineWidth = compact ? 5 : 7; g.lineCap = "round"; g.stroke(glowPath); }
      }
      // the bead at the tip
      if (st === "running" && !unknown) {
        const pulse = 0.75 + 0.25 * Math.sin(t * 9);
        const halo = g.createRadialGradient(tipX, cy, 0, tipX, cy, 9 * pulse);
        halo.addColorStop(0, `hsla(${h1}, 100%, 85%, 0.9)`); halo.addColorStop(1, `hsla(${h1}, 100%, 65%, 0)`);
        g.fillStyle = halo; g.beginPath(); g.arc(tipX, cy, 9 * pulse, 0, Math.PI * 2); g.fill();
        g.fillStyle = "#fff"; g.beginPath(); g.arc(tipX, cy, 2.2, 0, Math.PI * 2); g.fill();
      }
      // gold sweep head + sparks
      if (st === "done" && since < 1.6) {
        if (since < 0.9) {
          const head = g.createRadialGradient(goldTo, cy, 0, goldTo, cy, 16);
          head.addColorStop(0, "rgba(255,246,210,0.95)"); head.addColorStop(1, "rgba(255,200,80,0)");
          g.fillStyle = head; g.beginPath(); g.arc(goldTo, cy, 16, 0, Math.PI * 2); g.fill();
          if (Math.random() < 0.6) sparks.current.push({ x: goldTo, y: cy, vx: (Math.random() - 0.3) * 40, vy: -20 - Math.random() * 50, life: 0.7 });
        }
        for (const s of sparks.current) { s.x += s.vx * dt; s.y += s.vy * dt; s.vy += 60 * dt; s.life -= dt; g.fillStyle = `hsla(45, 100%, 75%, ${Math.max(0, s.life)})`; g.beginPath(); g.arc(s.x, s.y, 1.4, 0, Math.PI * 2); g.fill(); }
        sparks.current = sparks.current.filter((s) => s.life > 0);
      }
      // ember glow on a fresh failure
      if (st === "failed" && since < 1.2) {
        const gl = g.createLinearGradient(0, 0, w, 0);
        gl.addColorStop(0, `rgba(255,90,40,${0.18 * (1 - since / 1.2)})`); gl.addColorStop(1, "rgba(255,90,40,0)");
        g.fillStyle = gl; g.fillRect(0, 0, w, h);
      }
      const animating = st === "running" || since < 1.8 || sparks.current.length > 0 || Math.abs((tp ?? p) - p) > 0.002;
      idleFrames = animating ? 0 : idleFrames + 1;
      if (idleFrames < 3) raf = requestAnimationFrame(step); else raf = 0;
    };
    raf = requestAnimationFrame(step);
    // wake the loop when props change
    const wake = () => { if (!raf) { last = performance.now(); raf = requestAnimationFrame(step); } };
    (c as unknown as { __wake?: () => void }).__wake = wake;
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, [height, compact]);
  useEffect(() => { (ref.current as unknown as { __wake?: () => void } | null)?.__wake?.(); }, [progress, state]);

  const label = state === "done" ? "sequenced" : state === "failed" ? "broken" : state === "cancelled" ? "stopped" : word;
  const pct = state === "running" && progress !== null && progress !== undefined ? `${Math.round(progress * 100)}%` : state === "done" ? "100%" : "";
  return (
    <div className={`strandbar ${state}${compact ? " compact" : ""}`} style={{ height }}>
      <canvas ref={ref} style={{ width: "100%", height }} aria-hidden="true" />
      {words && (
        <div className="strand-words">
          <span className={`strand-word ${state}`} key={label}>{label}</span>
          {note ? <span className="strand-note">· {note}</span> : null}
          {pct ? <span className="strand-pct">{pct}</span> : null}
        </div>
      )}
    </div>
  );
}
