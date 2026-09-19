// THE BOARD — the company's apps and their environments, as HELIX last read them
// (HELIX_MARK1_PLAN.md §17: THE FORGE's cards + search, one card per app, one row per
// environment). Phase 1: reads only. Every value on this page came from Cloud Run, the app's own
// /api/health, or the repo; nothing is inferred, and a cell HELIX could not read says "unknown"
// rather than anything more comfortable.
//
// Plain names on purpose (2026-09-17): company / app / environment. The evolution-and-neural-
// network theme lives in the art: a double helix turning behind the board with signals running
// its strands (Three.js, like the orb), a neural net that fires across the page (canvas), HUD
// frames on the cards, and the lineage thread that joins DEV -> QA -> PROD.
import { Canvas, useFrame } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import "./board.css";

/** One deployed half of a cell (domain/fleet.py Serving, through api/fleet_routes.py). */
interface Serving {
  revision: string | null;
  commit: string | null;
  dirty: boolean;
  deployed_at: string | null;
  deployed_by: string | null;
  health: "ok" | "degraded" | "down" | "absent" | "unknown";
  traffic_percent: number | null;
  is_split: boolean;
  db: string | null;
  read_only: boolean | null;
  flags: Record<string, boolean>;
}

/** One environment row (domain/fleet.py Cell). */
interface Row {
  key: string;
  company: string;
  app: string;
  env: "dev" | "qa" | "prod";
  exists: boolean;
  run_service: string | null;
  hosting_site: string | null;
  repo: string;
  branch: string;
  health: Serving["health"];
  drift: "clean" | "behind" | "ahead" | "diverged" | "rolled_back" | "unknown";
  behind_by: number | null;
  repo_commit: string | null;
  checked_at: string | null;
  note: string | null;
  needs_attention: boolean;
  api: Serving | null;
  site: Serving | null;
}

interface Card { app: string; repo: string | null; envs: Row[]; needs_attention: boolean }

interface Company {
  id: string;
  label: string;
  gcp_project: string;
  region: string;
  checked_at: string | null;
  apps: Card[];
}

interface Readiness { what: string; ok: boolean; why: string | null }

interface Board { profile: string | null; readiness: Readiness[]; companies: Company[] }

const MUTED = { color: "var(--muted)" } as const;

const HEALTH_COLOR: Record<Serving["health"], string> = {
  ok: "var(--done)",
  degraded: "var(--working)",
  down: "var(--error)",
  absent: "var(--muted)",
  unknown: "var(--muted)",
};

const DRIFT_LABEL: Record<Row["drift"], string> = {
  clean: "up to date",
  behind: "behind",
  ahead: "ahead of the repo",
  diverged: "diverged",
  rolled_back: "rolled back",
  unknown: "drift unknown",
};

const DRIFT_COLOR: Record<Row["drift"], string> = {
  clean: "var(--done)",
  behind: "var(--cyan)",
  ahead: "var(--working)",
  diverged: "var(--error)",
  rolled_back: "var(--amber)",
  unknown: "var(--muted)",
};

const REDUCED = typeof window !== "undefined"
  && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

function ago(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

function when(iso: string | null): string {
  if (!iso) return "-";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// ================================================================== the art: the helix

/** A soft round glow for a point sprite (the orb's halo trick). */
function glowTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.55)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const HELIX = { points: 220, turns: 3.2, radius: 1.75, height: 15, rungEvery: 6 };
const SIGNALS = 12;
const TAIL = 9;

/** Where strand `s` is at parameter t in [0,1], in the helix's own space. */
function strandAt(t: number, s: 0 | 1, out: THREE.Vector3): THREE.Vector3 {
  const a = t * HELIX.turns * Math.PI * 2 + s * Math.PI;
  return out.set(Math.cos(a) * HELIX.radius, (t - 0.5) * HELIX.height, Math.sin(a) * HELIX.radius);
}

/** The double helix: two strands of glowing points, rungs between them, signals travelling the
 *  strands, and a drift of dust for depth. Turns slowly; leans with the cursor. */
function HelixScene({ pointer }: { pointer: React.MutableRefObject<{ x: number; y: number }> }) {
  const group = useRef<THREE.Group>(null!);
  const dust = useRef<THREE.Points>(null!);
  const signals = useRef<THREE.Points>(null!);
  const tex = useMemo(glowTexture, []);
  const tmp = useMemo(() => new THREE.Vector3(), []);

  const { strands, rungs } = useMemo(() => {
    const n = HELIX.points;
    const pos = new Float32Array(n * 2 * 3);
    const col = new Float32Array(n * 2 * 3);
    const cyan = new THREE.Color("#3fe0e0");
    const blue = new THREE.Color("#2a8cff");
    const gold = new THREE.Color("#ffc857");
    const v = new THREE.Vector3();
    for (let s = 0; s < 2; s++) {
      for (let i = 0; i < n; i++) {
        strandAt(i / (n - 1), s as 0 | 1, v);
        const k = (s * n + i) * 3;
        pos[k] = v.x; pos[k + 1] = v.y; pos[k + 2] = v.z;
        const c = i % 23 === 0 ? gold : s === 0 ? cyan : blue;
        col[k] = c.r; col[k + 1] = c.g; col[k + 2] = c.b;
      }
    }
    const sg = new THREE.BufferGeometry();
    sg.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    sg.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const rp: number[] = [];
    const a = new THREE.Vector3(), b = new THREE.Vector3();
    for (let i = 0; i < n; i += HELIX.rungEvery) {
      strandAt(i / (n - 1), 0, a); strandAt(i / (n - 1), 1, b);
      rp.push(a.x, a.y, a.z, b.x, b.y, b.z);
    }
    const rg = new THREE.BufferGeometry();
    rg.setAttribute("position", new THREE.BufferAttribute(new Float32Array(rp), 3));
    rg.setAttribute("color", new THREE.BufferAttribute(new Float32Array(rp.length), 3));
    return { strands: sg, rungs: rg };
  }, []);
  const rungRef = useRef<THREE.LineSegments>(null!);
  const twin = useRef<THREE.Group>(null!);
  const clock = useRef(0);

  const dustGeo = useMemo(() => {
    const n = 700;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 30;
      pos[i * 3 + 1] = (Math.random() - 0.5) * 20;
      pos[i * 3 + 2] = (Math.random() - 0.5) * 16 - 2;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return g;
  }, []);

  // signals: each has a strand, a parameter, a speed; TAIL points trail each head
  const sig = useMemo(() => Array.from({ length: SIGNALS }, (_, i) => ({
    s: (i % 2) as 0 | 1, t: Math.random(), v: 0.05 + Math.random() * 0.06,
  })), []);
  const sigGeo = useMemo(() => {
    const n = SIGNALS * TAIL;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    const col = new Float32Array(n * 3);
    for (let i = 0; i < SIGNALS; i++) {
      for (let j = 0; j < TAIL; j++) {
        const k = (i * TAIL + j) * 3;
        const f = 1 - j / TAIL;               // the head is white-hot, the tail fades to cyan
        col[k] = 0.25 + 0.75 * f; col[k + 1] = 0.9 * f + 0.1; col[k + 2] = 0.9 * f + 0.1;
      }
    }
    g.setAttribute("color", new THREE.BufferAttribute(col, 3));
    return g;
  }, []);

  useFrame((_, dtRaw) => {
    const dt = Math.min(0.05, dtRaw);
    clock.current += dt;
    const t = clock.current;
    const g = group.current;
    g.rotation.y += dt * 0.12;
    // a slow breath, and a sway
    const breath = 1 + 0.025 * Math.sin(t * 0.6);
    g.scale.setScalar(breath);
    g.position.y = 0.2 + 0.25 * Math.sin(t * 0.23);
    // the distant twin turns the other way, slower
    if (twin.current) { twin.current.rotation.y -= dt * 0.07; twin.current.rotation.z = -0.5 + 0.03 * Math.sin(t * 0.3); }
    // a wave of light climbs the rungs - the rung colors are rewritten every frame (74 rungs, cheap)
    const rc = rungRef.current?.geometry.getAttribute("color") as THREE.BufferAttribute | undefined;
    const rpos = rungRef.current?.geometry.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (rc && rpos) {
      for (let i = 0; i < rc.count; i++) {
        const y = rpos.getY(i);
        const w = Math.max(0, Math.sin(y * 0.9 - t * 1.6));
        const k = 0.12 + 0.95 * w * w * w * w;
        rc.setXYZ(i, 0.25 * k + 0.02, 0.88 * k + 0.05, 0.88 * k + 0.06);
      }
      rc.needsUpdate = true;
    }
    // lean with the cursor, gently
    const px = pointer.current.x, py = pointer.current.y;
    g.rotation.z += ((0.42 + px * 0.06) - g.rotation.z) * 0.04;
    g.rotation.x += ((py * 0.08) - g.rotation.x) * 0.04;
    dust.current.rotation.y += dt * 0.015;
    dust.current.rotation.x = py * 0.05;
    // the signals run the strands
    const attr = signals.current.geometry.getAttribute("position") as THREE.BufferAttribute;
    for (let i = 0; i < SIGNALS; i++) {
      const s = sig[i];
      s.t += s.v * dt;
      if (s.t > 1.02) { s.t = -0.02; s.s = (Math.random() < 0.5 ? 0 : 1); }
      for (let j = 0; j < TAIL; j++) {
        strandAt(Math.max(0, Math.min(1, s.t - j * 0.006)), s.s, tmp);
        attr.setXYZ(i * TAIL + j, tmp.x, tmp.y, tmp.z);
      }
    }
    attr.needsUpdate = true;
  });

  return (
    <>
      <group ref={group} position={[2.6, 0.2, -1.5]} rotation={[0, 0, 0.42]}>
        <points geometry={strands}>
          <pointsMaterial map={tex} size={0.26} vertexColors transparent depthWrite={false}
            blending={THREE.AdditiveBlending} sizeAttenuation opacity={1} />
        </points>
        {/* the bloom: the same strand points, drawn huge and faint, so every node wears a halo */}
        <points geometry={strands}>
          <pointsMaterial map={tex} size={0.95} vertexColors transparent depthWrite={false}
            blending={THREE.AdditiveBlending} sizeAttenuation opacity={0.16} />
        </points>
        <lineSegments ref={rungRef} geometry={rungs}>
          <lineBasicMaterial vertexColors transparent opacity={0.9} blending={THREE.AdditiveBlending} depthWrite={false} />
        </lineSegments>
        <points ref={signals} geometry={sigGeo}>
          <pointsMaterial map={tex} size={0.5} vertexColors transparent depthWrite={false}
            blending={THREE.AdditiveBlending} sizeAttenuation />
        </points>
      </group>
      {/* the twin: the same helix far behind and to the left, dim, turning the other way - depth */}
      <group ref={twin} position={[-6.5, 1.5, -9]} rotation={[0, 0, -0.5]} scale={[1.6, 1.6, 1.6]}>
        <points geometry={strands}>
          <pointsMaterial map={tex} size={0.22} vertexColors transparent depthWrite={false}
            blending={THREE.AdditiveBlending} sizeAttenuation opacity={0.35} />
        </points>
        <lineSegments geometry={rungs}>
          <lineBasicMaterial color="#2a8cff" transparent opacity={0.12} blending={THREE.AdditiveBlending} depthWrite={false} />
        </lineSegments>
      </group>
      <points ref={dust} geometry={dustGeo}>
        <pointsMaterial map={tex} color="#3fe0e0" size={0.09} transparent opacity={0.5}
          depthWrite={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
    </>
  );
}

function HelixLayer() {
  const pointer = useRef({ x: 0, y: 0 });
  useEffect(() => {
    const move = (e: MouseEvent) => {
      pointer.current.x = (e.clientX / window.innerWidth) * 2 - 1;
      pointer.current.y = -((e.clientY / window.innerHeight) * 2 - 1);
    };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, []);
  if (REDUCED) return null;
  return (
    <div className="board-layer board-helix">
      <Canvas dpr={[1, 1.5]} camera={{ fov: 48, position: [0, 0, 9.5] }} gl={{ antialias: true, alpha: true }}
        onCreated={({ scene }) => { scene.fog = new THREE.Fog("#080b0f", 10, 24); }}>
        <HelixScene pointer={pointer} />
      </Canvas>
    </div>
  );
}

// ================================================================== the art: the neural net

/**
 * THE NEURAL NET - nodes drifting across the page; when two pass close a link forms, and signals
 * fire along the links node to node, lighting each node they reach and sometimes chaining on.
 * Pure canvas, no data, no pointer events; paused when the tab is hidden.
 */
function NeuralLayer() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || REDUCED) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const N = 70;
    const LINK = 165;
    type Node = { x: number; y: number; vx: number; vy: number; r: number; p: number; flash: number };
    type Signal = { a: number; b: number; t: number; v: number; hops: number };
    let nodes: Node[] = [];
    let signals: Signal[] = [];
    let w = 0, h = 0, raf = 0, last = 0, alive = true, spawnIn = 0.3;

    const seed = () => {
      nodes = Array.from({ length: N }, () => ({
        x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - 0.5) * 14, vy: (Math.random() - 0.5) * 14,
        r: 1.2 + Math.random() * 2.0, p: Math.random() * Math.PI * 2, flash: 0,
      }));
    };
    const size = () => {
      const parent = canvas.parentElement;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = parent ? parent.clientWidth : window.innerWidth;
      h = parent ? parent.clientHeight : window.innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (nodes.length === 0) seed();
    };
    const neighbours = (i: number): number[] => {
      const out: number[] = [];
      const a = nodes[i];
      for (let j = 0; j < nodes.length; j++) {
        if (j === i) continue;
        const dx = a.x - nodes[j].x, dy = a.y - nodes[j].y;
        if (dx * dx + dy * dy < LINK * LINK) out.push(j);
      }
      return out;
    };
    const fire = (from: number, hops: number) => {
      const ns = neighbours(from);
      if (!ns.length) return;
      const to = ns[Math.floor(Math.random() * ns.length)];
      signals.push({ a: from, b: to, t: 0, v: 1.6 + Math.random() * 1.2, hops });
    };
    const step = (t: number) => {
      if (!alive) return;
      const dt = Math.min(0.05, (t - last) / 1000 || 0);
      last = t;
      ctx.clearRect(0, 0, w, h);
      for (const n of nodes) {
        n.x += n.vx * dt; n.y += n.vy * dt; n.p += dt * 0.8;
        n.flash = Math.max(0, n.flash - dt * 1.6);
        if (n.x < -10) n.x = w + 10; else if (n.x > w + 10) n.x = -10;
        if (n.y < -10) n.y = h + 10; else if (n.y > h + 10) n.y = -10;
      }
      // links
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          const dx = a.x - b.x, dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK * LINK) continue;
          const k = 1 - Math.sqrt(d2) / LINK;
          const lit = Math.max(a.flash, b.flash);
          ctx.strokeStyle = `rgba(63,224,224,${(0.3 * k * k + 0.45 * lit * k).toFixed(3)})`;
          ctx.lineWidth = lit > 0.3 ? 1.4 : 1;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      // signals
      spawnIn -= dt;
      if (spawnIn <= 0) { spawnIn = 0.25 + Math.random() * 0.5; fire(Math.floor(Math.random() * nodes.length), 0); }
      const keep: Signal[] = [];
      for (const s of signals) {
        s.t += s.v * dt;
        const a = nodes[s.a], b = nodes[s.b];
        if (s.t >= 1) {
          b.flash = 1;
          if (s.hops < 4 && Math.random() < 0.7) fire(s.b, s.hops + 1);
          continue;
        }
        keep.push(s);
        const x = a.x + (b.x - a.x) * s.t, y = a.y + (b.y - a.y) * s.t;
        const tx = a.x + (b.x - a.x) * Math.max(0, s.t - 0.12), ty = a.y + (b.y - a.y) * Math.max(0, s.t - 0.12);
        const grad = ctx.createLinearGradient(tx, ty, x, y);
        grad.addColorStop(0, "rgba(63,224,224,0)");
        grad.addColorStop(1, "rgba(200,255,255,0.9)");
        ctx.strokeStyle = grad; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
        ctx.fillStyle = "rgba(220,255,255,0.95)";
        ctx.shadowColor = "#3fe0e0"; ctx.shadowBlur = 10;
        ctx.beginPath(); ctx.arc(x, y, 1.8, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
      }
      signals = keep;
      // nodes: a slow twinkle, and a flash when a signal lands
      for (const n of nodes) {
        const glow = 0.6 + 0.35 * Math.sin(n.p) + n.flash * 0.6;
        const r = n.r + n.flash * 2.5;
        if (n.flash > 0.05) { ctx.shadowColor = "#3fe0e0"; ctx.shadowBlur = 14 * n.flash; }
        ctx.fillStyle = `rgba(${Math.round(63 + 160 * n.flash)},${Math.round(224 + 31 * n.flash)},${Math.round(224 + 31 * n.flash)},${Math.min(1, glow).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
      }
      raf = requestAnimationFrame(step);
    };
    const onVis = () => {
      if (document.hidden) cancelAnimationFrame(raf);
      else { last = performance.now(); raf = requestAnimationFrame(step); }
    };
    size();
    window.addEventListener("resize", size);
    document.addEventListener("visibilitychange", onVis);
    raf = requestAnimationFrame(step);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", size);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);
  if (REDUCED) return null;
  return <div className="board-layer board-mesh"><canvas ref={ref} aria-hidden="true" /></div>;
}

// ================================================================== small pieces

const GLYPHS = "01ABCDEF<>/\\|=+-_:;#%&$@?*";

/** Text that decodes: glyphs churn and settle left to right whenever the text changes. */
function useDecode(text: string, frames = 16, ms = 36): string {
  const [shown, setShown] = useState(text);
  useEffect(() => {
    if (REDUCED) { setShown(text); return; }
    let f = 0;
    const id = window.setInterval(() => {
      f += 1;
      const settled = Math.floor((f / frames) * text.length);
      setShown(text.split("").map((ch, i) =>
        ch === " " ? " " : i < settled ? ch : GLYPHS[Math.floor(Math.random() * GLYPHS.length)]).join(""));
      if (f >= frames) { setShown(text); window.clearInterval(id); }
    }, ms);
    return () => window.clearInterval(id);
  }, [text, frames, ms]);
  return shown;
}

function Decoded({ text, className, style }: { text: string; className?: string; style?: React.CSSProperties }) {
  const shown = useDecode(text);
  return <span className={className} style={style}>{shown}</span>;
}

function Lamp({ health }: { health: Serving["health"] }) {
  const c = HEALTH_COLOR[health];
  const lit = health === "ok" || health === "degraded" || health === "down";
  return (
    <span
      className={`board-lamp${lit ? " lit " + health : ""}`}
      title={health}
      style={{
        background: c, color: c,
        boxShadow: lit ? `0 0 10px ${c}, 0 0 24px color-mix(in srgb, ${c} 45%, transparent)` : "none",
        opacity: lit ? 1 : 0.5,
      }}
    />
  );
}

function Pill({ text, color, title }: { text: string; color: string; title?: string }) {
  return (
    <span className="board-pill" title={title}
      style={{ color, borderColor: `color-mix(in srgb, ${color} 45%, transparent)`, background: `color-mix(in srgb, ${color} 7%, transparent)` }}>
      {text}
    </span>
  );
}

/** The ring on a card: how many of its live environments are up. */
function Ring({ ok, total, warn }: { ok: number; total: number; warn: boolean }) {
  const r = 14, c = 2 * Math.PI * r;
  const frac = total ? ok / total : 0;
  return (
    <svg className={`board-ring${warn ? " warn" : ""}`} viewBox="0 0 34 34" aria-hidden="true">
      <circle className="track" cx="17" cy="17" r={r} />
      <circle className="arc" cx="17" cy="17" r={r} strokeDasharray={c} strokeDashoffset={c * (1 - frac)} />
    </svg>
  );
}

function Tile({ n, label, color }: { n: string | number; label: string; color?: string }) {
  return (
    <div className="board-tile materialize" style={color ? ({ "--tile-color": color } as React.CSSProperties) : undefined}>
      <div className="n">{n}</div>
      <div className="l">{label}</div>
    </div>
  );
}

// ================================================================== rows + cards

type Thread = "same" | "changed" | "unknown" | null;

function EnvRow({ row, thread }: { row: Row; thread: Thread }) {
  const api = row.api;
  const env = row.env.toUpperCase();
  if (!row.exists) {
    return (
      <div className={`board-row ${row.env} flex items-center gap-3 py-2.5 text-[13px]`} style={MUTED}>
        {thread && <span className={`board-thread ${thread}`} />}
        <Lamp health="absent" />
        <span className="board-env" style={{ opacity: 0.55 }}>{env}</span>
        <span>not deployed in this environment</span>
      </div>
    );
  }
  const unread = !row.checked_at;
  return (
    <div className={`board-row ${row.env} py-2.5`}>
      {thread && <span className={`board-thread ${thread}`} title={
        thread === "same" ? "Same commit as the environment below - one line of descent"
          : thread === "changed" ? "The commit changed between these environments"
            : "One side could not say which commit it serves"} />}
      {/* line 1: the environment and its two verdicts - is it up, has the repo moved on */}
      <div className="flex items-center gap-3 text-[13px]">
        <Lamp health={row.health} />
        <span className="board-env">{env}</span>
        {unread ? (
          <span style={MUTED}>not read yet</span>
        ) : (
          <>
            <span style={{ color: HEALTH_COLOR[row.health], minWidth: 56, letterSpacing: 1 }}>{row.health}</span>
            <Pill
              text={DRIFT_LABEL[row.drift] + (row.drift === "behind" && row.behind_by != null ? ` by ${row.behind_by}` : "")}
              color={DRIFT_COLOR[row.drift]}
              title={row.repo_commit ? `repo HEAD is ${row.repo_commit}` : undefined}
            />
            {api?.dirty && <Pill text="dirty" color="var(--error)" title="Built from a working tree with uncommitted changes" />}
            {api?.is_split && <Pill text={`split ${api.traffic_percent}%`} color="var(--working)" />}
            {api?.read_only && <Pill text="read-only" color="var(--amber)" />}
            {api?.flags?.appCheckRequired === true && <Pill text="AppCheck" color="var(--cyan)" />}
          </>
        )}
      </div>
      {/* line 2: what exactly is serving - commit, revision, when, from which database */}
      {!unread && api && (
        <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-[12px] mt-1.5 pl-6" style={MUTED}>
          {api.commit
            ? <Decoded text={api.commit} className="board-chip" />
            : <span className="board-chip dim">commit not recorded</span>}
          {api.revision && <span className="font-mono">{api.revision}</span>}
          {api.deployed_at && <span>{when(api.deployed_at)}{api.deployed_by ? ` · ${api.deployed_by}` : ""}</span>}
          {api.db && <span>db <span style={{ color: "var(--text)" }}>{api.db}</span></span>}
        </div>
      )}
      {row.note && (
        <div className="text-[12px] mt-1 pl-6" style={{ color: row.needs_attention || api?.dirty ? "var(--working)" : "var(--muted)" }}>
          {row.note}
        </div>
      )}
    </div>
  );
}

/** The thread between two neighbouring environments: same commit, a changed one, or unread. */
function threadBetween(a: Row, b: Row): Thread {
  if (!a.exists || !b.exists) return null;
  if (!a.checked_at || !b.checked_at) return null;
  const ca = a.api?.commit, cb = b.api?.commit;
  if (!ca || !cb) return "unknown";
  return ca === cb ? "same" : "changed";
}

function AppCard({ card, index, busy, onRead }: { card: Card; index: number; busy: boolean; onRead: (app: string) => void }) {
  const ref = useRef<HTMLElement | null>(null);
  const attention = card.needs_attention;
  const live = card.envs.filter((r) => r.exists && r.checked_at);
  const up = live.filter((r) => r.health === "ok").length;
  const dirty = card.envs.some((r) => r.api?.dirty);

  // 3D tilt + glare follow the cursor; written straight to the element, never through React state
  const onMove = (e: React.MouseEvent<HTMLElement>) => {
    const el = ref.current;
    if (!el || REDUCED) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
    el.style.setProperty("--ry", `${((px - 0.5) * 7).toFixed(2)}deg`);
    el.style.setProperty("--rx", `${((0.5 - py) * 7).toFixed(2)}deg`);
    el.style.setProperty("--mx", `${(px * 100).toFixed(1)}%`);
    el.style.setProperty("--my", `${(py * 100).toFixed(1)}%`);
  };
  const onLeave = () => {
    const el = ref.current;
    if (!el) return;
    el.style.setProperty("--rx", "0deg");
    el.style.setProperty("--ry", "0deg");
  };

  return (
    <section
      ref={ref}
      className={`board-card materialize p-4${busy ? " reading" : ""}${attention ? " attention" : ""}`}
      style={{ "--i": index * 3 } as React.CSSProperties}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      <i className="corners" aria-hidden="true" />
      <i className="trace" aria-hidden="true" />
      <i className="glare" aria-hidden="true" />
      <i className="scan" aria-hidden="true" />
      <div className="flex items-center gap-3 relative">
        <div>
          <div className="board-index">APP {String(index + 1).padStart(2, "0")}</div>
          <div className="board-app"><Decoded text={card.app} /></div>
        </div>
        {attention && <Pill text="needs a look" color="var(--working)" />}
        <div className="flex-1" />
        {live.length > 0 && <Ring ok={up} total={live.length} warn={attention || dirty} />}
        <button className="btn text-xs" disabled={busy} onClick={() => onRead(card.app)}>
          {busy ? "Reading…" : "Read"}
        </button>
      </div>
      {card.repo && (
        <div className="flex items-center gap-2 mt-2">
          <span className="board-chip dim elide" title={card.repo}>{card.repo}</span>
          {live.length > 0 && (
            <span className="board-pulse ml-auto" title={`${live.length} environment${live.length === 1 ? "" : "s"} read`}>
              {card.envs.filter((r) => r.exists).map((r) => <i key={r.key} className={r.checked_at ? r.health : ""} />)}
            </span>
          )}
        </div>
      )}
      <div className="mt-3 pl-1">
        {card.envs.map((row, i) => (
          <EnvRow key={row.key} row={row}
            thread={i < card.envs.length - 1 ? threadBetween(row, card.envs[i + 1]) : null} />
        ))}
      </div>
    </section>
  );
}

/** Does this card say the word? App, env, revision, commit, db, note, repo, flags - all searchable. */
function matches(card: Card, q: string): boolean {
  if (!q) return true;
  const hay = [card.app, card.repo || "",
    ...card.envs.flatMap((r) => [r.env, r.run_service || "", r.note || "", r.drift, r.health,
      r.api?.commit || "", r.api?.revision || "", r.api?.db || "", r.api?.deployed_by || "",
      r.api?.dirty ? "dirty" : "", r.api?.is_split ? "split" : "", r.api?.read_only ? "read-only" : "",
      r.api?.flags?.appCheckRequired ? "appcheck" : "", r.needs_attention ? "attention" : ""])]
    .join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

// ================================================================== the page

export default function BoardPage() {
  const [board, setBoard] = useState<Board | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // "*" for the whole company, or an app
  const [q, setQ] = useState("");

  const load = useCallback(() => {
    void api.get<Board>("/api/fleet")
      .then((b) => { setBoard(b); setFailed(null); })
      .catch((e: Error) => setFailed(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  const refresh = useCallback((app?: string) => {
    setBusy(app || "*");
    void api.post<Board>("/api/fleet/refresh", app ? { app } : {})
      .then((b) => { setBoard(b); setFailed(null); })
      .catch((e: Error) => setFailed(e.message))
      .finally(() => setBusy(null));
  }, []);

  const company = board?.companies[0] ?? null;
  const cards = useMemo(() => (company?.apps ?? []).filter((c) => matches(c, q)), [company, q]);
  const notReady = (board?.readiness ?? []).filter((r) => !r.ok);
  const cells = (company?.apps ?? []).flatMap((c) => c.envs).filter((r) => r.exists);
  const read = cells.filter((r) => r.checked_at);
  const up = read.filter((r) => r.health === "ok").length;
  const look = (company?.apps ?? []).filter((c) => c.needs_attention).length
    + read.filter((r) => r.api?.dirty && !r.needs_attention).length;
  const title = useDecode("BOARD", 22, 45);

  return (
    <div className="board-stage" style={{ pointerEvents: "auto" }}>
      <HelixLayer />
      <NeuralLayer />
      <div className="board-layer board-hud" />
      {busy && <div className="board-radar" aria-hidden="true" />}
      {busy && <div className="board-progress" aria-hidden="true" />}
      <i className="board-corner tl" /><i className="board-corner tr" />
      <i className="board-corner bl" /><i className="board-corner br" />

      <div className="h-full overflow-y-auto pt-16 px-8 pb-12 relative">
        <div className="max-w-[1140px] mx-auto space-y-5">
          <div className="flex items-end gap-4 flex-wrap">
            <div>
              <div className="board-kicker">{company?.label ?? "the company"} · the fleet</div>
              <div className="font-display font-bold flex items-center gap-3">
                <span className="text-glow-cyan text-[22px]" style={{ color: "var(--cyan)" }}>⬡</span>
                <span className="board-title">{title}</span>
              </div>
            </div>
            <div className="flex-1" />
            <div className="board-tiles">
              <Tile n={cells.length} label="cells" />
              <Tile n={read.length ? up : "–"} label="up" color={read.length && up === read.length ? "var(--done)" : undefined} />
              <Tile n={read.length ? look : "–"} label="need a look" color={look > 0 ? "var(--working)" : undefined} />
              <Tile n={company?.checked_at ? ago(company.checked_at) : "never"} label="last read" color="var(--cyan)" />
            </div>
            <button className="btn btn-primary text-xs self-center" disabled={busy !== null} onClick={() => refresh()}>
              {busy === "*" ? "Reading the fleet…" : "Refresh"}
            </button>
          </div>

          <div className="text-[13px] max-w-[820px]" style={MUTED}>
            Every app the company runs, in every environment: what is serving, which commit, from which
            database, and whether the repo has moved on. Read from Cloud Run and each app's own health
            endpoint — nothing here is guessed. Reads only; the deploy lane comes in Phase 2.
          </div>

          {notReady.length > 0 && (
            <div className="board-card p-4 text-[13px] space-y-1">
              <i className="corners" aria-hidden="true" />
              {notReady.map((r) => (
                <div key={r.what}>
                  <span className="text-xs tracking-wide mr-2" style={{ color: "var(--working)" }}>{r.what.toUpperCase()}</span>
                  {r.why}
                </div>
              ))}
            </div>
          )}

          {failed && <div className="text-[13px]" style={{ color: "var(--error)" }}>{failed}</div>}

          <div className="flex items-center gap-3 flex-wrap">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search apps, environments, commits, databases…"
              className="board-search flex-1 min-w-[260px]"
            />
            {read.length > 0 && (
              <span className="board-pulse" title={`${read.length} of ${cells.length} cells read`}>
                {cells.map((r) => <i key={r.key} className={r.checked_at ? r.health : ""} />)}
              </span>
            )}
            {company && (
              <span className="text-xs" style={MUTED}>{company.gcp_project} · {company.region}</span>
            )}
            {board?.profile && board.profile !== "desktop" && (
              <Pill text={`${board.profile} profile`} color="var(--muted)" />
            )}
          </div>

          {!failed && board === null && <div className="text-[13px]" style={MUTED}>Opening the board…</div>}

          {company && cards.length === 0 && (
            <div className="board-card p-5 text-[13px]" style={MUTED}>
              <i className="corners" aria-hidden="true" />
              Nothing matches "{q}".
            </div>
          )}

          <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
            {cards.map((card, i) => (
              <AppCard key={card.app} card={card} index={i} busy={busy === card.app || busy === "*"} onRead={(app) => refresh(app)} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
