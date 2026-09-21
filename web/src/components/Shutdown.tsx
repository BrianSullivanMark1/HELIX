// POWER OFF (Brian, 2026-09-22): the menu's last item. One question first - Are you sure - with a
// word about any task still running; then the boot curtain in reverse: the helix unwinds into
// dust that flies outward, the ring opens, the word decodes back into noise, the lights go out.
// Only when the picture is dark does HELIX get the quit, so the closing is a performance, not a cut.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useTaskCounts } from "../lib/jobs";
import "./shutdown.css";

export function PowerAsk({ onClose }: { onClose: () => void }) {
  const counts = useTaskCounts();
  const [going, setGoing] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !going) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, going]);
  if (going) return <Shutdown />;
  return (
    <div className="power-wrap" onClick={onClose}>
      <div className="power-card" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Power off HELIX">
        <div className="power-ic" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="30" height="30"><path fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" d="M12 3v9" /><path fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" d="M6.3 6.5a8 8 0 1 0 11.4 0" /></svg>
        </div>
        <div className="power-title">Power off HELIX?</div>
        <div className="power-body">
          {counts.running > 0
            ? <>{counts.running} task{counts.running === 1 ? " is" : "s are"} still running - they will be cut off. Let them finish first if they matter.</>
            : <>Everything stops - the voice, the ears, the watchers. The desktop icon starts it again.</>}
        </div>
        <div className="power-acts">
          <button className="task-btn" onClick={onClose} autoFocus>Keep it on</button>
          <button className="task-btn stop solid" onClick={() => setGoing(true)}>Power off</button>
        </div>
      </div>
    </div>
  );
}

/** The closing curtain. Draws for ~2.6 s, then asks the backend to quit and raises helix-off. */
function Shutdown() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    let raf = 0, t = 0, last = performance.now(), w = 0, h = 0, done = false;
    const dpr = Math.min(1.5, window.devicePixelRatio || 1);
    const size = () => { w = window.innerWidth; h = window.innerHeight; c.width = w * dpr; c.height = h * dpr; };
    size(); window.addEventListener("resize", size);
    const N = 160;
    const WORD = "HELIX";
    // every helix point gets a flight: a direction outward and a speed, used once it lets go
    const fly = Array.from({ length: N * 2 }, () => ({ a: Math.random() * Math.PI * 2, v: 120 + Math.random() * 360, s: 0.6 + Math.random() * 0.8 }));
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      // the page dims underneath
      g.fillStyle = `rgba(6, 9, 13, ${Math.min(1, 0.35 + t * 0.9).toFixed(3)})`; g.fillRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const H = Math.min(h * 0.5, 420), R = Math.min(w, h) * 0.1, turns = 2.6, ringR = H / 2 + 46;
      const unwind = Math.min(1, Math.max(0, (t - 0.2) / 1.6));       // the helix lets go from the top down
      const ease = unwind * unwind;
      const dark = Math.min(1, Math.max(0, (t - 1.9) / 0.6));           // the last lights
      g.globalCompositeOperation = "lighter";
      const spin = t * 1.1 + ease * 3;
      for (let s = 0; s < 2; s++) {
        for (let i = 0; i < N; i++) {
          const k = i / (N - 1);
          const a = k * turns * Math.PI * 2 + s * Math.PI + spin;
          const hx = cx + Math.cos(a) * R, hy = cy - H / 2 + k * H, z = Math.sin(a);
          const front = (z + 1) / 2;
          const f = fly[s * N + i];
          const gone = Math.max(0, Math.min(1, (ease - k * 0.85) / 0.15));          // 0 = still on the strand, 1 = flown
          const x = hx + Math.cos(f.a) * f.v * gone * gone * 1.6, y = hy + Math.sin(f.a) * f.v * gone * gone * 1.6 + gone * gone * 80;
          const alpha = (0.35 + 0.65 * front) * (1 - gone) * (1 - dark) + gone * (1 - gone) * 0.6;
          if (alpha <= 0.01) continue;
          const hue = s === 0 ? 185 : 215;
          g.fillStyle = i % 23 === 0 ? `hsla(42, 95%, 65%, ${alpha})` : `hsla(${hue}, 90%, ${55 + front * 30}%, ${alpha})`;
          g.beginPath(); g.arc(x, y, (1.6 + front * 2.4) * f.s * (1 - gone * 0.5), 0, Math.PI * 2); g.fill();
          if (s === 0 && i % 8 === 0 && gone < 0.5) {
            const a2 = a + Math.PI; const x2 = cx + Math.cos(a2) * R;
            g.strokeStyle = `hsla(190, 90%, 70%, ${0.35 * (1 - gone * 2) * (1 - dark)})`; g.lineWidth = 1;
            g.beginPath(); g.moveTo(hx, hy); g.lineTo(x2, hy); g.stroke();
          }
        }
      }
      // the ring opens the other way, and a spark rides its tip out
      const prog = Math.max(0, 1 - Math.min(1, t / 1.8));
      g.strokeStyle = `hsla(190, 90%, 70%, ${0.9 * (1 - dark)})`; g.lineWidth = 2; g.lineCap = "round";
      g.beginPath(); g.arc(cx, cy, ringR, -Math.PI / 2, -Math.PI / 2 + prog * Math.PI * 2); g.stroke();
      const tipA = -Math.PI / 2 + prog * Math.PI * 2;
      g.fillStyle = `rgba(255,255,255,${1 - dark})`; g.beginPath(); g.arc(cx + Math.cos(tipA) * ringR, cy + Math.sin(tipA) * ringR, 3, 0, Math.PI * 2); g.fill();
      g.globalCompositeOperation = "source-over";
      // the word decodes into noise, right to left, then fades
      const noise = Math.min(1, Math.max(0, (t - 0.5) / 1.2));
      g.font = `800 ${Math.round(Math.min(w, h) * 0.075)}px ui-sans-serif, system-ui`; g.textAlign = "center"; g.textBaseline = "middle";
      let word = "";
      for (let i = 0; i < WORD.length; i++) word += (WORD.length - 1 - i) / WORD.length < noise ? "01#/\\|"[Math.floor(Math.random() * 6)] : WORD[i];
      g.letterSpacing = "10px";
      g.fillStyle = `hsla(190, 90%, 78%, ${(1 - dark) * (1 - noise * 0.5)})`; g.fillText(word, cx, cy + ringR + 54);
      g.font = `500 11px ui-sans-serif, system-ui`; g.letterSpacing = "4px"; g.fillStyle = `hsla(190, 40%, 70%, ${0.7 * (1 - dark)})`;
      g.fillText(t < 1.9 ? "POWERING DOWN" : "GOOD NIGHT", cx, cy + ringR + 86);
      if (t < 2.7) raf = requestAnimationFrame(step);
      else if (!done) {
        done = true;
        void api.post("/api/shell/quit").catch(() => undefined);
        window.setTimeout(() => window.dispatchEvent(new CustomEvent("helix-off", { detail: { reason: "You powered HELIX off from the menu." } })), 500);
      }
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", size); };
  }, []);
  return <canvas ref={ref} className="shutdown" aria-hidden="true" />;
}
