// THE TIPS - one tooltip for the whole app, and it is HELIX telling you: a small head that turns
// to look at the thing you are pointing at, a glowing hand pointing at it too, and the words in a
// glass bubble. Any element with data-tip="..." gets it; every native title="..." is taken over
// (the browser's grey box never shows again). Mounted once, in App.
//
// It waits 380 ms so a sweep across the page does not flicker, follows the element (below it, or
// above when there is no room), and leaves on mouseout, click, scroll or Escape.
import { useEffect, useRef, useState } from "react";
import "./tip.css";

interface Shown { text: string; x: number; y: number; above: boolean; look: number; handX: number }

function tipOf(el: Element | null): { el: HTMLElement; text: string } | null {
  let cur: Element | null = el;
  while (cur && cur !== document.body) {
    const h = cur as HTMLElement;
    if (h.dataset && h.dataset.tip) return { el: h, text: h.dataset.tip };
    if (h.hasAttribute("title")) {
      const t = h.getAttribute("title") || "";
      h.removeAttribute("title");            // the browser's own box never shows
      if (t.trim()) { h.dataset.tip = t; return { el: h, text: t }; }
    }
    cur = cur.parentElement;
  }
  return null;
}

export default function TipLayer() {
  const [shown, setShown] = useState<Shown | null>(null);
  const timer = useRef(0);
  const current = useRef<HTMLElement | null>(null);
  const bubble = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const place = (el: HTMLElement, text: string) => {
      const r = el.getBoundingClientRect();
      const W = Math.min(340, Math.max(160, text.length * 6.6 + 84)), H = 58;
      const above = r.bottom + H + 18 > window.innerHeight;
      let x = r.left + r.width / 2 - W / 2;
      x = Math.max(8, Math.min(window.innerWidth - W - 8, x));
      const y = above ? r.top - H - 14 : r.bottom + 14;
      const targetX = r.left + r.width / 2;
      const look = Math.max(-1, Math.min(1, (targetX - (x + 26)) / 220));
      const handX = Math.max(44, Math.min(W - 20, targetX - x));
      setShown({ text, x, y, above, look, handX });
    };
    const hide = () => { window.clearTimeout(timer.current); current.current = null; setShown(null); };
    const over = (e: MouseEvent) => {
      const hit = tipOf(e.target as Element);
      if (!hit) { if (current.current && !current.current.contains(e.target as Node)) hide(); return; }
      if (hit.el === current.current) return;
      window.clearTimeout(timer.current);
      current.current = hit.el;
      setShown(null);
      timer.current = window.setTimeout(() => { if (current.current === hit.el && document.contains(hit.el)) place(hit.el, hit.el.dataset.tip || hit.text); }, 380);
    };
    const out = (e: MouseEvent) => {
      const to = e.relatedTarget as Node | null;
      if (current.current && (!to || !current.current.contains(to))) hide();
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    document.addEventListener("mouseover", over);
    document.addEventListener("mouseout", out);
    document.addEventListener("mousedown", hide, true);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("keydown", key);
    window.addEventListener("blur", hide);
    return () => {
      document.removeEventListener("mouseover", over); document.removeEventListener("mouseout", out);
      document.removeEventListener("mousedown", hide, true); document.removeEventListener("scroll", hide, true);
      window.removeEventListener("keydown", key); window.removeEventListener("blur", hide);
    };
  }, []);
  if (!shown) return null;
  const { text, x, y, above, look, handX } = shown;
  return (
    <div ref={bubble} className={`helix-tip${above ? " above" : ""}`} style={{ left: x, top: y }} role="tooltip" aria-hidden="true">
      <span className="tip-hand" style={{ left: handX, transform: `translateX(-50%) ${above ? "scaleY(-1)" : ""}` }}>
        <svg viewBox="0 0 24 30" width="18" height="22"><path d="M10 29V17.5c-1.6 0-2.6-.7-3.6-2.2L3.2 10.5c-.5-.8-.2-1.8.6-2.2.7-.4 1.6-.2 2.1.5L8.5 12V3.2A1.8 1.8 0 0 1 12.1 3v7.5h1V4.6a1.6 1.6 0 0 1 3.2 0v6h1V6.3a1.5 1.5 0 0 1 3 0v5.3h.9V8.8a1.4 1.4 0 0 1 2.8 0V19c0 4.5-3 7-6.5 7.4V29" fill="rgba(10,18,24,.9)" stroke="#8ff5ff" strokeWidth="1.3" strokeLinejoin="round" /></svg>
      </span>
      <span className="tip-face" aria-hidden="true">
        <svg viewBox="0 0 40 40" width="34" height="34">
          <circle cx="20" cy="20" r="16" fill="url(#tipSkin)" stroke="#5fe9ee" strokeWidth="1.3" />
          <defs><radialGradient id="tipSkin" cx="35%" cy="30%" r="80%"><stop offset="0" stopColor="#1c4a52" /><stop offset="1" stopColor="#081218" /></radialGradient></defs>
          <path d="M11 13 q4 -3 8 -1" fill="none" stroke="#bffbff" strokeWidth="1.6" strokeLinecap="round" />
          <path d="M29 13 q-4 -3 -8 -1" fill="none" stroke="#bffbff" strokeWidth="1.6" strokeLinecap="round" />
          <ellipse cx="14" cy="19" rx="4" ry="2.6" fill="#04070a" stroke="#8ff5ff" strokeWidth=".8" />
          <ellipse cx="26" cy="19" rx="4" ry="2.6" fill="#04070a" stroke="#8ff5ff" strokeWidth=".8" />
          <circle cx={14 + look * 1.8} cy="19" r="1.5" fill="#9ff7ff" />
          <circle cx={26 + look * 1.8} cy="19" r="1.5" fill="#9ff7ff" />
          <path d="M14 27 q6 4 12 0" fill="none" stroke="#bffbff" strokeWidth="1.6" strokeLinecap="round" />
          {[0, 1, 2, 3, 4].map((i) => <circle key={i} className="tip-node" cx={20 + 19 * Math.cos((i / 5) * Math.PI * 2)} cy={20 + 19 * Math.sin((i / 5) * Math.PI * 2)} r="1.2" fill="#3fe0e0" style={{ animationDelay: `${i * 0.25}s` }} />)}
        </svg>
      </span>
      <span className="tip-text">{text}</span>
    </div>
  );
}
