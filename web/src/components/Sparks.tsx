// SPARKS — typing burns the text in. Every keystroke in any text box throws a few embers from the
// caret that drift, fall and fade; a fixed canvas over the page, no pointer events, nothing when
// the tab is hidden or reduced motion is asked for. The caret's position comes from a mirror of
// the field (same font and padding, text up to the caret, a marker span at the end).
import { useEffect, useRef } from "react";

interface Ember { x: number; y: number; vx: number; vy: number; life: number; max: number; r: number; hue: number }

const REDUCED = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const MIRROR_PROPS = ["fontFamily", "fontSize", "fontWeight", "letterSpacing", "lineHeight", "textTransform", "paddingTop", "paddingLeft", "paddingRight", "paddingBottom", "borderLeftWidth", "borderTopWidth", "boxSizing", "whiteSpace", "wordWrap", "width"] as const;

function caretPoint(el: HTMLInputElement | HTMLTextAreaElement, mirror: HTMLDivElement): { x: number; y: number } {
  const cs = window.getComputedStyle(el);
  for (const p of MIRROR_PROPS) mirror.style[p] = cs[p];
  const isArea = el.tagName === "TEXTAREA";
  mirror.style.whiteSpace = isArea ? "pre-wrap" : "pre";
  mirror.style.wordWrap = isArea ? "break-word" : "normal";
  const pos = el.selectionStart ?? el.value.length;
  mirror.textContent = el.value.slice(0, pos);
  const mark = document.createElement("span");
  mark.textContent = el.value.slice(pos, pos + 1) || ".";
  mirror.appendChild(mark);
  const r = el.getBoundingClientRect();
  const x = r.left + mark.offsetLeft - el.scrollLeft;
  const y = r.top + mark.offsetTop - el.scrollTop + parseFloat(cs.fontSize) * 0.55;
  return { x: Math.min(r.right - 4, Math.max(r.left + 4, x)), y: Math.min(r.bottom - 4, Math.max(r.top + 4, y)) };
}

export default function Sparks() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || REDUCED) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const mirror = document.createElement("div");
    mirror.setAttribute("aria-hidden", "true");
    mirror.style.cssText = "position:fixed;top:0;left:-9999px;visibility:hidden;pointer-events:none;overflow:hidden;";
    document.body.appendChild(mirror);
    let embers: Ember[] = [];
    let raf = 0, last = 0, alive = true, w = 0, h = 0;
    const dpr = Math.min(1.25, window.devicePixelRatio || 1);
    const resize = () => { w = window.innerWidth; h = window.innerHeight; canvas.width = w * dpr; canvas.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); };
    resize();
    window.addEventListener("resize", resize);
    const hueOf = () => { const v = getComputedStyle(document.documentElement).getPropertyValue("--spark-hue").trim(); return v ? Number(v) : 190; };
    const burst = (x: number, y: number, n: number, strong: boolean) => {
      const hue = hueOf();
      for (let i = 0; i < n; i++) {
        const a = -Math.PI / 2 + (Math.random() - 0.5) * 2.2;
        const sp = (strong ? 90 : 60) + Math.random() * 90;
        embers.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 40, life: 0, max: 0.7 + Math.random() * 0.9, r: 1.2 + Math.random() * 2.2, hue: hue + (Math.random() - 0.5) * 50 });
      }
      if (embers.length > 400) embers = embers.slice(-400);
      if (!raf) { last = performance.now(); raf = requestAnimationFrame(step); }
    };
    const onInput = (e: Event) => {
      const el = e.target as HTMLElement | null;
      if (!el || !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return;
      if (el instanceof HTMLInputElement && el.type !== "text" && el.type !== "search" && el.type !== "url" && el.type !== "password") return;
      const ie = e as InputEvent;
      const strong = ie.inputType === "insertParagraph" || ie.inputType === "insertFromPaste";
      const { x, y } = caretPoint(el, mirror);
      burst(x, y, strong ? 26 : 7 + Math.floor(Math.random() * 5), strong);
    };
    const step = (now: number) => {
      if (!alive) return;
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = "lighter";
      for (const p of embers) {
        p.life += dt;
        p.vy += 260 * dt;                    // gravity: they fall
        p.vx *= Math.pow(0.35, dt);          // and slow sideways
        p.x += p.vx * dt; p.y += p.vy * dt;
        const k = 1 - p.life / p.max;
        if (k <= 0) continue;
        const flick = 0.7 + 0.3 * Math.sin(now * 0.03 + p.x);
        ctx.shadowBlur = 8; ctx.shadowColor = `hsla(${p.hue}, 100%, 70%, ${k})`;
        ctx.fillStyle = `hsla(${p.hue}, 95%, ${60 + 30 * k}%, ${k * flick})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r * (0.6 + 0.6 * k), 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
        if (k > 0.6) { ctx.fillStyle = `hsla(${p.hue}, 100%, 90%, ${(k - 0.6) * 0.8})`; ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 0.5, 0, Math.PI * 2); ctx.fill(); }
      }
      ctx.globalCompositeOperation = "source-over";
      embers = embers.filter((p) => p.life < p.max && p.y < h + 10);
      if (embers.length) raf = requestAnimationFrame(step);
      else { raf = 0; ctx.clearRect(0, 0, w, h); }
    };
    document.addEventListener("input", onInput, true);
    return () => {
      alive = false; if (raf) cancelAnimationFrame(raf);
      document.removeEventListener("input", onInput, true);
      window.removeEventListener("resize", resize);
      mirror.remove();
    };
  }, []);
  return <canvas ref={ref} className="fixed inset-0" style={{ zIndex: 60, pointerEvents: "none" }} aria-hidden="true" />;
}
