// THE WORDMARK — HELIX's name in the top-left, alive: the helix mark (components/HelixMark.tsx,
// a canvas turning about its own axis, crisp), the word with a slow shimmer, a breathing dot.
// Louder while the radio plays (--beat rides in from the mark's frame loop).
import { useCallback, useRef } from "react";
import HelixMark from "./HelixMark";
import "./wordmark.css";

export default function Wordmark({ onClick }: { onClick: () => void }) {
  const ref = useRef<HTMLButtonElement | null>(null);
  const onBeat = useCallback((level: number, kick: boolean) => {
    const el = ref.current; if (!el) return;
    el.style.setProperty("--beat", level.toFixed(3));
    if (kick) { el.classList.remove("kick"); void el.offsetWidth; el.classList.add("kick"); }
  }, []);
  return (
    <button ref={ref} className="wordmark" onClick={onClick} data-tip="HELIX - the Console">
      <span className="wm-mark" aria-hidden="true"><HelixMark size={40} onBeat={onBeat} /></span>
      <span className="wm-word">HELIX</span>
      <span className="wm-dot" aria-hidden="true" />
    </button>
  );
}
