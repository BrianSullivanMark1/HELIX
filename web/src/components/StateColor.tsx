// StateColor — one subscriber eases the same color the orb shows and writes it to --state-color
// (10Hz, only on meaningful change), so the whole HUD breathes with the star. Also fires the
// done/error wash: the shockwave leaves the sphere and crosses the interface.
import { useEffect, useRef } from "react";
import { HUE_LOOK, baseLook } from "./Orb";
import { useHelix } from "../lib/store";

export default function StateColor() {
  const washRef = useRef<HTMLDivElement>(null);
  const prevHue = useRef("none");

  useEffect(() => {
    const eased = [0.16, 0.55, 1.0];
    let last = [0, 0, 0];
    let lastWrite = 0;
    let raf = 0;

    const tick = (now: number) => {
      const s = useHelix.getState();
      const base = baseLook(s); // asleep, the whole HUD tints indigo with the star
      const target = s.hue !== "none" ? HUE_LOOK[s.hue] : base.color;
      for (let i = 0; i < 3; i++) eased[i] += (target[i] - eased[i]) * 0.06;

      if (now - lastWrite > 100) {
        lastWrite = now;
        const rgb = eased.map((v) => Math.round(v * 255));
        if (rgb.some((v, i) => Math.abs(v - last[i]) > 1)) {
          last = rgb;
          document.documentElement.style.setProperty(
            "--state-color", `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`);
        }
      }

      if (s.hue !== prevHue.current) {
        if ((s.hue === "done" || s.hue === "error") && washRef.current) {
          const el = washRef.current;
          el.style.background = s.hue === "done" ? "var(--done)" : "var(--error)";
          el.classList.remove("flash");
          void el.offsetWidth; // restart the animation
          el.classList.add("flash");
        }
        prevHue.current = s.hue;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <div ref={washRef} className="state-wash" />;
}
