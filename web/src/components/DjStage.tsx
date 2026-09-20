// THE DJ — the radio's stage while no video plays: a mini HELIX head in a headset, jamming in a
// neural net that pulses to the song. The room takes the track's theme (chill = cool blues, hype =
// fire, dark = violet, happy = gold, focus = ice, epic = magenta), the head bobs on the kicks,
// eyes squint on the beat, the mouth vibes to the level, the headset's LEDs run with the bands and
// the equalizer breathes behind. Pure canvas 2D: no WebGL context spent on it.
import { useEffect, useRef } from "react";
import { radioBeat } from "./Radio";

const THEME: Record<string, { h: number; h2: number; name: string }> = {
  chill: { h: 190, h2: 230, name: "chill" }, hype: { h: 14, h2: 44, name: "hype" }, dark: { h: 272, h2: 310, name: "dark" },
  happy: { h: 42, h2: 70, name: "happy" }, focus: { h: 200, h2: 214, name: "focus" }, epic: { h: 325, h2: 355, name: "epic" }, "": { h: 185, h2: 225, name: "" },
};

export default function DjStage({ playing, theme }: { playing: boolean; theme: string }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    let raf = 0, t = 0, last = performance.now(), bob = 0, bobV = 0, squint = 0, mouth = 0, nodT = 0;
    const pal = THEME[theme] || THEME[""];
    // the net: nodes that drift and link, lit by the beat
    type N = { x: number; y: number; vx: number; vy: number; f: number };
    let nodes: N[] = [];
    let w = 0, h = 0;
    const seed = () => {
      nodes = Array.from({ length: 46 }, () => ({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - 0.5) * 14, vy: (Math.random() - 0.5) * 10, f: 0 }));
    };
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      const cw = c.clientWidth, ch = c.clientHeight;
      if (c.width !== cw * 2 || c.height !== ch * 2) { c.width = cw * 2; c.height = ch * 2; w = cw; h = ch; seed(); }
      g.setTransform(2, 0, 0, 2, 0, 0);
      const { level, kick, bins } = radioBeat();
      const lvl = playing ? level : 0.05 + 0.04 * Math.sin(t * 2);
      // physics of the head: a kick throws it up, gravity brings it down, the shoulders follow
      if (kick) { bobV = -160; nodT = 1; }
      bobV += 900 * dt; bob += bobV * dt; if (bob > 0) { bob = 0; bobV *= -0.25; }
      nodT *= Math.pow(0.05, dt);
      squint += ((kick ? 1 : lvl * 0.8) - squint) * (kick ? 1 : 0.15);
      mouth += ((0.15 + lvl * 0.9) - mouth) * 0.35;
      // the room
      const grd = g.createRadialGradient(w / 2, h * 0.55, 0, w / 2, h * 0.55, Math.max(w, h) * 0.75);
      grd.addColorStop(0, `hsla(${pal.h}, 70%, ${14 + lvl * 18}%, 1)`); grd.addColorStop(1, "hsl(210, 30%, 3%)");
      g.fillStyle = grd; g.fillRect(0, 0, w, h);
      // the equalizer, behind everything, soft
      if (bins) {
        const n = 40, bw = w / n;
        for (let i = 0; i < n; i++) {
          const v = bins[Math.floor(i * (bins.length * 0.5) / n)] / 255;
          const bh = v * h * 0.55;
          g.fillStyle = `hsla(${pal.h + i * 2}, 90%, 60%, ${0.10 + v * 0.22})`;
          g.fillRect(i * bw + 1, h - bh, bw - 2, bh);
        }
      }
      // the net
      for (const n of nodes) {
        n.x += n.vx * dt * (1 + lvl * 2); n.y += n.vy * dt * (1 + lvl * 2);
        if (n.x < 0 || n.x > w) n.vx *= -1; if (n.y < 0 || n.y > h) n.vy *= -1;
        n.f = Math.max(n.f * Math.pow(0.08, dt), kick && Math.random() < 0.35 ? 1 : 0);
      }
      g.lineWidth = 1;
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d < 110) {
          const k = 1 - d / 110;
          g.strokeStyle = `hsla(${pal.h2}, 90%, 70%, ${(0.08 + 0.35 * Math.max(a.f, b.f)) * k + lvl * 0.15 * k})`;
          g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
        }
      }
      for (const n of nodes) {
        g.fillStyle = `hsla(${pal.h2}, 95%, ${65 + n.f * 30}%, ${0.5 + n.f * 0.5})`;
        g.beginPath(); g.arc(n.x, n.y, 1.6 + n.f * 2.5 + lvl, 0, Math.PI * 2); g.fill();
      }
      // THE HEAD
      const cx = w / 2, cy = h * 0.54 + bob * 0.35, R = Math.min(w, h) * 0.27;
      const tilt = Math.sin(t * 2.6) * 0.06 * (0.3 + lvl) + nodT * 0.12;
      g.save(); g.translate(cx, cy); g.rotate(tilt);
      // shoulders
      g.fillStyle = `hsla(${pal.h}, 40%, 12%, 0.9)`;
      g.beginPath(); g.ellipse(0, R * 1.55, R * 1.9, R * 0.6, 0, Math.PI, Math.PI * 2); g.fill();
      g.strokeStyle = `hsla(${pal.h2}, 80%, 60%, 0.5)`; g.lineWidth = 1.5; g.stroke();
      // the glow behind the head
      const halo = g.createRadialGradient(0, 0, R * 0.6, 0, 0, R * 1.9);
      halo.addColorStop(0, `hsla(${pal.h2}, 90%, 60%, ${0.25 + lvl * 0.35})`); halo.addColorStop(1, "transparent");
      g.fillStyle = halo; g.beginPath(); g.arc(0, 0, R * 1.9, 0, Math.PI * 2); g.fill();
      // the skull: a sphere of code
      const skin = g.createRadialGradient(-R * 0.3, -R * 0.35, R * 0.1, 0, 0, R);
      skin.addColorStop(0, `hsl(${pal.h}, 55%, 34%)`); skin.addColorStop(1, `hsl(${pal.h + 20}, 50%, 10%)`);
      g.fillStyle = skin; g.beginPath(); g.arc(0, 0, R, 0, Math.PI * 2); g.fill();
      g.strokeStyle = `hsla(${pal.h2}, 90%, 70%, 0.8)`; g.lineWidth = 1.5; g.stroke();
      // code on the skull
      g.save(); g.beginPath(); g.arc(0, 0, R, 0, Math.PI * 2); g.clip();
      g.font = `${Math.max(7, R * 0.14)}px ui-monospace, Menlo, Consolas, monospace`;
      g.fillStyle = `hsla(${pal.h2}, 90%, 80%, 0.28)`;
      for (let i = 0; i < 26; i++) { const ang = i * 0.48 + t * 0.15, rr = R * (0.45 + ((i * 7919) % 50) / 100); g.fillText(((i + Math.floor(t * 3)) % 2).toString(), Math.cos(ang) * rr, Math.sin(ang) * rr); }
      g.restore();
      // the visor / brows
      const browY = -R * 0.36 - (kick ? R * 0.05 : 0) - lvl * R * 0.04;
      g.strokeStyle = `hsl(${pal.h2}, 95%, 75%)`; g.lineWidth = Math.max(2, R * 0.07); g.lineCap = "round";
      g.beginPath(); g.moveTo(-R * 0.55, browY + R * 0.04); g.quadraticCurveTo(-R * 0.3, browY - R * 0.06, -R * 0.1, browY + R * 0.02); g.stroke();
      g.beginPath(); g.moveTo(R * 0.55, browY + R * 0.04); g.quadraticCurveTo(R * 0.3, browY - R * 0.06, R * 0.1, browY + R * 0.02); g.stroke();
      // eyes: squint on the beat, look around with the music
      const lookX = Math.sin(t * 0.9) * R * 0.05, eyeH = R * 0.14 * (1 - squint * 0.75);
      for (const sx of [-1, 1]) {
        const ex = sx * R * 0.32 + lookX, ey = -R * 0.12;
        g.fillStyle = "#05080b"; g.beginPath(); g.ellipse(ex, ey, R * 0.2, Math.max(R * 0.02, eyeH), 0, 0, Math.PI * 2); g.fill();
        g.strokeStyle = `hsl(${pal.h2}, 95%, 75%)`; g.lineWidth = 1.5; g.stroke();
        if (eyeH > R * 0.05) {
          const ir = g.createRadialGradient(ex + lookX * 0.4, ey, 0, ex + lookX * 0.4, ey, R * 0.1);
          ir.addColorStop(0, "#fff"); ir.addColorStop(0.3, `hsl(${pal.h2}, 95%, 70%)`); ir.addColorStop(1, `hsl(${pal.h}, 90%, 35%)`);
          g.fillStyle = ir; g.beginPath(); g.arc(ex + lookX * 0.4, ey, Math.min(eyeH * 0.9, R * 0.1), 0, Math.PI * 2); g.fill();
          g.fillStyle = "#000"; g.beginPath(); g.arc(ex + lookX * 0.4, ey, R * 0.04, 0, Math.PI * 2); g.fill();
        }
      }
      // the mouth: a grin that opens with the level
      const my = R * 0.36, mw = R * 0.45, open = mouth * R * 0.22;
      g.fillStyle = "#04070a"; g.beginPath(); g.moveTo(-mw, my); g.quadraticCurveTo(0, my + R * 0.16 + open, mw, my); g.quadraticCurveTo(0, my + R * 0.08, -mw, my); g.fill();
      g.strokeStyle = `hsl(${pal.h2}, 95%, 75%)`; g.lineWidth = Math.max(2, R * 0.05); g.beginPath(); g.moveTo(-mw, my); g.quadraticCurveTo(0, my + R * 0.16 + open, mw, my); g.stroke();
      if (open > R * 0.06) { g.fillStyle = "rgba(255,255,255,0.75)"; g.fillRect(-mw * 0.6, my + R * 0.02, mw * 1.2, R * 0.045); }
      // THE HEADSET: band over the top, cups on the sides, LEDs running the bands
      g.strokeStyle = `hsl(${pal.h}, 22%, 26%)`; g.lineWidth = Math.max(5, R * 0.14); g.lineCap = "round";
      g.beginPath(); g.arc(0, 0, R * 1.05, Math.PI * 1.15, Math.PI * 1.85); g.stroke();
      g.strokeStyle = `hsla(${pal.h2}, 90%, 70%, 0.6)`; g.lineWidth = 1.5;
      g.beginPath(); g.arc(0, 0, R * 1.05, Math.PI * 1.15, Math.PI * 1.85); g.stroke();
      for (const sx of [-1, 1]) {
        const hx = sx * R * 1.02, hy = R * 0.05;
        g.fillStyle = `hsl(${pal.h}, 25%, 14%)`; g.beginPath(); g.ellipse(hx, hy, R * 0.22, R * 0.34, 0, 0, Math.PI * 2); g.fill();
        g.strokeStyle = `hsl(${pal.h2}, 90%, 65%)`; g.lineWidth = 2; g.stroke();
        // three LEDs per cup
        for (let k = 0; k < 3; k++) {
          const v = bins ? bins[6 + k * 9] / 255 : 0.2;
          g.fillStyle = `hsla(${pal.h2 + k * 20}, 100%, ${60 + v * 35}%, ${0.35 + v * 0.65})`;
          g.beginPath(); g.arc(hx, hy - R * 0.18 + k * R * 0.18, R * 0.05 + v * R * 0.03, 0, Math.PI * 2); g.fill();
        }
      }
      // the mic boom on the right cup, with a lit tip
      g.strokeStyle = `hsl(${pal.h}, 20%, 30%)`; g.lineWidth = 2.5;
      g.beginPath(); g.moveTo(R * 1.02, R * 0.34); g.quadraticCurveTo(R * 0.8, R * 0.75, R * 0.35, R * 0.62); g.stroke();
      g.fillStyle = `hsl(${pal.h2}, 100%, ${70 + lvl * 25}%)`; g.beginPath(); g.arc(R * 0.35, R * 0.62, R * 0.06 + lvl * R * 0.02, 0, Math.PI * 2); g.fill();
      g.restore();
      // the caption
      g.font = "600 11px ui-sans-serif, system-ui"; g.fillStyle = `hsla(${pal.h2}, 90%, 80%, 0.7)`; g.textAlign = "right";
      g.fillText((pal.name ? pal.name.toUpperCase() + " ROOM · " : "") + (playing ? "ON AIR" : "STANDING BY"), w - 12, 18);
      g.textAlign = "left";
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, theme]);
  return <canvas ref={ref} className="radio-viz" />;
}
