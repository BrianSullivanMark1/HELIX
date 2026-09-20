// THE CONSOLE — the company's apps and their environments (the Board), merged with the Menu's
// protocols / agents / holograms / vault, THE FORGE's Dev / Git / Deploy buttons on every app card,
// and the add-project wizard. Formerly Board.tsx.
// (HELIX_MARK1_PLAN.md §17: THE FORGE's cards + search, one card per app, one row per
// environment). Phase 1: reads only. Every value on this page came from Cloud Run, the app's own
// /api/health, or the repo; nothing is inferred, and a cell HELIX could not read says "unknown"
// rather than anything more comfortable.
//
// Plain names on purpose (2026-09-17): company / app / environment. The evolution-and-neural-
// network theme lives in the art: a double helix turning behind the board with signals running
// its strands (Three.js, like the orb), a neural net that fires across the page (canvas), HUD
// frames on the cards, and the lineage thread that joins DEV -> QA -> PROD.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import { radioBeat } from "../components/Radio";
import "./console.css";
import Menu from "./Menu";
import { useHelix } from "../lib/store";

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
  detail?: string | null;
  needs_attention: boolean;
  api: Serving | null;
  site: Serving | null;
}

interface Link { repo: string | null; branch: string | null; folder: string | null; custom: boolean; linked: boolean; why: string | null }
interface Card { app: string; repo: string | null; link?: Link; envs: Row[]; needs_attention: boolean }

export const TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=HELIX%20console";

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

// Plain words, THE FORGE's way: what is running vs what is in GitHub.
const DRIFT_LABEL: Record<Row["drift"], string> = {
  clean: "matches GitHub",
  behind: "behind GitHub",
  ahead: "newer than GitHub",
  diverged: "differs from GitHub",
  rolled_back: "rolled back",
  unknown: "not compared",
};
const DRIFT_TITLE: Record<Row["drift"], string> = {
  clean: "What is running is exactly the latest commit on the branch.",
  behind: "GitHub has newer commits than what is running - a deploy would bring them.",
  ahead: "What is running is not in the branch's history - someone deployed something unpushed.",
  diverged: "What is running and the branch have gone different ways.",
  rolled_back: "Traffic was moved back to an older revision on purpose.",
  unknown: "HELIX could not compare - no version stamp on the deploy, or GitHub could not be read.",
};
const HEALTH_LABEL: Record<Row["health"], string> = { ok: "running", degraded: "struggling", down: "down", absent: "not deployed", unknown: "unknown" };

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

const HELIX = { points: 900, turns: 13, radius: 1.75, height: 60, rungEvery: 6 };
const TURN_H = HELIX.height / HELIX.turns;          // one full turn along the axis: the wrap of the endless surf
const END_FADE = 0.16;                              // the strands fade over this much of each end: no cut-off
function taper(t: number): number { const e = END_FADE; return Math.min(1, t / e, (1 - t) / e); }
const SIGNALS = 12;
const TAIL = 9;

/** Where strand `s` is at parameter t in [0,1], in the helix's own space. */
function strandAt(t: number, s: 0 | 1, out: THREE.Vector3): THREE.Vector3 {
  const a = t * HELIX.turns * Math.PI * 2 + s * Math.PI;
  return out.set(Math.cos(a) * HELIX.radius, (t - 0.5) * HELIX.height, Math.sin(a) * HELIX.radius);
}

/** The double helix: two strands of glowing points, rungs between them, signals travelling the
 *  strands, and a drift of dust for depth. Turns slowly; leans with the cursor. */
function HelixScene({ pointer, colors }: { pointer: React.MutableRefObject<{ x: number; y: number; vx: number; vy: number }>; colors: [string, string] }) {
  const group = useRef<THREE.Group>(null!);
  const strandMats = useRef<THREE.PointsMaterial[]>([]);
  const pulse = useRef({ next: 5, t: -1 });
  const spin = useRef(0);
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
        const f = taper(i / (n - 1));
        col[k] = c.r * f; col[k + 1] = c.g * f; col[k + 2] = c.b * f;
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
  const surf = useRef<THREE.Group>(null!);
  const clock = useRef(0);
  const camera = useThree((st) => st.camera);
  // THE HAND ON THE STRAND: every node keeps a displacement that eases toward where the cursor
  // pushes it (in view space, so it bends the way you see it) and springs back when you leave.
  const bend = useMemo(() => ({ d: new Float32Array(HELIX.points * 2 * 3), tmp: new THREE.Vector3(), tmp2: new THREE.Vector3(), inv: new THREE.Matrix4(), right: new THREE.Vector3(), up: new THREE.Vector3() }), []);

  // The strand colors: A on one strand, B on the other, gold every 23rd node. Rewritten when the
  // user picks new colors in Settings (the geometry is shared with the bloom and the twin).
  useEffect(() => {
    const col = strands.getAttribute("color") as THREE.BufferAttribute;
    const a = new THREE.Color(colors[0] || "#3fe0e0"), b = new THREE.Color(colors[1] || "#2a8cff");
    const gold = new THREE.Color("#ffc857");
    const n = HELIX.points;
    for (let sIdx = 0; sIdx < 2; sIdx++) {
      for (let i = 0; i < n; i++) {
        const c = i % 23 === 0 ? gold : sIdx === 0 ? a : b;
        const f = taper(i / (n - 1));
        col.setXYZ(sIdx * n + i, c.r * f, c.g * f, c.b * f);
      }
    }
    col.needsUpdate = true;
  }, [strands, colors]);

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
    // the mouse spins it: horizontal motion adds spin that decays, so a sweep across the window
    // sends the helix turning and it settles back to its idle drift
    const p = pointer.current;
    spin.current += p.vx * 0.9;
    p.vx *= 0.5; p.vy *= 0.5;
    spin.current *= 0.94;
    g.rotation.y += dt * 0.12 + spin.current * dt;
    // the pulse: every 6-11 s a flash runs the whole helix - every node brightens and swells,
    // then it settles. While it runs the rung wave is driven faster too.
    const pu = pulse.current;
    pu.next -= dt;
    // THE RADIO: when a song plays, the helix keeps its beat - a kick fires the pulse, the bass
    // swells it, the spin follows the energy. Silent radio, nothing changes.
    const beat = radioBeat();
    const kick = beat.kick;
    if (kick && pu.t < 0) { pu.t = 0; pu.next = 6 + Math.random() * 5; }
    if (pu.next <= 0) { pu.t = 0; pu.next = 6 + Math.random() * 5; }
    let flash = 0;
    if (pu.t >= 0) {
      pu.t += dt * (beat.playing ? 2.2 : 1);
      flash = Math.max(0, Math.sin(Math.min(1, pu.t / 1.1) * Math.PI));
      if (pu.t > 1.1) pu.t = -1;
    }
    flash = Math.max(flash, beat.level * 0.7);
    spin.current += beat.level * beat.level * dt * 1.4;
    for (const m of strandMats.current) { if (m) m.size = m.userData.base * (1 + 0.9 * flash); }
    // a slow breath, and a sway
    const breath = 1 + 0.025 * Math.sin(t * 0.6) + 0.04 * flash + 0.05 * beat.level;
    g.scale.setScalar(breath);
    g.position.y = 0.2 + 0.25 * Math.sin(t * 0.23);
    // the distant twin turns the other way, slower
    if (twin.current) { twin.current.rotation.y -= dt * 0.07 + spin.current * dt * 0.3; twin.current.rotation.z = -0.5 + 0.03 * Math.sin(t * 0.3); }
    // THE SURF: the strands slide along their own axis forever - one turn, then wrap, and the
    // faded ends hide the seam. Faster with the music.
    if (surf.current) { surf.current.position.y = -((t * (0.35 + beat.level * 1.2)) % TURN_H); }
    // THE BEND: the cursor pushes the nodes it passes over; they ease out and spring back
    g.updateMatrixWorld();
    if (surf.current) surf.current.updateMatrixWorld();
    const sp = strands.getAttribute("position") as THREE.BufferAttribute;
    const n = HELIX.points;
    const B = bend;
    const m = surf.current ? surf.current.matrixWorld : g.matrixWorld;
    B.inv.copy(m).invert();
    B.right.set(1, 0, 0).applyQuaternion(camera.quaternion); B.up.set(0, 1, 0).applyQuaternion(camera.quaternion);
    const mx = pointer.current.x, my = pointer.current.y, REACH = 0.34;
    const wake = 1 - Math.pow(0.001, dt);
    for (let s2 = 0; s2 < 2; s2++) for (let i = 0; i < n; i++) {
      const idx = s2 * n + i;
      strandAt(i / (n - 1), s2 as 0 | 1, B.tmp);
      B.tmp2.copy(B.tmp).applyMatrix4(m).project(camera);
      const dx = B.tmp2.x - mx, dy = (B.tmp2.y - my) * 0.75;
      const dd = Math.hypot(dx, dy);
      let tx = 0, ty = 0, tz = 0;
      if (dd < REACH && B.tmp2.z < 1) {
        const f = Math.pow(1 - dd / REACH, 2) * 1.1;
        const ux = dx / (dd || 1), uy = dy / (dd || 1);
        // the push, in world space, along the camera's right/up
        tx = (B.right.x * ux + B.up.x * uy) * f; ty = (B.right.y * ux + B.up.y * uy) * f; tz = (B.right.z * ux + B.up.z * uy) * f;
      }
      const k = idx * 3;
      B.d[k] += (tx - B.d[k]) * wake * 0.8; B.d[k + 1] += (ty - B.d[k + 1]) * wake * 0.8; B.d[k + 2] += (tz - B.d[k + 2]) * wake * 0.8;
      // world displacement -> local (rotation only)
      B.tmp2.set(B.d[k], B.d[k + 1], B.d[k + 2]).transformDirection(B.inv).multiplyScalar(Math.hypot(B.d[k], B.d[k + 1], B.d[k + 2]));
      sp.setXYZ(idx, B.tmp.x + B.tmp2.x, B.tmp.y + B.tmp2.y, B.tmp.z + B.tmp2.z);
    }
    sp.needsUpdate = true;
    // a wave of light climbs the rungs; the rungs follow the bent strands
    const rc = rungRef.current?.geometry.getAttribute("color") as THREE.BufferAttribute | undefined;
    const rpos = rungRef.current?.geometry.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (rc && rpos) {
      for (let i = 0; i < rc.count / 2; i++) {
        const node = Math.min(n - 1, i * HELIX.rungEvery);
        rpos.setXYZ(i * 2, sp.getX(node), sp.getY(node), sp.getZ(node));
        rpos.setXYZ(i * 2 + 1, sp.getX(n + node), sp.getY(n + node), sp.getZ(n + node));
        const y = rpos.getY(i * 2);
        const w = Math.max(0, Math.sin(y * 0.9 - t * 1.6));
        const fade = taper(node / (n - 1));
        const k = Math.min(1, 0.12 + 0.95 * w * w * w * w + flash * 0.8) * fade;
        rc.setXYZ(i * 2, 0.25 * k + 0.02 * fade, 0.88 * k + 0.05 * fade, 0.88 * k + 0.06 * fade);
        rc.setXYZ(i * 2 + 1, 0.25 * k + 0.02 * fade, 0.88 * k + 0.05 * fade, 0.88 * k + 0.06 * fade);
      }
      rc.needsUpdate = true; rpos.needsUpdate = true;
    }
    // lean with the cursor, gently
    const px = pointer.current.x, py = pointer.current.y;
    g.rotation.z += ((0.42 + px * 0.22) - g.rotation.z) * 0.05;
    g.rotation.x += ((py * 0.3) - g.rotation.x) * 0.05;
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
        <group ref={surf}>
          <points geometry={strands} frustumCulled={false}>
            <pointsMaterial ref={(m) => { if (m) { m.userData.base = 0.26; strandMats.current[0] = m; } }}
              map={tex} size={0.26} vertexColors transparent depthWrite={false}
              blending={THREE.AdditiveBlending} sizeAttenuation opacity={1} />
          </points>
          {/* the bloom: the same strand points, drawn huge and faint, so every node wears a halo */}
          <points geometry={strands} frustumCulled={false}>
            <pointsMaterial ref={(m) => { if (m) { m.userData.base = 0.95; strandMats.current[1] = m; } }}
              map={tex} size={0.95} vertexColors transparent depthWrite={false}
              blending={THREE.AdditiveBlending} sizeAttenuation opacity={0.16} />
          </points>
          <lineSegments ref={rungRef} geometry={rungs} frustumCulled={false}>
            <lineBasicMaterial vertexColors transparent opacity={0.9} blending={THREE.AdditiveBlending} depthWrite={false} />
          </lineSegments>
          <points ref={signals} geometry={sigGeo} frustumCulled={false}>
            <pointsMaterial map={tex} size={0.5} vertexColors transparent depthWrite={false}
              blending={THREE.AdditiveBlending} sizeAttenuation />
          </points>
        </group>
      </group>
      {/* the twin: the same helix far behind and to the left, dim, turning the other way - depth */}
      <group ref={twin} position={[-6.5, 1.5, -9]} rotation={[0, 0, -0.5]} scale={[1.6, 1.6, 1.6]}>
        <points geometry={strands} frustumCulled={false}>
          <pointsMaterial map={tex} size={0.22} vertexColors transparent depthWrite={false}
            blending={THREE.AdditiveBlending} sizeAttenuation opacity={0.35} />
        </points>
        <lineSegments geometry={rungs} frustumCulled={false}>
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

/** ONE WEBGL CONTEXT PER CANVAS, RELEASED ON UNMOUNT. Browsers keep about sixteen WebGL contexts
 *  alive and silently kill the oldest when a new one arrives ("THREE.WebGLRenderer: Context Lost",
 *  seen 2026-09-21 - the face went black). Every canvas mounts this: it lets the browser RESTORE a
 *  lost context instead of abandoning it (three re-uploads everything on restore). R3F releases
 *  the context itself on unmount; the Talk page now opens one context, not two. */
export function ContextGuard() {
  const gl = useThree((st) => st.gl);
  useEffect(() => {
    const el = gl.domElement;
    const lost = (e: Event) => { e.preventDefault(); };
    el.addEventListener("webglcontextlost", lost);
    return () => { el.removeEventListener("webglcontextlost", lost); };   // R3F itself disposes and releases the context on unmount
  }, [gl]);
  return null;
}

/** The helix as a group for ANOTHER canvas (the face's), so a page never opens two contexts. */
export function HelixBackdrop({ colors, position = [0, 0, -6], scale = 0.9 }: { colors: [string, string]; position?: [number, number, number]; scale?: number }) {
  const pointer = usePointer();
  return (
    <group position={position} scale={scale}>
      <HelixScene pointer={pointer} colors={colors} />
    </group>
  );
}

function usePointer() {
  const pointer = useRef({ x: 0, y: 0, vx: 0, vy: 0 });
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = -((e.clientY / window.innerHeight) * 2 - 1);
      pointer.current.vx += nx - pointer.current.x;
      pointer.current.vy += ny - pointer.current.y;
      pointer.current.x = nx;
      pointer.current.y = ny;
    };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, []);
  return pointer;
}

export function HelixLayer({ colors }: { colors: [string, string] }) {
  const pointer = useRef({ x: 0, y: 0, vx: 0, vy: 0 });
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = -((e.clientY / window.innerHeight) * 2 - 1);
      pointer.current.vx += nx - pointer.current.x;
      pointer.current.vy += ny - pointer.current.y;
      pointer.current.x = nx;
      pointer.current.y = ny;
    };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, []);
  if (REDUCED) return null;
  return (
    <div className="board-layer board-helix">
      <Canvas dpr={[1, 1.5]} camera={{ fov: 48, position: [0, 0, 9.5] }} gl={{ antialias: true, alpha: true }}
        onCreated={({ scene }) => { scene.fog = new THREE.Fog("#080b0f", 10, 24); }}>
        <ContextGuard />
        <HelixScene pointer={pointer} colors={colors} />
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
export function NeuralLayer({ density = 1, keepOut }: { density?: number; keepOut?: { x: number; y: number; r: number } | null } = {}) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || REDUCED) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const N = Math.round(70 * density);
    const LINK = 165;
    type Node = { x: number; y: number; vx: number; vy: number; r: number; p: number; flash: number; hx: number; hy: number; px: number; py: number };
    type Signal = { a: number; b: number; t: number; v: number; hops: number };
    let nodes: Node[] = [];
    let signals: Signal[] = [];
    let w = 0, h = 0, raf = 0, last = 0, alive = true, spawnIn = 0.3;
    // THE HAND IN THE NET: the cursor pushes nodes away (links stretch, then snap with a spark),
    // and a click sends a ring out that throws everything it crosses. Nodes drift home after.
    const mouse = { x: -9999, y: -9999, vx: 0, vy: 0, lx: -9999, ly: -9999 };
    const beatNow = { level: 0, kick: false, tick: 0 };
    type Ring = { x: number; y: number; r: number; v: number; life: number };
    type Spark = { x: number; y: number; vx: number; vy: number; life: number };
    let rings: Ring[] = [];
    let sparks: Spark[] = [];
    const REACH = 130;
    const onMove = (e: MouseEvent) => {
      const r = canvas.getBoundingClientRect();
      mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top;
    };
    const onLeave = () => { mouse.x = -9999; mouse.y = -9999; };
    const onDown = (e: MouseEvent) => {
      const r = canvas.getBoundingClientRect();
      rings.push({ x: e.clientX - r.left, y: e.clientY - r.top, r: 0, v: 520, life: 1 });
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mousedown", onDown);
    document.addEventListener("mouseleave", onLeave);

    const seed = () => {
      nodes = Array.from({ length: N }, () => {
        const x = Math.random() * w, y = Math.random() * h;
        return { x, y, hx: x, hy: y, px: 0, py: 0,
          vx: (Math.random() - 0.5) * 14, vy: (Math.random() - 0.5) * 14,
          r: 1.2 + Math.random() * 2.0, p: Math.random() * Math.PI * 2, flash: 0 };
      });
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
      const b0 = radioBeat();
      beatNow.level = b0.level; beatNow.kick = b0.kick; beatNow.tick = t / 1000;
      if (b0.kick) rings.push({ x: keepOut ? keepOut.x * w : w / 2, y: keepOut ? keepOut.y * h : h / 2, r: keepOut ? keepOut.r * Math.min(w, h) : 0, v: 420 + b0.level * 400, life: 0.7 });   // a kick: a wave through the jelly
      ctx.clearRect(0, 0, w, h);
      mouse.vx = mouse.x - mouse.lx; mouse.vy = mouse.y - mouse.ly; mouse.lx = mouse.x; mouse.ly = mouse.y;
      const speed = Math.min(60, Math.hypot(mouse.vx, mouse.vy));
      for (const n of nodes) {
        // the home drifts; the node is the home plus a push that relaxes back
        n.hx += n.vx * dt; n.hy += n.vy * dt; n.p += dt * 0.8;
        n.flash = Math.max(0, n.flash - dt * 1.6);
        if (n.hx < -10) n.hx = w + 10; else if (n.hx > w + 10) n.hx = -10;
        if (n.hy < -10) n.hy = h + 10; else if (n.hy > h + 10) n.hy = -10;
        // the cursor: push away, harder when it moves fast
        const dx = n.x - mouse.x, dy = n.y - mouse.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < REACH * REACH && d2 > 0.01) {
          const d = Math.sqrt(d2);
          const f = (1 - d / REACH) * (60 + speed * 6);
          n.px += (dx / d) * f * dt * 8; n.py += (dy / d) * f * dt * 8;
          if (speed > 18 && Math.random() < 0.05) sparks.push({ x: n.x, y: n.y, vx: (dx / d) * 120 + mouse.vx * 4, vy: (dy / d) * 120 + mouse.vy * 4, life: 0.5 });
        }
        // THE FACE / THE CENTER: the net is a lattice the face breaks through - nodes are kept out
        // of the circle and gather at its rim, a constellation with a hole in it
        if (keepOut) {
          const kx = keepOut.x * w, ky = keepOut.y * h, kr = keepOut.r * Math.min(w, h);
          const fx = n.hx + n.px - kx, fy = n.hy + n.py - ky;
          const fd = Math.hypot(fx, fy);
          if (fd < kr && fd > 0.01) { const push = (kr - fd) * 0.9; n.px += (fx / fd) * push; n.py += (fy / fd) * push; }
        }
        // THE MUSIC: bass wobbles the jelly, a kick fires a ring
        if (beatNow.level > 0.02) { n.px += Math.sin(n.p * 3 + beatNow.tick * 7) * beatNow.level * 40 * dt; n.py += Math.cos(n.p * 2.3 + beatNow.tick * 6) * beatNow.level * 40 * dt; }
        // the rings
        for (const rg of rings) {
          const rdx = n.x - rg.x, rdy = n.y - rg.y;
          const rd = Math.hypot(rdx, rdy);
          if (Math.abs(rd - rg.r) < 26 && rd > 0.01) {
            const f = 180 * rg.life;
            n.px += (rdx / rd) * f * dt * 8; n.py += (rdy / rd) * f * dt * 8;
            n.flash = Math.max(n.flash, 0.8 * rg.life);
          }
        }
        // relax home
        n.px *= Math.pow(0.12, dt); n.py *= Math.pow(0.12, dt);
        n.x = n.hx + n.px; n.y = n.hy + n.py;
      }
      // rings expand and fade; sparks fly and die
      for (const rg of rings) { rg.r += rg.v * dt; rg.life -= dt * 1.4; }
      rings = rings.filter((rg) => rg.life > 0);
      for (const sp of sparks) { sp.x += sp.vx * dt; sp.y += sp.vy * dt; sp.vx *= 0.92; sp.vy *= 0.92; sp.life -= dt * 2; }
      sparks = sparks.filter((sp) => sp.life > 0);
      // links
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          const dx = a.x - b.x, dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK * LINK) continue;
          const k = 1 - Math.sqrt(d2) / LINK;
          const lit = Math.max(a.flash, b.flash);
          // a link under strain (either end pushed far from home) brightens white, then snaps
          const strain = Math.min(1, (Math.hypot(a.px, a.py) + Math.hypot(b.px, b.py)) / 90);
          if (strain > 0.85 && Math.random() < 0.08) {
            const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
            for (let q = 0; q < 3; q++) sparks.push({ x: mx, y: my, vx: (Math.random() - 0.5) * 160, vy: (Math.random() - 0.5) * 160, life: 0.45 });
            continue;
          }
          ctx.strokeStyle = strain > 0.4
            ? `rgba(${Math.round(63 + 190 * strain)},${Math.round(224 + 31 * strain)},255,${(0.3 * k + 0.5 * strain).toFixed(3)})`
            : `rgba(63,224,224,${(0.3 * k * k + 0.45 * lit * k).toFixed(3)})`;
          ctx.lineWidth = lit > 0.3 || strain > 0.4 ? 1.4 : 1;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      // signals
      const beat = beatNow;
      spawnIn -= dt * (1 + beat.level * 4);
      if (spawnIn <= 0) { spawnIn = 0.25 + Math.random() * 0.5; fire(Math.floor(Math.random() * nodes.length), 0); }
      if (beat.kick) { for (let k = 0; k < 6; k++) { const n = nodes[Math.floor(Math.random() * nodes.length)]; n.flash = 1; fire(nodes.indexOf(n), 2); } }
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
      // the rings and sparks
      for (const rg of rings) {
        ctx.strokeStyle = `rgba(200,255,255,${(0.55 * rg.life).toFixed(3)})`; ctx.lineWidth = 2 * rg.life + 0.5;
        ctx.shadowColor = "#3fe0e0"; ctx.shadowBlur = 18 * rg.life;
        ctx.beginPath(); ctx.arc(rg.x, rg.y, rg.r, 0, Math.PI * 2); ctx.stroke(); ctx.shadowBlur = 0;
      }
      for (const sp of sparks) {
        ctx.fillStyle = `rgba(230,255,255,${Math.min(1, sp.life * 2).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sp.x, sp.y, 1.4, 0, Math.PI * 2); ctx.fill();
      }
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
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mousedown", onDown);
      document.removeEventListener("mouseleave", onLeave);
    };
  }, [density, keepOut?.x, keepOut?.y, keepOut?.r]); // eslint-disable-line react-hooks/exhaustive-deps
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
      <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-[13px]">
        <Lamp health={row.health} />
        <span className="board-env">{env}</span>
        {unread ? (
          <span style={MUTED}>not read yet</span>
        ) : (
          <>
            <span style={{ color: HEALTH_COLOR[row.health], minWidth: 56, letterSpacing: 1 }} title={row.health === "ok" ? "The service answers and says it is healthy" : row.health}>{HEALTH_LABEL[row.health]}</span>
            <Pill
              text={row.drift === "behind" && row.behind_by != null ? `${row.behind_by} commit${row.behind_by === 1 ? "" : "s"} behind GitHub` : DRIFT_LABEL[row.drift]}
              color={DRIFT_COLOR[row.drift]}
              title={DRIFT_TITLE[row.drift] + (row.repo_commit ? ` GitHub is at ${row.repo_commit}.` : "")}
            />
            {api?.dirty && <Pill text="unsaved changes" color="var(--error)" title="Deployed from a folder with uncommitted changes - not exactly what is in GitHub" />}
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
            : <span className="board-chip dim" title="This deploy was not stamped with its commit. Deploys from HELIX are.">no version stamp</span>}
          {api.revision && <span className="font-mono">{api.revision}</span>}
          {api.deployed_at && <span>{when(api.deployed_at)}{api.deployed_by ? ` · ${api.deployed_by}` : ""}</span>}
          {api.db && <span>db <span style={{ color: "var(--text)" }}>{api.db}</span></span>}
        </div>
      )}
      {row.note && (
        <div className="text-[12px] mt-1 pl-6" style={{ color: row.needs_attention || api?.dirty ? "var(--working)" : "var(--muted)" }}>
          {row.note}
          {row.detail && (
            <details className="board-detail">
              <summary>what the tool said</summary>
              <pre>{row.detail}</pre>
            </details>
          )}
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

type Action = "dev" | "git" | "deploy";

function AppCard({ card, index, busy, queued, onRead, onAction, onLink, collapsed, onToggle }: { card: Card; index: number; busy: boolean; queued?: boolean; onRead: (app: string) => void; onAction: (app: string, a: Action) => void; onLink: (app: string) => void; collapsed?: boolean; onToggle?: () => void }) {
  const ref = useRef<HTMLElement | null>(null);
  // A PROTOTYPE: an app with no production environment. Marked in the header, never judged.
  const prototype = !card.envs.some((r) => r.env === "prod" && r.exists);
  const envsMissing = card.envs.filter((r) => !r.exists).map((r) => r.env);
  const attention = card.needs_attention;
  const unlinked = card.link ? !card.link.linked : false;
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
      className={`board-card materialize p-4${busy ? " reading" : ""}${attention ? " attention" : ""}${unlinked ? " unlinked" : ""}${collapsed ? " collapsed" : ""}${prototype ? " prototype" : ""}`}
      style={{ "--i": index * 3 } as React.CSSProperties}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      <i className="corners" aria-hidden="true" />
      <i className="trace" aria-hidden="true" />
      <i className="glare" aria-hidden="true" />
      <i className="scan" aria-hidden="true" />
      <div className="flex items-center gap-3 relative">
        <button className="board-fold" onClick={onToggle} title={collapsed ? "Expand" : "Collapse"} aria-label={collapsed ? "Expand" : "Collapse"}>{collapsed ? "▸" : "▾"}</button>
        <div>
          <div className="board-index">APP {String(index + 1).padStart(2, "0")}{prototype && <span className="board-proto" title={`No production environment - a prototype. ${envsMissing.length ? "Missing: " + envsMissing.join(", ").toUpperCase() + ". " : ""}Add environments from the gear when it is ready.`}>◇ PROTOTYPE</span>}</div>
          <div className="board-app"><Decoded text={card.app} /></div>
        </div>
        {attention && <Pill text="needs a look" color="var(--working)" />}
        {collapsed && live.length > 0 && <span className="text-[11px]" style={MUTED}>{up}/{live.length} running</span>}
        <div className="flex-1" />
        {live.length > 0 && <Ring ok={up} total={live.length} warn={attention || dirty} />}
        <button className="btn text-xs" disabled={busy || queued} onClick={() => onRead(card.app)} title="Read this app's environments now">
          {busy ? "Reading…" : queued ? "Queued…" : "Read"}
        </button>
        <button className={`board-gear${unlinked ? " spin" : ""}`} onClick={() => onLink(card.app)}
          title={unlinked ? `Not linked - ${card.link?.why || "set the repo"}` : "This project's settings: repo, branch, folder"} aria-label="Project settings">
          ⚙
        </button>
      </div>
      {!collapsed && <>
      {unlinked && card.link?.why && (
        <div className="board-unlinked-note">{card.link.why}</div>
      )}
      {/* THE FORGE's buttons: develop in a conversation, see the repo, ship it */}
      <div className="board-actions">
        <button className="act dev" onClick={() => onAction(card.app, "dev")} title="A conversation with HELIX about this project - the orb, scoped here">◉ Dev</button>
        <button className="act" onClick={() => onAction(card.app, "git")} title="The repo: what is committed, what is not, the lines of work">⎇ Git</button>
        <button className="act ship" onClick={() => onAction(card.app, "deploy")} title="Ship an environment, or roll one back">▲ Deploy</button>
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
          <EnvRow key={row.key} row={unlinked && row.note === card.link?.why ? { ...row, note: null } : row}
            thread={i < card.envs.length - 1 ? threadBetween(row, card.envs[i + 1]) : null} />
        ))}
        {prototype && envsMissing.length > 0 && (
          <div className="board-proto-note">A prototype: no {envsMissing.join(" or ").toUpperCase()} yet. When it is ready, add the missing environments from ⚙ - HELIX will spin them up in windy-celerity the fleet's way (that lane comes with Deploy).</div>
        )}
      </div>
      </>}
    </section>
  );
}

/** Does this card say the word? App, env, revision, commit, db, note, repo, flags - all searchable. */
function matches(card: Card, q: string): boolean {
  if (!q) return true;
  const hay = [card.app, card.repo || "",
    ...card.envs.flatMap((r) => [r.env, r.run_service || "", r.note || "", r.drift, r.health,
      r.api?.commit || "", r.api?.revision || "", r.api?.db || "", r.api?.deployed_by || "",
      r.api?.dirty ? "dirty unsaved" : "", r.api?.is_split ? "split" : "", r.api?.read_only ? "read-only" : "",
      r.api?.flags?.appCheckRequired ? "appcheck" : "", r.needs_attention ? "attention" : ""])]
    .join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

// ================================================================== the page

type Tab = "apps" | "tasks" | "agents" | "models" | "knowledge";
const TABS: [string, Tab][] = [["Apps", "apps"], ["Protocols", "tasks"], ["Agents", "agents"], ["Holograms", "models"], ["Vault", "knowledge"]];

/** The right-hand drawer THE FORGE opens for Git and Deploy. UI first (Brian, 2026-09-19): the
 *  reads are live where the fleet has them; every verb that would change a service is shown,
 *  named, and disabled until its lane is wired behind the domain's checks. */
function Drawer({ title, sub, onClose, children }: { title: string; sub?: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="board-drawer-wrap" onClick={onClose}>
      <aside className="board-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-3">
          <div>
            <div className="board-kicker">{sub}</div>
            <div className="board-app" style={{ fontSize: 17 }}>{title}</div>
          </div>
          <div className="flex-1" />
          <button className="btn text-xs" onClick={onClose}>✕</button>
        </div>
        {children}
      </aside>
    </div>
  );
}

// ================================================================== the git graph

interface GitCommit { sha: string; parents: string[]; subject: string; author: string; at: string }
interface GitDoc { repo: string; branch: string; branches: { name: string; sha: string }[]; commits: GitCommit[]; problem?: string | null }

/** Lanes for a list of commits, newest first: a commit takes the lane that was waiting for its
 *  sha (a child expected it), else a fresh one; its first parent inherits the lane, other parents
 *  open lanes. The same idea every git GUI draws. Returns lane per commit and the edges. */
function layoutGraph(commits: GitCommit[]) {
  const lanes: (string | null)[] = [];
  const laneOf = new Map<string, number>();
  const edges: { from: number; to: number; fromLane: number; toLane: number }[] = [];
  const rowOf = new Map<string, number>();
  commits.forEach((c, i) => rowOf.set(c.sha, i));
  commits.forEach((c, row) => {
    let lane = lanes.indexOf(c.sha);
    if (lane < 0) { lane = lanes.indexOf(null); if (lane < 0) { lane = lanes.length; lanes.push(null); } }
    laneOf.set(c.sha, lane);
    lanes[lane] = null;
    // any other lane also waiting for this sha (a merge child) joins here
    lanes.forEach((v, k) => { if (v === c.sha) { lanes[k] = null; } });
    c.parents.forEach((p, k) => {
      let pl: number;
      const existing = lanes.indexOf(p);
      if (existing >= 0) pl = existing;
      else if (k === 0) { pl = lane; lanes[lane] = p; }
      else { pl = lanes.indexOf(null); if (pl < 0) { pl = lanes.length; lanes.push(null); } lanes[pl] = p; }
      const prow = rowOf.get(p);
      if (prow !== undefined) edges.push({ from: row, to: prow, fromLane: lane, toLane: pl });
      else edges.push({ from: row, to: commits.length, fromLane: lane, toLane: pl });   // runs off the bottom
    });
  });
  return { laneOf, edges, width: Math.max(1, lanes.length) };
}

const LANE_COLORS = ["#3fe0e0", "#2a8cff", "#f0be5a", "#c86dff", "#5adf8a", "#ff7a7a", "#ffa94d"];

function GitGraph({ doc, serving }: { doc: GitDoc; serving: { env: string; commit: string }[] }) {
  const { laneOf, edges, width } = useMemo(() => layoutGraph(doc.commits), [doc]);
  const ROW = 34, COL = 18, LEFT = 14, TOP = 18;
  const gw = LEFT * 2 + COL * Math.max(width - 1, 0) + 8;
  const tips = new Map<string, string[]>();
  doc.branches.forEach((b) => { tips.set(b.sha, [...(tips.get(b.sha) || []), b.name]); });
  const served = new Map<string, string[]>();
  serving.forEach((s) => { const k = doc.commits.find((c) => c.sha.startsWith(s.commit))?.sha; if (k) served.set(k, [...(served.get(k) || []), s.env]); });
  const H = TOP + ROW * doc.commits.length;
  return (
    <div className="board-graph">
      <svg className="board-graph-svg" width={gw} height={H} style={{ flex: "none" }}>
        {edges.map((e, i) => {
          const x1 = LEFT + e.fromLane * COL, y1 = TOP + e.from * ROW, x2 = LEFT + e.toLane * COL, y2 = TOP + e.to * ROW;
          const col = LANE_COLORS[(e.fromLane === e.toLane ? e.toLane : e.toLane) % LANE_COLORS.length];
          const d = x1 === x2 ? `M${x1},${y1} L${x2},${y2}` : `M${x1},${y1} C${x1},${y1 + ROW * 0.6} ${x2},${y2 - ROW * 0.6} ${x2},${y2}`;
          return <path key={i} d={d} stroke={col} strokeWidth={2} fill="none" opacity={0.75} className="board-graph-edge" style={{ animationDelay: `${i * 30}ms` }} />;
        })}
        {doc.commits.map((c, i) => {
          const lane = laneOf.get(c.sha) ?? 0;
          const x = LEFT + lane * COL, y = TOP + i * ROW;
          const col = LANE_COLORS[lane % LANE_COLORS.length];
          const isTip = tips.has(c.sha), isServed = served.has(c.sha), merge = c.parents.length > 1;
          return (
            <g key={c.sha} className="board-graph-node" style={{ animationDelay: `${i * 40}ms` }}>
              {isServed && <circle cx={x} cy={y} r={9} fill="none" stroke="var(--gold)" strokeWidth={1.5} className="board-graph-served" />}
              <circle cx={x} cy={y} r={merge ? 4 : 5} fill={merge ? "var(--panel)" : col} stroke={col} strokeWidth={2} />
              {isTip && <circle cx={x} cy={y} r={2} fill="#fff" />}
            </g>
          );
        })}
      </svg>
      <div className="board-graph-rows">
        {doc.commits.map((c, i) => {
          const lane = laneOf.get(c.sha) ?? 0;
          const names = tips.get(c.sha) || [], envs = served.get(c.sha) || [];
          return (
            <div key={c.sha} className="board-graph-row" style={{ height: ROW, animationDelay: `${i * 40}ms` }}>
              <span className="board-chip mono" style={{ color: LANE_COLORS[lane % LANE_COLORS.length] }}>{c.sha.slice(0, 7)}</span>
              {names.map((n) => <span key={n} className={`board-ref${n === doc.branch ? " main" : ""}`}>⎇ {n}</span>)}
              {envs.map((e) => <span key={e} className={`board-ref served${e === "prod" ? " prod" : ""}`}>▲ {e}</span>)}
              <span className="elide board-graph-subject" title={c.subject}>{c.subject}</span>
              <span className="board-graph-meta">{c.author}{c.at ? ` · ${ago(c.at)}` : ""}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface ScanDoc { where: "folder" | "github"; folder: string | null; branch: string | null; files_scanned: number; files_skipped: number; high: number; medium: number; clean: boolean; findings: { path: string; line: number; kind: string; severity: string; excerpt: string }[] }

function GitDrawer({ card, onClose, onBoard }: { card: Card; onClose: () => void; onBoard?: (b: Board) => void }) {
  const head = card.envs.find((r) => r.repo_commit)?.repo_commit ?? null;
  const live = card.envs.filter((r) => r.exists && r.checked_at);
  const [doc, setDoc] = useState<GitDoc | null>(null);
  const [gitErr, setGitErr] = useState<string | null>(null);
  const [gen, setGen] = useState(0);
  const [switching, setSwitching] = useState(false);
  const [scan, setScan] = useState<ScanDoc | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanErr, setScanErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setDoc(null); setGitErr(null);
    void api.get<GitDoc>(`/api/fleet/git?app_name=${encodeURIComponent(card.app)}`)
      .then((d) => { if (alive) setDoc(d); })
      .catch((e: Error) => { if (alive) setGitErr(e.message); });
    return () => { alive = false; };
  }, [card.app, card.link?.repo, card.link?.branch, gen]);
  const serving = live.filter((r) => r.api?.commit).map((r) => ({ env: r.env, commit: r.api!.commit! }));
  // THE BRANCH: pick the one the board reads and (later) deploys from. Saved as the project link.
  const chooseBranch = async (name: string) => {
    if (!doc || name === doc.branch) return;
    setSwitching(true);
    try {
      const b = await api.put<Board>("/api/fleet/link", { app: card.app, repo: card.link?.custom ? card.link.repo : "", branch: name, folder: card.link?.folder || "" });
      onBoard?.(b);
      setScan(null);
      setGen((g) => g + 1);
    } catch (e) { setGitErr((e as Error).message); } finally { setSwitching(false); }
  };
  const runScan = async () => {
    setScanning(true); setScanErr(null);
    try { setScan(await api.post<ScanDoc>("/api/fleet/scan", { app: card.app, branch: doc?.branch })); }
    catch (e) { setScanErr((e as Error).message); } finally { setScanning(false); }
  };
  return (
    <Drawer title={card.app} sub="GIT · the repo" onClose={onClose}>
      <div className="space-y-4 text-[13px]">
        <div className="board-panel">
          <div className="board-kicker mb-1">NOW</div>
          <div className="flex items-center gap-3 flex-wrap">
            <span className="board-chip dim">{card.repo}</span>
            {doc && doc.branches.length > 0 ? (
              <label className="board-branch" title="The branch this app is read from - and deploys from once the lane is wired">
                <span>⎇</span>
                <select value={doc.branch} disabled={switching} onChange={(e) => void chooseBranch(e.target.value)}>
                  {doc.branches.map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
                  {!doc.branches.some((b) => b.name === doc.branch) && <option value={doc.branch}>{doc.branch}</option>}
                </select>
                {switching && <span style={MUTED}>switching…</span>}
              </label>
            ) : (
              <span className="board-chip dim">⎇ {card.envs[0]?.branch ?? "main"}</span>
            )}
            {head ? <span className="board-chip">HEAD {head}</span> : <span style={MUTED}>HEAD not read yet</span>}
          </div>
          <div className="mt-2" style={MUTED}>Working tree, staged files and the commit box read from the clone on this PC - wired with the deploy lane.</div>
        </div>
        <div className="board-panel">
          <div className="board-kicker mb-1">SERVING</div>
          {live.length === 0 && <div style={MUTED}>Nothing read yet - press Read on the card.</div>}
          {live.map((r) => (
            <div key={r.key} className="flex items-center gap-3 py-1">
              <span className="board-env" style={{ color: r.env === "prod" ? "var(--gold)" : "var(--text)" }}>{r.env.toUpperCase()}</span>
              {r.api?.commit ? <span className="board-chip">{r.api.commit}{r.api.dirty ? " + unsaved changes" : ""}</span> : <span className="board-chip dim">no version stamp</span>}
              <span style={MUTED}>{r.drift === "behind" && r.behind_by != null ? `${r.behind_by} behind GitHub` : DRIFT_LABEL[r.drift]}</span>
            </div>
          ))}
        </div>
        <div className="board-panel">
          <div className="board-kicker mb-1">HISTORY · LINES OF WORK</div>
          {doc === null && gitErr === null && <div style={MUTED}>Reading the graph from GitHub…</div>}
          {gitErr && <div style={{ color: "var(--error)" }}>{gitErr}</div>}
          {doc && doc.commits.length === 0 && <div style={MUTED}>{doc.problem || "No commits came back."}</div>}
          {doc && doc.commits.length > 0 && (
            <>
              <div className="flex items-center gap-2 flex-wrap mb-2 text-[12px]" style={MUTED}>
                <span>{doc.commits.length} commits · {doc.branches.length} branch{doc.branches.length === 1 ? "" : "es"}</span>
                <span>· ⎇ a branch tip · ▲ what an environment is serving · hollow = merge</span>
              </div>
              <GitGraph doc={doc} serving={serving} />
            </>
          )}
        </div>
        <div className="board-panel">
          <div className="flex items-center gap-3 flex-wrap">
            <div className="board-kicker">SECRETS SCAN</div>
            <div className="flex-1" />
            <button className="btn btn-primary text-xs" disabled={scanning} onClick={() => void runScan()}>{scanning ? "Scanning…" : "⌕ Scan this branch"}</button>
          </div>
          <div className="mt-1" style={MUTED}>Keys, tokens, private keys, passwords in connection strings, committed .env and key files - {card.link?.folder ? "read from the linked folder on this PC (git-tracked files only)" : "read from GitHub"}. Values are masked; nothing leaves this PC.</div>
          {scanErr && <div className="mt-2" style={{ color: "var(--error)" }}>{scanErr}</div>}
          {scan && (
            <div className="mt-3">
              <div className="flex items-center gap-2 flex-wrap">
                {scan.clean
                  ? <Pill text="clean" color="var(--done)" />
                  : <><Pill text={`${scan.high} must fix`} color="var(--error)" />{scan.medium > 0 && <Pill text={`${scan.medium} look at`} color="var(--working)" />}</>}
                <span style={MUTED}>{scan.files_scanned} files read · {scan.files_skipped} skipped · {scan.where === "folder" ? scan.folder : `${doc?.repo} @ ${scan.branch}`}</span>
              </div>
              {scan.findings.length > 0 && (
                <div className="board-scan">
                  {scan.findings.map((f, i) => (
                    <div key={i} className={`board-scan-row ${f.severity}`}>
                      <span className="board-scan-kind">{f.kind}</span>
                      <span className="board-chip mono">{f.path}{f.line ? `:${f.line}` : ""}</span>
                      <span className="board-scan-ex">{f.excerpt}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <a className="btn text-xs" href={`https://github.com/${card.repo}`} target="_blank" rel="noreferrer noopener">Open on GitHub ↗</a>
        </div>
      </div>
    </Drawer>
  );
}

function DeployDrawer({ card, onClose }: { card: Card; onClose: () => void }) {
  const [env, setEnv] = useState<"dev" | "qa" | "prod">("dev");
  const row = card.envs.find((r) => r.env === env);
  const checks: { ok: boolean | null; text: string }[] = [
    { ok: row?.exists ?? false, text: row?.exists ? "The service exists on Cloud Run" : "No service in this environment - never created from here (rule 5)" },
    { ok: row?.checked_at ? row.health === "ok" : null, text: row?.checked_at ? `Running ${row.api?.revision ?? "?"} - ${HEALTH_LABEL[row.health]}` : "Not read yet" },
    { ok: row?.api?.commit ? !row.api.dirty : null, text: row?.api?.dirty ? "What is serving was built from a dirty tree" : row?.api?.commit ? `Serving commit ${row.api.commit}` : "Commit not recorded on the serving revision" },
    { ok: true, text: card.app === "MES" ? "Env vars: backend/deploy.ps1 is the only source (rule 2)" : "No --set-env-vars, --vpc-connector or --service-account will be passed (rule 1)" },
    { ok: env === "prod" ? null : true, text: env === "prod" ? "Production: allowlisted identity + a second person's confirm + an audit row (§10.2)" : "Dev / QA: your gcloud login is enough" },
  ];
  return (
    <Drawer title={card.app} sub="DEPLOY · ship or roll back" onClose={onClose}>
      <div className="space-y-4 text-[13px]">
        <div className="flex gap-2">
          {(["dev", "qa", "prod"] as const).map((e) => (
            <button key={e} className={`board-envpick${env === e ? " on" : ""}${e === "prod" ? " prod" : ""}`} onClick={() => setEnv(e)}>
              {e.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="board-panel">
          <div className="board-kicker mb-2">PRE-FLIGHT</div>
          {checks.map((c, i) => (
            <div key={i} className="flex items-start gap-2 py-1">
              <span style={{ color: c.ok === true ? "var(--done)" : c.ok === false ? "var(--error)" : "var(--muted)", width: 14 }}>{c.ok === true ? "●" : c.ok === false ? "●" : "○"}</span>
              <span>{c.text}</span>
            </div>
          ))}
        </div>
        <div className="board-panel">
          <div className="board-kicker mb-1">WHAT SHIPS</div>
          <div style={MUTED}>The working tree of the clone on this PC, as <span className="board-chip dim">gcloud run deploy --source .</span> with a <span className="board-chip dim">version=&lt;sha&gt;</span> label, then the Hosting site - the wrapped <span className="board-chip dim">dev.ps1</span> flow, never rewritten.</div>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <button className="btn btn-primary" disabled title="Wired next round, behind check_deploy and the four production conditions">▲ Deploy {env.toUpperCase()}</button>
          <button className="btn" disabled title="A traffic shift to a revision that served - no build, no flags">↶ Roll back</button>
          <span className="text-xs" style={{ color: "var(--working)" }}>UI only - the lane is wired after this layout is signed off</span>
        </div>
      </div>
    </Drawer>
  );
}

/** Add a project: THE FORGE's new-project wizard, three doors. UI first. */
/** THE GEAR: where this project lives. Repo + branch (what the board reads and, later, deploys
 *  from), the folder it is worked on from, and the one GitHub token the whole board uses - with
 *  a button straight to GitHub's token page so "get a token" is one click, not a search. */
function LinkProject({ card, onClose, onSaved }: { card: Card; onClose: () => void; onSaved: (b: Board) => void }) {
  const link = card.link;
  const [repo, setRepo] = useState(link?.custom ? link.repo || "" : "");
  const [branch, setBranch] = useState(link?.custom ? link.branch || "" : "");
  const [folder, setFolder] = useState(link?.folder || "");
  const [token, setToken] = useState("");
  const [tokenSet, setTokenSet] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    void api.get<{ secrets?: Record<string, boolean> }>("/api/settings").then((d) => setTokenSet(Boolean(d.secrets?.github_token))).catch(() => setTokenSet(null));
  }, []);
  const save = async () => {
    setSaving(true); setError(null);
    try {
      if (token.trim()) {
        await api.put("/api/settings", { values: { github_token: token.trim() } });
        window.dispatchEvent(new CustomEvent("helix-settings-saved"));
      }
      const b = await api.put<Board>("/api/fleet/link", { app: card.app, repo, branch, folder });
      onSaved(b);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const noToken = tokenSet === false && !token.trim();
  return (
    <div className="board-drawer-wrap" onClick={onClose}>
      <div className="board-modal" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div>
            <div className="board-kicker">PROJECT SETTINGS</div>
            <div className="board-app" style={{ fontSize: 17 }}>{card.app}</div>
          </div>
          <div className="flex-1" />
          {link && (link.linked
            ? <Pill text="linked" color="var(--done)" />
            : <Pill text="not linked" color="var(--error)" />)}
          <button className="btn text-xs" onClick={onClose}>✕</button>
        </div>
        {link && !link.linked && link.why && <div className="board-unlinked-note mb-4">{link.why}</div>}

        <div className="board-form">
          <label>
            <span>GitHub repo</span>
            <input placeholder={link?.repo ? `${link.repo}  (the fleet's default)` : "owner/name"} value={repo} onChange={(e) => setRepo(e.target.value)} />
            <em>owner/name or the full URL. Leave empty to use the fleet's default.</em>
          </label>
          <label>
            <span>Branch</span>
            <input placeholder={link?.branch || "main"} value={branch} onChange={(e) => setBranch(e.target.value)} />
            <em>What each environment is supposed to deploy from.</em>
          </label>
          <label>
            <span>Folder on this PC</span>
            <input placeholder="C:\Users\you\Desktop\MyApp" value={folder} onChange={(e) => setFolder(e.target.value)} />
            <em>Where you work on it. Dev opens here once the lanes are wired.</em>
          </label>
          <label className={noToken ? "warn" : ""}>
            <span>GitHub token <small>{tokenSet === null ? "" : tokenSet ? "· set" : "· not set"}</small></span>
            <div className="flex gap-2">
              <input type="password" placeholder={tokenSet ? "leave empty to keep the current one" : "ghp_… or github_pat_…"} value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
              <button type="button" className="btn text-xs shrink-0" onClick={() => window.open(TOKEN_URL, "_blank", "noopener")} title="Opens GitHub's token page with the repo scope pre-ticked">⎇ Get a token ↗</button>
            </div>
            <em>One token for the whole board, kept in HELIX's settings, never shown again. Classic token, <b>repo</b> scope - it must be able to see Brendan's and Alex's repos too, so a fine-grained token limited to your own repos is not enough.</em>
          </label>
        </div>

        {error && <div className="text-[13px] mt-3" style={{ color: "var(--error)" }}>{error}</div>}
        <div className="flex items-center gap-3 mt-5">
          <button className="btn btn-primary" disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : "Save and read"}</button>
          <button className="btn" onClick={onClose}>Cancel</button>
          <span className="text-xs ml-auto" style={MUTED}>saved to this PC's HELIX settings</span>
        </div>
      </div>
    </div>
  );
}

function AddProject({ onClose }: { onClose: () => void }) {
  const [door, setDoor] = useState<"folder" | "github" | "new" | null>(null);
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [envs, setEnvs] = useState<"dev" | "dev+qa" | "all">("dev");
  return (
    <div className="board-drawer-wrap" onClick={onClose}>
      <div className="board-modal" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div>
            <div className="board-kicker">NEW ON THE CONSOLE</div>
            <div className="board-app" style={{ fontSize: 17 }}>ADD A PROJECT</div>
          </div>
          <div className="flex-1" />
          <button className="btn text-xs" onClick={onClose}>✕</button>
        </div>
        <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
          {([
            ["folder", "▣", "A folder on this PC", "An app that already lives on disk. HELIX reads it in place."],
            ["github", "⎇", "Clone from GitHub", "Paste the repo. HELIX clones it beside the others and reads it."],
            ["new", "✦", "A new app", "Start from the fleet's layout. UI first - you approve the look, then the logic."],
          ] as const).map(([k, ic, t, d]) => (
            <button key={k} className={`board-door${door === k ? " on" : ""}`} onClick={() => setDoor(k)}>
              <div className="text-[22px]" style={{ color: "var(--cyan)" }}>{ic}</div>
              <div className="font-semibold mt-1">{t}</div>
              <div className="text-[12px] mt-1" style={MUTED}>{d}</div>
            </button>
          ))}
        </div>
        {door === "folder" && (
          <div className="mt-4 space-y-2 text-[13px]">
            <input className="w-full" placeholder="C:\Users\you\Desktop\MyApp" value={path} onChange={(e) => setPath(e.target.value)} />
            <div style={MUTED}>A native folder picker replaces the typed path once the backend side lands.</div>
          </div>
        )}
        {door === "github" && (
          <div className="mt-4 space-y-2 text-[13px]">
            <input className="w-full" placeholder="https://github.com/Alex-Mark1/WMS_V1" value={url} onChange={(e) => setUrl(e.target.value)} />
            <div style={MUTED}>Cloned with your GitHub token (Settings → The Board) into the projects folder.</div>
          </div>
        )}
        {door === "new" && (
          <div className="mt-4 space-y-3 text-[13px]">
            <input className="w-full" placeholder="App name, e.g. QC Lab" value={name} onChange={(e) => setName(e.target.value)} />
            <div>
              <div className="board-kicker mb-1">ENVIRONMENTS ON CREATION</div>
              <div className="flex gap-2 flex-wrap">
                {([["dev", "DEV only - a prototype", "One environment to build in. Marked PROTOTYPE on the card; add QA and PROD later from the gear."], ["dev+qa", "DEV + QA", "Build and test; production comes when it earns it."], ["all", "DEV + QA + PROD", "The fleet's full layout from day one."]] as const).map(([k, label, hint]) => (
                  <button key={k} className={`board-envpick${envs === k ? " on" : ""}`} title={hint} onClick={() => setEnvs(k)}>{label}</button>
                ))}
              </div>
            </div>
            <div style={MUTED}>Vite + Flask + one Cloud Run service and one Hosting site per environment, in windy-celerity - the fleet's layout, spun up by HELIX. The GitHub repo is created <b>private</b>, always.</div>
          </div>
        )}
        <div className="flex items-center gap-3 mt-5">
          <button className="btn btn-primary" disabled={!door} title="Wired next round">Add</button>
          <span className="text-xs" style={{ color: "var(--working)" }}>UI only this round</span>
        </div>
      </div>
    </div>
  );
}

export default function ConsolePage() {
  const navigate = useHelix((s) => s.navigate);
  const [tab, setTab] = useState<Tab>("apps");
  const [drawer, setDrawer] = useState<{ app: string; kind: Action } | null>(null);
  const [adding, setAdding] = useState(false);
  const [linking, setLinking] = useState<string | null>(null);
  // what is folded is remembered on this PC
  const [folded, setFolded] = useState<Record<string, boolean>>(() => { try { return JSON.parse(localStorage.getItem("helix_console_folds") || "{}"); } catch { return {}; } });
  const fold = (k: string) => setFolded((f) => { const n = { ...f, [k]: !f[k] }; try { localStorage.setItem("helix_console_folds", JSON.stringify(n)); } catch { /* no storage */ } return n; });
  const [collapsedCards, setCollapsedCardsState] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem("helix_console_cards") || "[]"); } catch { return []; } });
  const setCollapsedAll = (v: string[]) => { setCollapsedCardsState(v); try { localStorage.setItem("helix_console_cards", JSON.stringify(v)); } catch { /* no storage */ } };
  const toggleCard = (app: string) => setCollapsedAll(collapsedCards.includes(app) ? collapsedCards.filter((a) => a !== app) : [...collapsedCards, app]);
  const [colors, setColors] = useState<[string, string]>(["#3fe0e0", "#2a8cff"]);
  useEffect(() => {
    void api.get<{ values?: Record<string, unknown> }>("/api/settings").then((d) => {
      const a = String(d.values?.helix_color_a || ""), b = String(d.values?.helix_color_b || "");
      if (/^#[0-9a-f]{6}$/i.test(a) || /^#[0-9a-f]{6}$/i.test(b)) setColors([a || "#3fe0e0", b || "#2a8cff"]);
    }).catch(() => undefined);
  }, []);
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

  // READS ARE QUEUED, NEVER DROPPED: the backend reads one thing at a time (a dozen gcloud calls
  // each), so a second Read while one runs used to bounce off with "busy" and look like nothing
  // happened. Now every Read joins a line and runs in turn; the card shows "queued" meanwhile.
  const queue = useRef<(string | undefined)[]>([]);
  const running = useRef(false);
  const [queued, setQueued] = useState<string[]>([]);
  const pump = useCallback(() => {
    if (running.current) return;
    if (queue.current.length === 0) { setQueued([]); return; }
    const app = queue.current.shift();
    running.current = true;
    setBusy(app || "*");
    setQueued([...queue.current].map((a) => a || "*"));
    void api.post<Board>("/api/fleet/refresh", app ? { app } : {})
      .then((b) => { setBoard(b); setFailed(null); })
      .catch((e: Error) => setFailed(e.message))
      .finally(() => { running.current = false; setBusy(null); if (queue.current.length) pump(); else setQueued([]); });
  }, []);
  const refresh = useCallback((app?: string) => {
    if (queue.current.includes(app) || (running.current && busy === (app || "*"))) return;
    queue.current.push(app);
    pump();
  }, [pump, busy]);
  // AUTO-READ: opening the Console with a board older than ten minutes (or never read) reads the
  // whole company - you should never land on "not read yet".
  const autoRead = useRef(false);
  useEffect(() => {
    if (!board || autoRead.current) return;
    autoRead.current = true;
    const at = board.companies[0]?.checked_at ? Date.parse(board.companies[0].checked_at) : 0;
    const unread = (board.companies[0]?.apps ?? []).some((c) => c.envs.some((r) => r.exists && !r.checked_at));
    if (!at || Date.now() - at > 10 * 60 * 1000 || unread) refresh();
  }, [board, refresh]);
  // and again every ten minutes while the Console is open
  useEffect(() => { const id = window.setInterval(() => refresh(), 10 * 60 * 1000); return () => window.clearInterval(id); }, [refresh]);

  const company = board?.companies[0] ?? null;
  const cards = useMemo(() => (company?.apps ?? []).filter((c) => matches(c, q)), [company, q]);
  const notReady = (board?.readiness ?? []).filter((r) => !r.ok);
  const cells = (company?.apps ?? []).flatMap((c) => c.envs).filter((r) => r.exists);
  const read = cells.filter((r) => r.checked_at);
  const up = read.filter((r) => r.health === "ok").length;
  const look = (company?.apps ?? []).filter((c) => c.needs_attention).length
    + read.filter((r) => r.api?.dirty && !r.needs_attention).length;
  const title = useDecode("CONSOLE", 22, 45);

  return (
    <div className="board-stage" style={{ pointerEvents: "auto" }}>
      <HelixLayer colors={colors} />
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
              <div className="board-kicker">{company?.label ?? "the company"} · everything you build and run</div>
              <div className="font-display font-bold flex items-center gap-3">
                <span className="text-glow-cyan text-[22px]" style={{ color: "var(--cyan)" }}>⬡</span>
                <span className="board-title">{title}</span>
              </div>
            </div>
            <div className="flex-1" />
            <div className="flex flex-col items-end gap-1.5">
              <div className="flex items-center gap-2">
                <button className="btn btn-primary text-xs" disabled={busy !== null} onClick={() => refresh()}>
                  {busy === "*" ? "Reading the fleet…" : "⟳ Refresh"}
                </button>
                <button className="btn btn-primary text-xs" onClick={() => setAdding(true)}>＋ Add a project</button>
              </div>
              <div className="text-[11px] tracking-wider" style={MUTED} title={`${cells.length} cells, ${read.length ? up : "-"} up, ${read.length ? look : "-"} need a look`}>
                {company?.checked_at ? `last read ${ago(company.checked_at)}` : "not read yet"} · {read.length ? `${up}/${read.length} up` : `${cells.length} cells`}{look > 0 ? ` · ${look} need a look` : ""}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1 flex-wrap">
            {TABS.map(([label, key]) => (
              <button key={key} className={`board-tab${tab === key ? " on" : ""}`} onClick={() => setTab(key)}>{label}</button>
            ))}
          </div>
          {tab === "apps" && (
            <div className="text-[13px] max-w-[820px]" style={MUTED}>
              Every app the company runs, in every environment: what is serving, which commit, from which
              database, and whether the repo has moved on. Read from Cloud Run and each app's own health
              endpoint — nothing here is guessed. Dev opens the orb on a project; Git and Deploy are THE FORGE's lanes.
            </div>
          )}
          {tab !== "apps" && <Menu tab={tab} embedded />}

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

          {tab === "apps" && <>
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

          {/* PROJECTS - the first section of many: Slack, Google, Listeners join it as HELIX grows */}
          <section className={`board-section${folded.projects ? " folded" : ""}`}>
            <button className="board-section-head" onClick={() => fold("projects")}>
              <span className="board-section-chev">{folded.projects ? "▸" : "▾"}</span>
              <span className="board-section-title">PROJECTS</span>
              <span className="board-section-sub">{cards.length} app{cards.length === 1 ? "" : "s"} · {read.length}/{cells.length} cells read{look > 0 ? ` · ${look} need a look` : ""}</span>
              <span className="flex-1" />
              <span className="board-section-tools" onClick={(e) => e.stopPropagation()}>
                <button className="btn-nav text-xs" onClick={() => setCollapsedAll(cards.map((c) => c.app))}>Collapse all</button>
                <button className="btn-nav text-xs" onClick={() => setCollapsedAll([])}>Expand all</button>
              </span>
            </button>
            {!folded.projects && (
              <div className="grid gap-4 mt-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
                {cards.map((card, i) => (
                  <AppCard key={card.app} card={card} index={i} busy={busy === card.app || busy === "*"} queued={queued.includes(card.app) || queued.includes("*")} onRead={(app) => refresh(app)}
                    collapsed={collapsedCards.includes(card.app)} onToggle={() => toggleCard(card.app)}
                    onLink={(app) => setLinking(app)}
                    onAction={(app, kind) => kind === "dev" ? navigate({ name: "talk", project: app }) : setDrawer({ app, kind })} />
                ))}
              </div>
            )}
          </section>
          <section className="board-section soon">
            <div className="board-section-head" style={{ cursor: "default" }}>
              <span className="board-section-chev">▸</span>
              <span className="board-section-title">SLACK · GOOGLE · LISTENERS</span>
              <span className="board-section-sub">the next sections: the company's channels, calendar and mail, and scheduled listeners that watch and act - coming as HELIX grows into the business</span>
            </div>
          </section>
          {company && (
            <div className="mt-2">
              <div className="board-kicker mb-2">LOCAL BUILDS · what HELIX made on this PC</div>
              <Menu tab="apps" embedded />
            </div>
          )}
          </>}
        </div>
      </div>
      {drawer && (() => { const c = company?.apps.find((x) => x.app === drawer.app); if (!c) return null;
        return drawer.kind === "git" ? <GitDrawer card={c} onClose={() => setDrawer(null)} onBoard={(b) => setBoard(b)} /> : <DeployDrawer card={c} onClose={() => setDrawer(null)} />; })()}
      {adding && <AddProject onClose={() => setAdding(false)} />}
      {linking && (() => { const c = company?.apps.find((x) => x.app === linking); if (!c) return null;
        return <LinkProject card={c} onClose={() => setLinking(null)} onSaved={(b) => { setBoard(b); setLinking(null); refresh(c.app); }} />; })()}
    </div>
  );
}
