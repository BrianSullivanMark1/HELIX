// THE WORDMARK — HELIX's name in the top-left, alive: a double helix that turns with light running
// its strands, rungs that fire one after another, a spark orbiting the mark, the word in a
// gradient with a slow shimmer, and a breathing halo. Louder while the radio plays.
import { useEffect, useRef } from "react";
import { radioBeat } from "./Radio";
import "./wordmark.css";

export default function Wordmark({ onClick }: { onClick: () => void }) {
  const ref = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    let raf = 0;
    const el = ref.current;
    const step = () => {
      const b = radioBeat();
      if (el) { el.style.setProperty("--beat", b.level.toFixed(3)); if (b.kick) { el.classList.remove("kick"); void el.offsetWidth; el.classList.add("kick"); } }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <button ref={ref} className="wordmark" onClick={onClick} title="HELIX - the Console">
      <span className="wm-mark" aria-hidden="true">
        <svg viewBox="0 0 48 48" width="40" height="40">
          <defs>
            <linearGradient id="wmA" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#dffbff" /><stop offset="0.5" stopColor="#3fe0e0" /><stop offset="1" stopColor="#2a8cff" /></linearGradient>
            <linearGradient id="wmB" x1="1" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#ffffff" /><stop offset="1" stopColor="#3fe0e0" /></linearGradient>
            <radialGradient id="wmHalo"><stop offset="0" stopColor="#3fe0e0" stopOpacity="0.55" /><stop offset="1" stopColor="#3fe0e0" stopOpacity="0" /></radialGradient>
            <filter id="wmGlow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="1.6" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
          </defs>
          <circle className="wm-halo" cx="24" cy="24" r="22" fill="url(#wmHalo)" />
          <circle className="wm-ring" cx="24" cy="24" r="21" fill="none" stroke="url(#wmA)" strokeWidth="0.8" strokeDasharray="3 5" />
          <circle className="wm-ring2" cx="24" cy="24" r="17.5" fill="none" stroke="url(#wmB)" strokeWidth="0.6" strokeDasharray="1 7" opacity="0.7" />
          <g filter="url(#wmGlow)">
            <path className="wm-strand a" d="M15 7 C 33 14, 15 34, 33 41" fill="none" stroke="url(#wmA)" strokeWidth="2.8" strokeLinecap="round" />
            <path className="wm-strand b" d="M33 7 C 15 14, 33 34, 15 41" fill="none" stroke="url(#wmB)" strokeWidth="2.8" strokeLinecap="round" />
            <g className="wm-rungs" stroke="#dffbff" strokeWidth="1.5" strokeLinecap="round">
              {[11, 16.5, 22, 27.5, 33, 38].map((y, i) => {
                const k = Math.abs(Math.cos(((y - 7) / 34) * Math.PI)) * 8.5;
                return <line key={y} x1={24 - k} x2={24 + k} y1={y} y2={y} style={{ animationDelay: `${i * 0.18}s` }} />;
              })}
            </g>
          </g>
          <g className="wm-orbit"><circle cx="24" cy="3" r="1.6" fill="#ffffff" /><circle cx="24" cy="3" r="3.2" fill="#3fe0e0" opacity="0.35" /></g>
        </svg>
      </span>
      <span className="wm-word">HELIX</span>
      <span className="wm-dot" aria-hidden="true" />
    </button>
  );
}
