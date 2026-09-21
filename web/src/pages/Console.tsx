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
import StatusFace, { STATUS_LABEL, type FaceStatus } from "../components/StatusFace";
import { TasksSection } from "../components/Tasks";
import { VaultCard } from "../components/Vault";
import Strandbar from "../components/Strandbar";
import { useTaskCounts } from "../lib/jobs";
import { bendEvery, density, dprCap, frameMs2D, glows, loop, perf } from "../lib/perf";
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
  served?: string[];
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
function HelixScene({ pointer, colors, tempo = 1, spinRate = 1, bendable = true }: { pointer: React.MutableRefObject<{ x: number; y: number; vx: number; vy: number }>; colors: [string, string]; tempo?: number; spinRate?: number; bendable?: boolean }) {
  const group = useRef<THREE.Group>(null!);
  const frameNo = useRef(0);
  const surfPos = useRef(0);      // the strands' travel along their own axis, integrated (never a jump when the speed changes)
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
    // THE TURN about its own long axis: quicker than it was, quicker still in the background
    // (spinRate), and it breathes - slow, then fast, then slow again over ~14 s
    const breathe = 0.55 + 0.45 * Math.sin(t * 0.45 + spinRate);
    g.rotation.y += dt * (0.22 + 0.5 * breathe) * spinRate + spin.current * dt;
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
    spin.current += beat.level * beat.level * dt * 1.0 * breathe;
    g.rotation.y += dt * beat.level * 0.7 * breathe + (beat.kick ? 0.05 * breathe : 0);   // a song turns it; a kick nudges it
    for (const m of strandMats.current) { if (m) m.size = m.userData.base * (1 + 0.9 * flash); }
    // a slow breath, and a sway
    const breath = 1 + 0.025 * Math.sin(t * 0.6) + 0.04 * flash + 0.05 * beat.level;
    g.scale.setScalar(breath);
    g.position.y = 0.2 + 0.25 * Math.sin(t * 0.23);
    // the distant twin turns the other way, slower
    if (twin.current) { twin.current.rotation.y -= dt * 0.07 + spin.current * dt * 0.3; twin.current.rotation.z = -0.5 + 0.03 * Math.sin(t * 0.3); }
    // THE SURF: the strands slide along their own axis forever - one turn, then wrap, and the
    // faded ends hide the seam. Faster with the music.
    // The pace is integrated, and it OSCILLATES: slow, then fast, then slow (Brian: "the music helix
    // moves too fast - it should oscillate between slow and fast"); the music rides on top of the
    // breath instead of driving it flat out, and a kick gives one shove.
    if (surf.current) {
      const pace = (0.45 + 0.75 * breathe) * tempo * (1 + beat.level * 1.1 * breathe) + (beat.kick ? 0.6 : 0);
      surfPos.current = (surfPos.current + dt * pace) % TURN_H;
      surf.current.position.y = -surfPos.current;
    }
    // THE BEND: the cursor pushes the nodes it passes over; they ease out and spring back.
    // 1800 projections a frame: the far strands never bend (nobody can tell), and on a slow
    // machine the near one bends every 2nd or 3rd frame (the ease hides it).
    frameNo.current++;
    const sp = strands.getAttribute("position") as THREE.BufferAttribute;
    const n = HELIX.points;
    const B = bend;
    const doBend = bendable && frameNo.current % bendEvery() === 0;
    g.updateMatrixWorld();
    if (surf.current) surf.current.updateMatrixWorld();
    if (doBend) {
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
    }
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
export function HelixBackdrop({ colors, position = [0, 0, -6], scale = 0.9, tempo = 1, spinRate = 1, bendable = true }: { colors: [string, string]; position?: [number, number, number]; scale?: number; tempo?: number; spinRate?: number; bendable?: boolean }) {
  const pointer = usePointer();
  return (
    <group position={position} scale={scale}>
      <HelixScene pointer={pointer} colors={colors} tempo={tempo} spinRate={spinRate} bendable={bendable} />
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
  const level = usePerfLevel();
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
      <Canvas dpr={[1, dprCap()]} key={level} camera={{ fov: 48, position: [0, 0, 9.5] }} gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
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
const density_ = density;
/** The governor's level as React state, so a canvas can re-seed when the machine turns out slow. */
export function usePerfLevel(): string {
  const [level, setLevel] = useState(perf.level);
  useEffect(() => { const on = () => setLevel(perf.level); window.addEventListener("helix-perf", on); return () => window.removeEventListener("helix-perf", on); }, []);
  return level;
}
export function NeuralLayer({ density = 1, keepOut, follow, depth = false, breach = false }: { density?: number; keepOut?: { x: number; y: number; r: number } | null; follow?: React.MutableRefObject<{ x: number; y: number; r: number } | null>; depth?: boolean; breach?: boolean } = {}) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const level = usePerfLevel();
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || REDUCED) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const N = Math.round(70 * density * density_());
    const LINK = 165;
    let due = 0;
    // THE 3D LATTICE (Brian, 2026-09-22): with a face in the net, the nodes crowd its rim and thin
    // out with distance, the near ones bigger and brighter, so the head reads as pushing through
    // the lattice toward you. `z` is a node's depth (1 = at the rim, in front; 0 = far back).
    // HERE'S JOHNNY (Brian, 2026-09-22): with `breach`, the net is a sparse membrane the head
    // breaks THROUGH - nodes in its way are thrown out past the rim with a spark, the links they
    // leave stretch white and snap, and the moment the head moves on or draws back they drift home
    // and the links knit again with a small flash. No shell crowds the rim, nothing cages the head.
    type Node = { x: number; y: number; vx: number; vy: number; r: number; p: number; flash: number; hx: number; hy: number; px: number; py: number; z: number; shell: boolean; inside: boolean; torn: boolean };
    const ko = () => (follow && follow.current) || keepOut || null;
    type Signal = { a: number; b: number; t: number; v: number; hops: number };
    let nodes: Node[] = [];
    let signals: Signal[] = [];
    let w = 0, h = 0, raf = 0, last = 0, alive = true, spawnIn = 0.3;
    // THE HAND IN THE NET: the cursor pushes nodes away (links stretch, then snap with a spark),
    // and a click sends a ring out that throws everything it crosses. Nodes drift home after.
    const mouse = { x: -9999, y: -9999, vx: 0, vy: 0, lx: -9999, ly: -9999 };
    const beatNow = { level: 0, kick: false, tick: 0 };
    type Ring = { x: number; y: number; r: number; v: number; life: number; hue?: string };
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
    // A TASK FINISHED (CURRENT TASKS): a wave rolls through the whole net from the middle - gold
    // when it is done, ember when it failed.
    const onWave = (e: Event) => {
      const kind = String((e as CustomEvent).detail?.kind || "done");
      const k = ko();
      const x = k ? k.x * w : w / 2, y = k ? k.y * h : h / 2;
      rings.push({ x, y, r: k ? k.r * Math.min(w, h) : 0, v: 640, life: 1, hue: kind === "done" ? "255,200,80" : "255,110,70" });
      window.setTimeout(() => rings.push({ x, y, r: k ? k.r * Math.min(w, h) : 0, v: 520, life: 0.8, hue: kind === "done" ? "255,220,140" : "255,140,100" }), 180);
      for (const n of nodes) if (Math.random() < 0.5) n.flash = Math.max(n.flash, 0.9);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("helix-wave", onWave);
    document.addEventListener("mouseleave", onLeave);

    const seed = () => {
      const k = ko();
      nodes = Array.from({ length: N }, (_, i) => {
        let x = Math.random() * w, y = Math.random() * h, z = 0.15 + Math.random() * 0.5;
        if (k && depth && !breach && i < N * 0.55) {
          // more than half the nodes crowd the rim: a shell 1.0..1.9 radii out, densest at the rim
          const kr = k.r * Math.min(w, h);
          const rad = kr * (1.02 + Math.pow(Math.random(), 1.8) * 0.9);
          const a = Math.random() * Math.PI * 2;
          x = k.x * w + Math.cos(a) * rad; y = k.y * h + Math.sin(a) * rad;
          z = 1 - (rad - kr) / (kr * 0.9);
        }
        return { x, y, hx: x, hy: y, px: 0, py: 0, z, shell: Boolean(k && depth && !breach && i < N * 0.55), inside: false, torn: false,
          vx: (Math.random() - 0.5) * 14 * (0.4 + 0.6 * z), vy: (Math.random() - 0.5) * 14 * (0.4 + 0.6 * z),
          r: 1.2 + Math.random() * 2.0, p: Math.random() * Math.PI * 2, flash: 0 };
      });
    };
    const size = () => {
      const parent = canvas.parentElement;
      const dpr = Math.min(1.25, window.devicePixelRatio || 1);   // a full-window 2D layer: 1.25x at most (2x was four times the pixels)
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
      // the governor's budget: 60 fps at full, 30 when lean, 20 when minimal
      if (t < due) { raf = requestAnimationFrame(step); return; }
      due = t + frameMs2D() - 1;
      const dt = Math.min(0.05, (t - last) / 1000 || 0);
      last = t;
      const glow = glows();
      const b0 = radioBeat();
      const keepOut = ko();
      beatNow.level = b0.level; beatNow.kick = b0.kick; beatNow.tick = t / 1000;
      if (b0.kick) rings.push({ x: keepOut ? keepOut.x * w : w / 2, y: keepOut ? keepOut.y * h : h / 2, r: keepOut ? keepOut.r * Math.min(w, h) : 0, v: 420 + b0.level * 400, life: 0.7 });   // a kick: a wave through the jelly
      const kx0 = keepOut ? keepOut.x * w : w / 2, ky0 = keepOut ? keepOut.y * h : h / 2, kr0 = keepOut ? keepOut.r * Math.min(w, h) : 0;
      // depth per node from where it is NOW relative to the rim (a roaming face re-ranks the net)
      if (depth && keepOut) for (const n of nodes) { const d = Math.hypot(n.x - kx0, n.y - ky0); n.z += ((d < kr0 * 1.9 ? 1 - Math.max(0, d - kr0) / (kr0 * 0.9) : 0.15) - n.z) * 0.04; }
      const zOf = (n: Node) => (depth && keepOut ? Math.max(0.12, Math.min(1, n.z)) : 1);
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
          if (breach) {
            // the head ARRIVES on a node: it is torn out of the sheet with a spark
            const inside = fd < kr * 1.05;
            if (inside && !n.inside) { n.flash = 1; n.torn = true; for (let q = 0; q < 2; q++) sparks.push({ x: n.x, y: n.y, vx: (fx / fd) * (140 + Math.random() * 120), vy: (fy / fd) * (140 + Math.random() * 120), life: 0.5 }); }
            n.inside = inside;
            // and HEALS when the head has gone and it is nearly home again: a small knit-flash
            if (n.torn && !inside && Math.hypot(n.px, n.py) < 14) { n.torn = false; n.flash = Math.max(n.flash, 0.55); }
          }
          // the rim breathes outward - the near shell swells toward you and settles, like a chest
          if (depth && fd > 0.01 && fd < kr * 2.2) { const sw = Math.sin(beatNow.tick * 0.9 + n.p) * 6 * n.z; n.px += (fx / fd) * sw * dt * 4; n.py += (fy / fd) * sw * dt * 4; }
          // the shell is the face's: when the head moves (the stage), its near nodes drift after it
          // and settle on its rim again, so the halo travels with the head
          if (n.shell && fd > 0.01) { const want = kr * (1.06 + 0.7 * ((n.p * 7) % 1)); const tx = kx + (fx / fd) * want, ty = ky + (fy / fd) * want; n.hx += (tx - n.hx) * Math.min(1, dt * 2.2); n.hy += (ty - n.hy) * Math.min(1, dt * 2.2); }
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
          const zl = 0.28 + 0.72 * (zOf(a) + zOf(b)) * 0.5;      // links between near nodes are the bright ones
          // a link under strain (either end pushed far from home) brightens white, then snaps
          const strain = Math.min(1, (Math.hypot(a.px, a.py) + Math.hypot(b.px, b.py)) / 90);
          if (strain > 0.85 && Math.random() < 0.08) {
            const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
            for (let q = 0; q < 3; q++) sparks.push({ x: mx, y: my, vx: (Math.random() - 0.5) * 160, vy: (Math.random() - 0.5) * 160, life: 0.45 });
            continue;
          }
          ctx.strokeStyle = strain > 0.4
            ? `rgba(${Math.round(63 + 190 * strain)},${Math.round(224 + 31 * strain)},255,${((0.3 * k + 0.5 * strain) * zl).toFixed(3)})`
            : `rgba(63,224,224,${((0.3 * k * k + 0.45 * lit * k) * zl).toFixed(3)})`;
          ctx.lineWidth = (lit > 0.3 || strain > 0.4 ? 1.4 : 1) * (0.7 + 0.6 * zl);
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
        if (glow) { ctx.shadowColor = "#3fe0e0"; ctx.shadowBlur = 10; }
        ctx.beginPath(); ctx.arc(x, y, 1.8, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
      }
      signals = keep;
      // the rings and sparks
      for (const rg of rings) {
        ctx.strokeStyle = `rgba(${rg.hue || "200,255,255"},${(0.55 * rg.life).toFixed(3)})`; ctx.lineWidth = 2 * rg.life + 0.5;
        if (glow) { ctx.shadowColor = rg.hue ? `rgb(${rg.hue})` : "#3fe0e0"; ctx.shadowBlur = 18 * rg.life; }
        ctx.beginPath(); ctx.arc(rg.x, rg.y, rg.r, 0, Math.PI * 2); ctx.stroke(); ctx.shadowBlur = 0;
      }
      for (const sp of sparks) {
        ctx.fillStyle = `rgba(230,255,255,${Math.min(1, sp.life * 2).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sp.x, sp.y, 1.4, 0, Math.PI * 2); ctx.fill();
      }
      // nodes: a slow twinkle, and a flash when a signal lands
      for (const n of nodes) {
        const z = zOf(n);
        const glow = (0.6 + 0.35 * Math.sin(n.p) + n.flash * 0.6) * (0.3 + 0.7 * z);
        const r = (n.r + n.flash * 2.5) * (0.55 + 1.15 * z);
        if (glow && n.flash > 0.3) { ctx.shadowColor = "#3fe0e0"; ctx.shadowBlur = 14 * n.flash; }   // only a landing signal glows; the near shell reads by size and light
        ctx.fillStyle = `rgba(${Math.round(63 + 160 * n.flash + 60 * z)},${Math.round(224 + 31 * n.flash)},${Math.round(224 + 31 * n.flash)},${Math.min(1, glow).toFixed(3)})`;
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
      window.removeEventListener("helix-wave", onWave);
      document.removeEventListener("mouseleave", onLeave);
    };
  }, [density, keepOut?.x, keepOut?.y, keepOut?.r, depth, level]); // eslint-disable-line react-hooks/exhaustive-deps
  if (REDUCED) return null;
  return <div className="board-layer board-mesh"><canvas ref={ref} aria-hidden="true" /></div>;
}

// ================================================================== the rail

/** THE RAIL (Brian, 2026-09-22): a narrow menu on the left with the page's sections and what is in
 *  each - click to jump. It reads the DOM ids the sections carry (sec-*, app-*). Folds to a strip. */
function Rail({ cards, tasks }: { cards: Card[]; tasks: { running: number; done: number; failed: number } }) {
  const [open, setOpen] = useState<boolean>(() => { try { return localStorage.getItem("helix_rail") !== "0"; } catch { return true; } });
  const jump = (id: string) => { const el = document.getElementById(id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); };
  const toggle = () => setOpen((o) => { try { localStorage.setItem("helix_rail", o ? "0" : "1"); } catch { /* fine */ } return !o; });
  const face = (c: Card): FaceStatus => c.envs.some((r) => r.exists && r.checked_at && r.health === "down") ? "down" : c.needs_attention ? "degraded" : c.envs.some((r) => r.exists && !r.checked_at) ? "unknown" : c.envs.some((r) => r.exists) ? "ok" : "absent";
  return (
    <nav className={`board-rail${open ? " open" : ""}`} aria-label="Sections">
      <button className="board-rail-toggle" onClick={toggle} data-tip={open ? "Fold the rail" : "Sections"}>{open ? "‹" : "›"}</button>
      {open && (
        <>
          <button className="board-rail-sec" onClick={() => jump("sec-tasks")}>
            <span className="board-rail-name">TASKS</span>
            <span className="board-rail-sub">{tasks.running ? <b className="run">{tasks.running} running</b> : "quiet"}{tasks.done ? <> · <b className="gold">{tasks.done}</b></> : null}{tasks.failed ? <> · <b className="red">{tasks.failed}</b></> : null}</span>
          </button>
          <button className="board-rail-sec" onClick={() => jump("sec-projects")}>
            <span className="board-rail-name">PROJECTS</span>
            <span className="board-rail-sub">{cards.length} apps</span>
          </button>
          <div className="board-rail-items">
            {cards.map((c) => (
              <button key={c.app} className="board-rail-item" onClick={() => jump(`app-${c.app}`)}>
                <StatusFace status={face(c)} size={16} />
                <span>{c.app}</span>
                {!c.envs.some((r) => r.env === "prod" && r.exists) && <i className="board-rail-proto" title="prototype">◇</i>}
              </button>
            ))}
          </div>
          <button className="board-rail-sec dim" onClick={() => jump("sec-next")}>
            <span className="board-rail-name">SLACK · GOOGLE</span>
            <span className="board-rail-sub">coming</span>
          </button>
          <button className="board-rail-sec" onClick={() => jump("sec-builds")}>
            <span className="board-rail-name">BUILDS</span>
            <span className="board-rail-sub">on this PC</span>
          </button>
        </>
      )}
    </nav>
  );
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


function Pill({ text, color, title }: { text: string; color: string; title?: string }) {
  return (
    <span className="board-pill" title={title}
      style={{ color, borderColor: `color-mix(in srgb, ${color} 45%, transparent)`, background: `color-mix(in srgb, ${color} 7%, transparent)` }}>
      {text}
    </span>
  );
}

/** The ring on a card: how many of its live environments are up. */


// ================================================================== rows + cards

type Thread = "same" | "changed" | "unknown" | null;

function EnvRow({ row, thread, busy, queued, onCreate }: { row: Row; thread: Thread; busy?: boolean; queued?: boolean; onCreate?: () => void }) {
  const env = row.env.toUpperCase();
  const api = row.api;
  const unread = row.exists && !row.checked_at;
  const [why, setWhy] = useState(false);
  const status: FaceStatus = !row.exists ? "absent" : busy ? "reading" : queued ? "queued" : unread ? "unknown" : row.health === "ok" ? "ok" : row.health === "degraded" ? "degraded" : row.health === "down" ? "down" : "unknown";
  const words: string[] = [];
  if (row.exists && row.checked_at) {
    words.push(`${env} is ${STATUS_LABEL[status]}${api?.revision ? ` on ${api.revision}` : ""}${api?.deployed_at ? `, deployed ${when(api.deployed_at)}${api.deployed_by ? ` by ${api.deployed_by}` : ""}` : ""}.`);
    words.push(DRIFT_TITLE[row.drift] + (row.repo_commit ? ` GitHub is at ${row.repo_commit}.` : ""));
    if (api && !api.commit) words.push("This deploy carries no version stamp, so HELIX cannot compare it with GitHub. Deploys made from HELIX are stamped.");
    if (api?.dirty) words.push("Deployed from a folder with unsaved (uncommitted) changes, so what is running is not exactly what is in GitHub.");
    if (api?.db) words.push(`Database: ${api.db}${api.read_only ? " (read-only)" : ""}.`);
    if (row.note) words.push(row.note);
  } else if (!row.exists) {
    words.push(`${env} is not deployed. A prototype until it is - add it from the gear when it is ready.`);
  } else {
    words.push(`${env} has not been read yet.`);
  }
  return (
    <div className={`board-row${!row.exists ? " absent" : ""}`} style={{ position: "relative" }}>
      {thread && <i className={`board-thread ${thread}`} aria-hidden="true" />}
      <div className="flex items-center gap-x-2.5 text-[13px]">
        <StatusFace status={status} size={30} title={`${env}: ${STATUS_LABEL[status]} - click for the words`} onClick={() => setWhy((w) => !w)}
          attention={row.exists && Boolean(row.checked_at) && (row.needs_attention || Boolean(api?.dirty))} />
        <span className="board-env">{env}</span>
        {/* the face says running / not deployed / not read: only what needs a look stays in words */}
        {!row.exists ? (
          onCreate && (
            <button className="board-create" onClick={onCreate} data-tip={`Create the ${env} environment - a gated, deliberate act: the plan is shown first, nothing runs until it is confirmed`}>
              <i aria-hidden="true">✦</i> Create
            </button>
          )
        ) : unread ? (
          (busy || queued) ? <Strandbar progress={null} state="running" height={10} words={false} compact /> : null
        ) : (
          <>
            {row.health !== "ok" && <span style={{ color: HEALTH_COLOR[row.health], letterSpacing: 1 }}>{HEALTH_LABEL[row.health]}</span>}
            {(row.drift === "behind" || row.drift === "ahead" || row.drift === "diverged") && (
              <Pill
                text={row.drift === "behind" && row.behind_by != null ? `${row.behind_by} behind` : DRIFT_LABEL[row.drift]}
                color={DRIFT_COLOR[row.drift]}
                title={DRIFT_TITLE[row.drift] + (row.repo_commit ? ` GitHub is at ${row.repo_commit}.` : "")}
              />
            )}
            {api?.dirty && <span className="board-dot warn" title="Deployed with unsaved changes - not exactly what is in GitHub" />}
            {api?.is_split && <span className="board-dot" style={{ background: "var(--working)" }} title={`Traffic split: ${api.traffic_percent}%`} />}
            {api?.read_only && <span className="board-dot" style={{ background: "var(--amber)" }} title="Read-only database" />}
            {api?.commit ? <Decoded text={api.commit} className="board-chip mono" /> : null}
          </>
        )}
        <div className="flex-1" />
        <button className={`board-why${why ? " on" : ""}`} onClick={() => setWhy((w) => !w)} title="What this means, in plain words" aria-label="Explain">?</button>
      </div>
      {why && (
        <div className="board-why-pop" onClick={() => setWhy(false)}>
          {words.map((w, i) => <p key={i}>{w}</p>)}
          {api?.revision && <p className="dim">{api.revision}{api.deployed_at ? ` · ${when(api.deployed_at)}` : ""}{api.deployed_by ? ` · ${api.deployed_by}` : ""}</p>}
          {row.detail && <pre>{row.detail}</pre>}
        </div>
      )}
    </div>
  );
}

function threadBetween(a: Row, b: Row): Thread {
  if (!a.exists || !b.exists) return null;
  if (!a.checked_at || !b.checked_at) return null;
  const ca = a.api?.commit, cb = b.api?.commit;
  if (!ca || !cb) return "unknown";
  return ca === cb ? "same" : "changed";
}

type Action = "dev" | "git" | "deploy" | "create";

function AppCard({ card, index, busy, queued, onRead, onAction, onLink, collapsed, onToggle }: { card: Card; index: number; busy: boolean; queued?: boolean; onRead: (app: string) => void; onAction: (app: string, a: Action, env?: string) => void; onLink: (app: string) => void; collapsed?: boolean; onToggle?: () => void }) {
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
      id={`app-${card.app}`}
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
        <button className="board-title" onClick={onToggle} title={collapsed ? "Expand" : "Collapse"}>
          <span className={`board-fold${collapsed ? "" : " open"}`} aria-hidden="true">▸</span>
          <span>
            <span className="board-index">APP {String(index + 1).padStart(2, "0")}{prototype && <span className="board-proto" title={`No production environment - a prototype. ${envsMissing.length ? "Missing: " + envsMissing.join(", ").toUpperCase() + ". " : ""}Add environments from the gear when it is ready.`}>◇ PROTOTYPE</span>}</span>
            <span className="board-app"><Decoded text={card.app} /></span>
          </span>
        </button>
        {attention && <Pill text="needs a look" color="var(--working)" />}
        {collapsed && live.length > 0 && <span className="text-[11px]" style={MUTED}>{up}/{live.length} running</span>}
        <div className="flex-1" />
        <StatusFace status={busy ? "reading" : queued ? "queued" : live.length === 0 ? "unknown" : attention || up < live.length ? "degraded" : prototype ? "prototype" : "ok"} size={40}
          title={busy ? "Reading" : queued ? "Queued" : live.length === 0 ? "Not read yet" : `${up} of ${live.length} running${attention ? " - needs a look" : ""}${dirty ? " - unsaved changes deployed" : ""}`} />
        <button className="btn text-xs" disabled={busy || queued} onClick={() => onRead(card.app)} title="Read this app's environments now">
          {busy ? <Strandbar progress={null} state="running" height={10} words={false} compact /> : queued ? "Queued" : "Read"}
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
      <div className="flex items-center gap-2 mt-2">
        {card.link?.folder && (
          <button className="board-folder" title={`Open ${card.link.folder} in Explorer`}
            onClick={() => void api.post("/api/fleet/open_folder", { app: card.app }).catch((e: Error) => window.alert(e.message))}>
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2a1.5 1.5 0 0 1 1.1.5L11 7h8.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-11Z" /></svg>
            <span className="elide">{card.link.folder.split(/[\\/]/).pop()}</span>
          </button>
        )}
        {card.repo && <a className="board-chip dim elide" title={`${card.repo} on GitHub`} href={`https://github.com/${card.repo}`} target="_blank" rel="noreferrer noopener">⎇ {card.repo.split("/")[1]}</a>}
      </div>
      <div className="mt-3 pl-1">
        {card.envs.map((row, i) => (
          <EnvRow key={row.key} row={unlinked && row.note === card.link?.why ? { ...row, note: null } : row} busy={busy} queued={queued}
            thread={i < card.envs.length - 1 ? threadBetween(row, card.envs[i + 1]) : null}
            onCreate={prototype && envsMissing.includes(row.env) ? () => onAction(card.app, "create", row.env) : undefined} />
        ))}
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

/** The tabs' pictures (Brian, 2026-09-22): each one moves when its tab is on or hovered - the
 *  hexagons of Apps breathe, Protocols' arrow runs its loop, the Agent blinks, the Hologram turns,
 *  the Vault's shackle lifts. Plain SVG + CSS, nothing drawn per frame. */
function TabIcon({ tab }: { tab: Tab }) {
  const P = { fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (tab) {
    case "apps": return <svg className="tab-ic apps" viewBox="0 0 24 24" width="15" height="15" {...P}><path className="hex a" d="M12 3l5 3v6l-5 3-5-3V6z" /><path className="hex b" d="M6 12l4 2.3V19l-4 2.3L2 19v-4.7z" transform="translate(1 -1) scale(.8)" /><path className="hex c" d="M18 12l4 2.3V19l-4 2.3L14 19v-4.7z" transform="translate(-1 -1) scale(.8)" /></svg>;
    case "tasks": return <svg className="tab-ic tasks" viewBox="0 0 24 24" width="15" height="15" {...P}><path className="loop" d="M4 12a8 8 0 0 1 14-5.3" /><path className="loop b" d="M20 12a8 8 0 0 1-14 5.3" /><path className="tip" d="M18 3v4h-4" /><path className="tip" d="M6 21v-4h4" /></svg>;
    case "agents": return <svg className="tab-ic agents" viewBox="0 0 24 24" width="15" height="15" {...P}><rect x="4" y="7" width="16" height="12" rx="4" /><path d="M12 3v4M9 3h6" /><circle className="eye" cx="9" cy="13" r="1.4" fill="currentColor" stroke="none" /><circle className="eye" cx="15" cy="13" r="1.4" fill="currentColor" stroke="none" /></svg>;
    case "models": return <svg className="tab-ic models" viewBox="0 0 24 24" width="15" height="15" {...P}><g className="cube"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" /><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" /></g></svg>;
    case "knowledge": return <svg className="tab-ic vault" viewBox="0 0 24 24" width="15" height="15" {...P}><rect x="5" y="11" width="14" height="10" rx="2" /><path className="shackle" d="M8 11V7.5a4 4 0 0 1 8 0V11" /><circle cx="12" cy="16" r="1.5" fill="currentColor" stroke="none" /></svg>;
  }
}

/** The right-hand drawer THE FORGE opens for Git and Deploy. UI first (Brian, 2026-09-19): the
 *  reads are live where the fleet has them; every verb that would change a service is shown,
 *  named, and disabled until its lane is wired behind the domain's checks. */
/** A WINDOW: centred, wide, with the room dimmed behind it. Nothing docks over it. */
function Drawer({ title, sub, onClose, children, wide, tools }: { title: string; sub?: string; onClose: () => void; children: React.ReactNode; wide?: boolean; tools?: React.ReactNode }) {
  useEffect(() => { const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); }; window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, [onClose]);
  return (
    <div className="board-drawer-wrap centred" onClick={onClose}>
      <aside className={`board-window${wide ? " wide" : ""}`} onClick={(e) => e.stopPropagation()}>
        <i className="corners" aria-hidden="true" />
        <div className="board-window-head">
          <div>
            <div className="board-kicker">{sub}</div>
            <div className="board-app" style={{ fontSize: 20 }}>{title}</div>
          </div>
          <div className="flex-1" />
          {tools}
          <button className="btn text-xs" onClick={onClose}>✕</button>
        </div>
        <div className="board-window-body">{children}</div>
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

/** THE STRANDS: the commit graph drawn as DNA - each lane a glowing strand with light running
 *  along it, commits as beads (a merge is a knot of two strands), branch tips as bright caps, and
 *  the environments as gold rings on whatever they are serving. Hover a bead for the words; click
 *  to pin them. Nothing is text until you ask. */
/** THE STRANDS (Brian, 2026-09-22): the lines between commits are little double helices - two
 *  strands winding round the path with rungs, turning on their own, and LIQUID under the mouse:
 *  the hand pushes the strand aside and it springs back. One canvas under the SVG nodes (the
 *  nodes keep the hit-testing), on the governor's 2D budget, sprites for the glow. */
function GitStrands({ edges, width, height, left, top, col, row }: { edges: { from: number; to: number; fromLane: number; toLane: number }[]; width: number; height: number; left: number; top: number; col: number; row: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    const dpr = Math.min(1.5, window.devicePixelRatio || 1);
    c.width = Math.round(width * dpr); c.height = Math.round(height * dpr);
    const hueOf = (hex: string) => { const r = parseInt(hex.slice(1, 3), 16) / 255, gg = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255; const mx = Math.max(r, gg, b), mn = Math.min(r, gg, b), d = mx - mn; if (!d) return 190; let h = mx === r ? ((gg - b) / d) % 6 : mx === gg ? (b - r) / d + 2 : (r - gg) / d + 4; h = Math.round(h * 60); return h < 0 ? h + 360 : h; };
    // every edge sampled once: the points along its path and their tangents; each sample keeps a push
    type Pt = { x: number; y: number; tx: number; ty: number; px: number; py: number };
    const strands = edges.map((e) => {
      const x1 = left + e.fromLane * col, y1 = top + e.from * row, x2 = left + e.toLane * col, y2 = top + e.to * row;
      const n = Math.max(6, Math.round(Math.hypot(x2 - x1, y2 - y1) / 4));
      const pts: Pt[] = [];
      for (let i = 0; i <= n; i++) {
        const t = i / n;
        let x: number, y: number, tx: number, ty: number;
        if (x1 === x2) { x = x1; y = y1 + (y2 - y1) * t; tx = 0; ty = 1; }
        else {                                        // the same cubic the SVG used
          const c1y = y1 + row * 0.7, c2y = y2 - row * 0.7, u = 1 - t;
          x = u * u * u * x1 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x2;
          y = u * u * u * y1 + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * y2;
          const dx = 3 * u * u * (x1 - x1) + 6 * u * t * (x2 - x1) + 3 * t * t * (x2 - x2);
          const dy = 3 * u * u * (c1y - y1) + 6 * u * t * (c2y - c1y) + 3 * t * t * (y2 - c2y);
          const l = Math.hypot(dx, dy) || 1; tx = dx / l; ty = dy / l;
        }
        pts.push({ x, y, tx, ty, px: 0, py: 0 });
      }
      return { pts, hue: hueOf(LANE_COLORS[e.toLane % LANE_COLORS.length]), phase: Math.random() * 6.28, len: Math.hypot(x2 - x1, y2 - y1) };
    });
    const mouse = { x: -9999, y: -9999 };
    const onMove = (e: MouseEvent) => { const r = c.getBoundingClientRect(); mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top; };
    const onLeave = () => { mouse.x = -9999; mouse.y = -9999; };
    c.parentElement?.addEventListener("mousemove", onMove); c.parentElement?.addEventListener("mouseleave", onLeave);
    let t = 0;
    const R = 4.2, TURN = 14, REACH = 42;
    const stop = loop((dt) => {
      t += dt;
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, width, height);
      const glow = glows();
      for (const s of strands) {
        // the liquid: each sample is pushed off the cursor, eases back
        for (const p of s.pts) {
          const dx = p.x + p.px - mouse.x, dy = p.y + p.py - mouse.y, d = Math.hypot(dx, dy);
          if (d < REACH && d > 0.01) { const f = (1 - d / REACH) * 26; p.px += (dx / d) * f * dt * 6; p.py += (dy / d) * f * dt * 6; }
          p.px *= Math.pow(0.08, dt); p.py *= Math.pow(0.08, dt);
        }
        const spin = t * 2.4 + s.phase;
        // rungs first, then the two strands as short lit segments, back one dimmer
        for (let k = 0; k < 2; k++) {
          let prev: { x: number; y: number } | null = null;
          for (let i = 0; i < s.pts.length; i++) {
            const p = s.pts[i]; const along = (i / (s.pts.length - 1)) * s.len;
            const a = along / TURN * Math.PI * 2 + spin + k * Math.PI;
            const off = Math.cos(a) * R, depth = (Math.sin(a) + 1) / 2;
            const nx = -p.ty, ny = p.tx;
            const x = p.x + p.px + nx * off, y = p.y + p.py + ny * off;
            if (prev) {
              g.strokeStyle = `hsla(${s.hue}, 90%, ${50 + depth * 35}%, ${0.25 + 0.65 * depth})`;
              g.lineWidth = 0.9 + depth * 1.5;
              g.beginPath(); g.moveTo(prev.x, prev.y); g.lineTo(x, y); g.stroke();
            }
            if (k === 0 && i % 4 === 2) {   // a rung to the other strand
              const a2 = a + Math.PI, off2 = Math.cos(a2) * R;
              g.strokeStyle = `hsla(${s.hue}, 80%, 80%, ${0.18 + 0.4 * Math.abs(Math.cos(a))})`; g.lineWidth = 0.8;
              g.beginPath(); g.moveTo(x, y); g.lineTo(p.x + p.px + nx * off2, p.y + p.py + ny * off2); g.stroke();
            }
            if (glow && depth > 0.92 && i % 3 === 0) { g.fillStyle = `hsla(${s.hue}, 100%, 85%, 0.9)`; g.beginPath(); g.arc(x, y, 1.2, 0, Math.PI * 2); g.fill(); }
            prev = { x, y };
          }
        }
      }
    });
    return () => { stop(); c.parentElement?.removeEventListener("mousemove", onMove); c.parentElement?.removeEventListener("mouseleave", onLeave); };
  }, [edges, width, height, left, top, col, row]);
  return <canvas ref={ref} className="board-strands-canvas" style={{ width, height }} aria-hidden="true" />;
}

function GitGraph({ doc, serving, onPick }: { doc: GitDoc; serving: { env: string; commit: string }[]; onPick?: (c: GitCommit | null) => void }) {
  const { laneOf, edges, width } = useMemo(() => layoutGraph(doc.commits), [doc]);
  const [hover, setHover] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const ROW = 30, COL = 26, LEFT = 22, TOP = 22;
  const gw = LEFT * 2 + COL * Math.max(width - 1, 0) + 12;
  const tips = new Map<string, string[]>();
  doc.branches.forEach((b) => { tips.set(b.sha, [...(tips.get(b.sha) || []), b.name]); });
  const served = new Map<string, string[]>();
  serving.forEach((s) => { const k = doc.commits.find((c) => c.sha.startsWith(s.commit))?.sha; if (k) served.set(k, [...(served.get(k) || []), s.env]); });
  const H = TOP + ROW * doc.commits.length + 10;
  const active = pinned || hover;
  const activeCommit = active ? doc.commits.find((c) => c.sha === active) || null : null;
  useEffect(() => { onPick?.(pinned ? doc.commits.find((c) => c.sha === pinned) || null : null); }, [pinned, doc, onPick]);
  return (
    <div className="board-strands">
      <div className="board-strands-stack" style={{ width: gw, height: H }}>
      <GitStrands edges={edges} width={gw} height={H} left={LEFT} top={TOP} col={COL} row={ROW} />
      <svg className="board-strands-svg" width={gw} height={H}>
        <defs>
          {LANE_COLORS.map((c, i) => (
            <linearGradient key={i} id={`lane${i}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={c} stopOpacity="0.95" /><stop offset="1" stopColor={c} stopOpacity="0.35" /></linearGradient>
          ))}
          <filter id="strandGlow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        </defs>
        {doc.commits.map((c, i) => {
          const lane = laneOf.get(c.sha) ?? 0;
          const x = LEFT + lane * COL, y = TOP + i * ROW;
          const col = LANE_COLORS[lane % LANE_COLORS.length];
          const isTip = tips.has(c.sha), envs = served.get(c.sha) || [], merge = c.parents.length > 1;
          const on = active === c.sha;
          return (
            <g key={c.sha} className="board-graph-node" style={{ animationDelay: `${i * 30}ms`, cursor: "pointer" }}
              onMouseEnter={() => setHover(c.sha)} onMouseLeave={() => setHover(null)} onClick={() => setPinned((p) => (p === c.sha ? null : c.sha))}>
              <circle cx={x} cy={y} r={14} fill="transparent" />
              {envs.length > 0 && <circle cx={x} cy={y} r={10} fill="none" stroke="var(--gold)" strokeWidth={1.5} className="board-graph-served" />}
              {envs.some((e) => e === "prod") && <circle cx={x} cy={y} r={13} fill="none" stroke="var(--gold)" strokeWidth={0.8} opacity={0.6} strokeDasharray="2 3" />}
              <circle cx={x} cy={y} r={on ? 7 : merge ? 4.5 : 5.5} fill={merge ? "#0b1218" : col} stroke={on ? "#fff" : col} strokeWidth={merge ? 2.4 : 1.6} filter="url(#strandGlow)" />
              {isTip && <circle cx={x} cy={y} r={2.2} fill="#fff" />}
              {on && <circle cx={x} cy={y} r={11} fill="none" stroke="#fff" strokeWidth={0.8} opacity={0.5} />}
            </g>
          );
        })}
      </svg>
      </div>
      <div className="board-strands-rows">
        {doc.commits.map((c) => {
          const lane = laneOf.get(c.sha) ?? 0;
          const names = tips.get(c.sha) || [], envs = served.get(c.sha) || [];
          const on = active === c.sha;
          return (
            <div key={c.sha} className={`board-strand-row${on ? " on" : ""}`} style={{ height: ROW }}
              onMouseEnter={() => setHover(c.sha)} onMouseLeave={() => setHover(null)} onClick={() => setPinned((p) => (p === c.sha ? null : c.sha))}>
              <span className="board-chip mono" style={{ color: LANE_COLORS[lane % LANE_COLORS.length] }}>{c.sha.slice(0, 7)}</span>
              {names.map((n) => <span key={n} className={`board-ref${n === doc.branch ? " main" : ""}`}>⎇ {n}</span>)}
              {envs.map((e) => <span key={e} className={`board-ref served${e === "prod" ? " prod" : ""}`}>▲ {e}</span>)}
              <span className="elide board-graph-subject">{c.subject}</span>
            </div>
          );
        })}
      </div>
      {activeCommit && (
        <div className={`board-strand-card${pinned ? " pinned" : ""}`}>
          <div className="board-kicker">{pinned ? "PINNED · click again to release" : "COMMIT"}</div>
          <div className="text-[14px] mt-1">{activeCommit.subject}</div>
          <div className="text-[12px] mt-1" style={MUTED}>{activeCommit.author}{activeCommit.at ? ` · ${new Date(activeCommit.at).toLocaleString()}` : ""}</div>
          <div className="flex items-center gap-2 flex-wrap mt-2">
            <span className="board-chip mono">{activeCommit.sha.slice(0, 12)}</span>
            {(tips.get(activeCommit.sha) || []).map((n) => <span key={n} className="board-ref">⎇ {n}</span>)}
            {(served.get(activeCommit.sha) || []).map((e) => <span key={e} className="board-ref served">▲ {e}</span>)}
            {activeCommit.parents.length > 1 && <span className="board-ref">merge of {activeCommit.parents.length}</span>}
          </div>
          <a className="text-[12px] mt-2 inline-block" style={{ color: "var(--cyan)" }} href={`https://github.com/${doc.repo}/commit/${activeCommit.sha}`} target="_blank" rel="noreferrer noopener">Open on GitHub ↗</a>
        </div>
      )}
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
  // THE MERGE CHECK: would this branch fold into main cleanly? Read from the linked clone.
  const [mergeInto, setMergeInto] = useState("main");
  const [merge, setMerge] = useState<{ clean: boolean; conflicts: string[]; ahead: number; behind: number; base: string } | null>(null);
  const [mergeErr, setMergeErr] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const runMerge = async () => {
    if (!doc) return;
    setChecking(true); setMergeErr(null); setMerge(null);
    try { setMerge(await api.post("/api/fleet/merge_check", { app: card.app, from: doc.branch, into: mergeInto })); }
    catch (e) { setMergeErr((e as Error).message); } finally { setChecking(false); }
  };
  return (
    <Drawer title={card.app} sub="GIT · the strands" onClose={onClose} wide
      tools={<a className="btn text-xs" href={`https://github.com/${card.repo}`} target="_blank" rel="noreferrer noopener">GitHub ↗</a>}>
      <div className="board-git">
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
          {doc && doc.branches.length > 1 && (
            <div className="board-merge mt-3">
              <div className="board-kicker mb-1">FOLD THIS BRANCH INTO</div>
              <div className="flex items-center gap-2 flex-wrap">
                <label className="board-branch"><span>⎇</span>
                  <select value={mergeInto} onChange={(e) => setMergeInto(e.target.value)}>
                    {doc.branches.filter((b) => b.name !== doc.branch).map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
                  </select>
                </label>
                <button className="btn btn-primary text-xs board-wait" disabled={checking || !doc} onClick={() => void runMerge()}>{checking ? <Strandbar progress={null} state="running" height={10} words={false} compact /> : "Check for conflicts"}</button>
                <button className="btn text-xs" disabled title="Merging from HELIX lands with the conflict editor - next round">Merge</button>
              </div>
              {mergeErr && <div className="mt-2" style={{ color: "var(--error)" }}>{mergeErr}</div>}
              {merge && (
                <div className="mt-2">
                  {merge.clean
                    ? <div className="flex items-center gap-2"><Pill text="merges cleanly" color="var(--done)" /><span style={MUTED}>{doc.branch} is {merge.ahead} commit{merge.ahead === 1 ? "" : "s"} ahead of {mergeInto}{merge.behind ? ` and ${merge.behind} behind` : ""} · common ancestor {merge.base}</span></div>
                    : <div>
                        <div className="flex items-center gap-2"><Pill text={`${merge.conflicts.length} file${merge.conflicts.length === 1 ? "" : "s"} conflict`} color="var(--error)" /><span style={MUTED}>{doc.branch} vs {mergeInto} · these need a decision in the conflict editor (next round)</span></div>
                        <div className="board-scan mt-2">{merge.conflicts.map((f) => <div key={f} className="board-scan-row high"><span className="board-scan-kind">conflict</span><span className="board-chip mono">{f}</span><span /></div>)}</div>
                      </div>}
                </div>
              )}
            </div>
          )}
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
      </div>
      <div className="space-y-4 text-[13px]">
        <div className="board-panel">
          <div className="flex items-center gap-3 flex-wrap">
            <div className="board-kicker">SECRETS SCAN</div>
            <div className="flex-1" />
            <button className="btn btn-primary text-xs board-wait" disabled={scanning} onClick={() => void runScan()}>{scanning ? <Strandbar progress={null} state="running" height={10} words={false} compact /> : "⌕ Scan this branch"}</button>
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
      </div>
      </div>
    </Drawer>
  );
}

interface DeployStatus { running: boolean; job: { kind: string; app: string; env: string; to?: string; by?: string } | null; log: string[]; recent: { id: string; at: string; kind: string; app: string; env: string; by?: string; ok?: boolean; to?: string; planned?: boolean }[] }
interface Targets { served: string[]; current: string | null; targets: string[] }

/** THE DEPLOY WINDOW: an environment picked at the top; the revisions of that environment as a
 *  strand of beads (the live one lit gold, earlier ones dimmer, the ones you may roll back to
 *  clickable); Ship on the right with pre-flight; the gates for prod (type it, then say yes);
 *  the log streaming in as it runs; the audit trail under it. */
function DeployDrawer({ card, onClose, env: envIn, mode: modeIn }: { card: Card; onClose: () => void; env?: string; mode?: "deploy" | "create" }) {
  const [env, setEnv] = useState<"dev" | "qa" | "prod">((envIn as "dev" | "qa" | "prod") || "dev");
  const row = card.envs.find((r) => r.env === env);
  const [st, setSt] = useState<DeployStatus | null>(null);
  const [tg, setTg] = useState<Targets | null>(null);
  const [who, setWho] = useState<{ identity: string | null; prod: boolean } | null>(null);
  const [consoleRoot, setConsoleRoot] = useState<string>("");
  const [pick, setPick] = useState<string | null>(null);          // a revision picked on the strand
  const [gate, setGate] = useState<{ verb: "deploy" | "rollback" | "create"; env: string; to?: string } | null>(modeIn === "create" ? { verb: "create", env: envIn || "qa" } : null);
  const [phrase, setPhrase] = useState("");
  const [sure, setSure] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<{ service: string; site: string | null; steps: { what: string; cmd: string }[]; note: string } | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  const load = useCallback(() => {
    void api.get<DeployStatus>("/api/deploy/status").then(setSt).catch(() => undefined);
    void api.get<Targets>(`/api/deploy/targets?app_name=${encodeURIComponent(card.app)}&env=${env}`).then(setTg).catch(() => setTg(null));
  }, [card.app, env]);
  useEffect(() => { load(); void api.get<{ identity: string | null; prod: boolean }>("/api/deploy/identity").then(setWho).catch(() => undefined);
    void api.get<{ values?: Record<string, unknown> }>("/api/settings").then((d) => setConsoleRoot(String(d.values?.console_root || ""))).catch(() => undefined); }, [load]);
  useEffect(() => {
    const onLine = (e: Event) => { const d = (e as CustomEvent).detail as { line?: string; t?: string }; setSt((s) => s ? { ...s, running: d.t === "deploy", log: d.line ? [...s.log, d.line].slice(-400) : s.log } : s); if (d.t === "deploy_done") load(); };
    window.addEventListener("helix-deploy", onLine);
    return () => window.removeEventListener("helix-deploy", onLine);
  }, [load]);
  useEffect(() => { logRef.current?.scrollTo({ top: 1e9 }); }, [st?.log.length]);
  const isProd = env === "prod";
  const exists = row?.exists ?? false;
  const checks: { ok: boolean | null; text: string }[] = [
    { ok: exists, text: exists ? "The service exists on Cloud Run" : "No service in this environment - never created by a deploy (rule 5)" },
    { ok: row?.checked_at ? row.health === "ok" : null, text: row?.checked_at ? `Running ${row.api?.revision ?? "?"} - ${HEALTH_LABEL[row.health]}` : "Not read yet" },
    { ok: row?.api?.commit ? !row.api.dirty : null, text: row?.api?.dirty ? "What is running was built from unsaved changes" : row?.api?.commit ? `Running commit ${row.api.commit}` : "No version stamp on the running revision" },
    { ok: true, text: card.app === "MES" ? "Env vars: backend/deploy.ps1 is the only source (rule 2)" : card.app === "ECHO" ? "Env vars: dev.ps1's ECHO block, never hand-typed" : "No --set-env-vars, --vpc-connector or --service-account (rule 1)" },
    { ok: consoleRoot ? true : false, text: consoleRoot ? `dev.ps1 wrapped from ${consoleRoot}` : "The console checkout is not set - set it below" },
    { ok: isProd ? (who?.prod ?? null) : true, text: isProd ? (who?.identity ? `${who.identity} ${who.prod ? "may" : "may NOT"} touch production` : "No gcloud account signed in") : `Dev / QA: ${who?.identity || "your gcloud login"} is enough` },
  ];
  const canShip = exists && !!consoleRoot && (!isProd || !!who?.prod) && !st?.running;
  const go = async () => {
    if (!gate) return;
    setErr(null);
    try {
      if (gate.verb === "deploy") await api.post("/api/deploy", { app: card.app, env: gate.env, phrase, sure });
      else if (gate.verb === "rollback") await api.post("/api/deploy/rollback", { app: card.app, env: gate.env, to: gate.to, phrase, sure });
      else { const r = await api.post<{ service: string; site: string | null; steps: { what: string; cmd: string }[]; note: string }>("/api/deploy/create_plan", { app: card.app, env: gate.env, typed: phrase, sure }); setPlan(r); }
      setGate(null); setPhrase(""); setSure(false); load();
    } catch (e) { setErr((e as Error).message); }
  };
  const saveRoot = (v: string) => void api.put("/api/deploy/console_root", { path: v }).then(() => { setConsoleRoot(v); setErr(null); }).catch((e: Error) => setErr(e.message));
  const served = tg?.served ?? [];
  return (
    <Drawer title={card.app} sub="DEPLOY · ship, roll back, grow" onClose={onClose} wide
      tools={<div className="flex gap-2">{(["dev", "qa", "prod"] as const).map((e) => <button key={e} className={`board-envpick${env === e ? " on" : ""}${e === "prod" ? " prod" : ""}`} onClick={() => { setEnv(e); setPick(null); setGate(null); }}>{e.toUpperCase()}</button>)}</div>}>
      <div className="board-deploy">
        <div className="space-y-4 text-[13px]">
          <div className="board-panel">
            <div className="board-kicker mb-2">THE REVISIONS · {env.toUpperCase()} {exists ? "" : "· not deployed"}</div>
            {!exists ? (
              <div style={MUTED}>{card.app} has no {env.toUpperCase()} yet. Create it deliberately below - never from a deploy.</div>
            ) : served.length === 0 ? (
              <div style={MUTED}>{row?.checked_at ? "No revisions came back from the last read." : "Read the app to see its revisions."}</div>
            ) : (
              <div className="board-revs">
                <svg className="board-revs-svg" width="100%" height={Math.max(60, served.length * 34 + 20)} viewBox={`0 0 40 ${Math.max(60, served.length * 34 + 20)}`} preserveAspectRatio="none">
                  <path d={`M20 6 ${served.map((_, i) => `L20 ${16 + i * 34}`).join(" ")}`} stroke="var(--gold)" strokeWidth="5" opacity="0.15" fill="none" />
                  <path d={`M20 6 ${served.map((_, i) => `L20 ${16 + i * 34}`).join(" ")}`} stroke="var(--gold)" strokeWidth="1.5" opacity="0.7" fill="none" className="board-graph-edge" />
                </svg>
                <div className="board-revs-list">
                  {served.map((r, i) => {
                    const live = r === tg?.current, can = (tg?.targets || []).includes(r), on = pick === r;
                    return (
                      <div key={r} className={`board-rev${live ? " live" : ""}${can ? " can" : ""}${on ? " on" : ""}`} onClick={() => can && setPick(on ? null : r)} title={live ? "Serving now" : can ? "A revision that served - click to roll back to it" : "Not a rollback target"}>
                        <span className="board-rev-bead" />
                        <span className="board-chip mono">{r}</span>
                        {live && <Pill text="serving" color="var(--gold)" />}
                        {i === 0 && !live && <Pill text="newest" color="var(--cyan)" />}
                        {on && <button className="btn btn-primary text-xs ml-auto" onClick={(e) => { e.stopPropagation(); setGate({ verb: "rollback", env, to: r }); }}>↶ Roll back to this</button>}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
          <div className="board-panel">
            <div className="board-kicker mb-2">PRE-FLIGHT</div>
            {checks.map((c, i) => (
              <div key={i} className="flex items-start gap-2 py-1">
                <span style={{ color: c.ok === true ? "var(--done)" : c.ok === false ? "var(--error)" : "var(--muted)", width: 14 }}>{c.ok === true ? "●" : c.ok === false ? "●" : "○"}</span>
                <span>{c.text}</span>
              </div>
            ))}
            {!consoleRoot && (
              <div className="mt-2 flex items-center gap-2">
                <input className="flex-1" placeholder="C:\\Users\\you\\...\\BRMS_MES_WEB_VERSION" onKeyDown={(e) => { if (e.key === "Enter") saveRoot((e.target as HTMLInputElement).value); }} />
                <span className="text-xs" style={MUTED}>the folder with dev.ps1 · Enter</span>
              </div>
            )}
            <div className="flex items-center gap-3 flex-wrap mt-3">
              <button className="btn btn-primary" disabled={!canShip} onClick={() => setGate({ verb: "deploy", env })}>▲ Ship {env.toUpperCase()}</button>
              {!exists && <button className="btn board-create" disabled={!who?.prod} title={who?.prod ? "" : "Creating an environment is for Brian, Brendan and Kate"} onClick={() => setGate({ verb: "create", env })}>＋ Create {env.toUpperCase()}</button>}
              {st?.running && <span className="text-xs" style={{ color: "var(--working)" }}>a {st.job?.kind} is running on {st.job?.app} {st.job?.env}…</span>}
            </div>
            {err && <div className="mt-2" style={{ color: "var(--error)" }}>{err}</div>}
          </div>
          {plan && (
            <div className="board-panel board-plan">
              <div className="board-kicker mb-1">THE PLAN · {card.app} {plan.service}</div>
              {plan.steps.map((s, i) => <div key={i} className="board-plan-step"><span className="board-plan-n">{i + 1}</span><div><div>{s.what}</div><code>{s.cmd}</code></div></div>)}
              <div className="mt-2 text-xs" style={{ color: "var(--working)" }}>{plan.note}</div>
            </div>
          )}
        </div>
        <div className="space-y-4 text-[13px]">
          <div className="board-panel board-term">
            <div className="flex items-center gap-2">
              <div className="board-kicker">THE RUN</div>
              <span className={`board-term-dot${st?.running ? " on" : ""}`} />
              <div className="flex-1" />
              {st?.job && <span className="text-xs" style={MUTED}>{st.job.kind} · {st.job.app} {st.job.env}{st.job.to ? ` → ${st.job.to}` : ""}{st.job.by ? ` · ${st.job.by}` : ""}</span>}
            </div>
            <div ref={logRef} className="board-term-log">
              {(st?.log || []).length === 0 && <div style={MUTED}>Nothing has run yet. The lines of the next ship or rollback stream here.</div>}
              {(st?.log || []).map((l, i) => <div key={i} className={`board-term-line${/error|failed|denied/i.test(l) ? " bad" : /\[helix\]/.test(l) ? " helix" : /https?:\/\//.test(l) ? " url" : ""}`}>{l}</div>)}
            </div>
          </div>
          <div className="board-panel">
            <div className="board-kicker mb-1">THE TRAIL · what was done, by whom</div>
            {(st?.recent || []).length === 0 && <div style={MUTED}>No deploys or rollbacks from HELIX yet.</div>}
            {(st?.recent || []).slice(0, 8).map((r) => (
              <div key={r.id} className="flex items-center gap-2 py-1 text-[12px]">
                <span style={{ color: r.planned ? "var(--working)" : r.ok === true ? "var(--done)" : r.ok === false ? "var(--error)" : "var(--muted)" }}>●</span>
                <span className="board-chip dim">{r.kind}</span><span>{r.app} {r.env}{r.to ? ` → ${r.to}` : ""}</span>
                <span className="ml-auto" style={MUTED}>{r.by?.split("@")[0]} · {ago(r.at)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      {gate && (
        <div className="board-gate-wrap" onClick={() => setGate(null)}>
          <div className={`board-gate${gate.env === "prod" ? " prod" : ""}`} onClick={(e) => e.stopPropagation()}>
            <div className="board-kicker">{gate.env === "prod" ? "PRODUCTION · the gate" : `${gate.env.toUpperCase()} · confirm`}</div>
            <div className="board-app mt-1" style={{ fontSize: 18 }}>
              {gate.verb === "deploy" ? `Ship ${card.app} to ${gate.env.toUpperCase()}` : gate.verb === "rollback" ? `Roll ${card.app} ${gate.env.toUpperCase()} back to ${gate.to}` : `Create ${card.app} ${gate.env.toUpperCase()}`}
            </div>
            <div className="mt-2 text-[13px]" style={MUTED}>
              {gate.verb === "deploy" && `dev.ps1 -App ${card.app.toLowerCase()} -Action be-deploy -Env ${gate.env}, from ${consoleRoot || "the console checkout"}. Signed in as ${who?.identity || "?"}. An audit row is written first.`}
              {gate.verb === "rollback" && `A traffic shift to a revision that already served - no build, no flags. The current revision stays in Cloud Run to roll forward to.`}
              {gate.verb === "create" && `This shows the exact plan - the Cloud Run service, its env vars from dev.ps1, the Hosting site - and runs nothing. Running it is the next step, with the plan read beside a person.`}
            </div>
            {(gate.env === "prod" || gate.verb === "create") && (
              <label className="block mt-3">
                <span className="board-kicker">{gate.verb === "create" ? `TYPE THE APP'S NAME: ${card.app}` : `TYPE: ${gate.verb} prod`}</span>
                <input className="w-full mt-1" autoFocus value={phrase} onChange={(e) => setPhrase(e.target.value)} placeholder={gate.verb === "create" ? card.app : `${gate.verb} prod`} />
              </label>
            )}
            <label className="flex items-center gap-2 mt-3 text-[13px]">
              <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} /> Are you sure? Yes, do it.
            </label>
            {err && <div className="mt-2 text-[12px]" style={{ color: "var(--error)" }}>{err}</div>}
            <div className="flex items-center gap-2 mt-4">
              <button className={`btn ${gate.env === "prod" ? "btn-danger" : "btn-primary"}`} disabled={!sure || ((gate.env === "prod" || gate.verb === "create") && !phrase.trim())} onClick={() => void go()}>
                {gate.verb === "deploy" ? "▲ Ship" : gate.verb === "rollback" ? "↶ Roll back" : "Show the plan"}
              </button>
              <button className="btn" onClick={() => setGate(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </Drawer>
  );
}

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
  const [drawer, setDrawer] = useState<{ app: string; kind: Action; env?: string } | null>(null);
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
  const taskCounts = useTaskCounts();

  return (
    <div className="board-stage" style={{ pointerEvents: "auto" }}>
      <HelixLayer colors={colors} />
      <NeuralLayer />
      <div className="board-layer board-hud" />
      <i className="board-corner tl" /><i className="board-corner tr" />
      <i className="board-corner bl" /><i className="board-corner br" />

      <Rail cards={company?.apps ?? []} tasks={taskCounts} />
      <div className="h-full overflow-y-auto pt-16 px-8 pb-12 relative board-scroll">
        <div className="max-w-[1140px] mx-auto space-y-5">
          <div className="flex items-end gap-4 flex-wrap">
            <div>
              <div className="board-kicker">{company?.label ?? "the company"} · everything you build and run</div>
              <div className="font-display font-bold flex items-center gap-3">
                <span className="text-glow-cyan text-[22px]" style={{ color: "var(--cyan)" }}>⬡</span>
                <span className="board-title" data-tip="Every app the company runs, in every environment: what is serving, which commit, from which database, whether the repo has moved on. Read from Cloud Run and each app's own health endpoint - nothing here is guessed.">{title}</span>
                <span className="board-readline" title={`${cells.length} cells, ${read.length ? up : "-"} up, ${read.length ? look : "-"} need a look`}>
                  {company?.checked_at ? `read ${ago(company.checked_at)}` : "not read yet"} · {read.length ? `${up}/${read.length} up` : `${cells.length} cells`}{look > 0 ? ` · ${look} need a look` : ""}
                </span>
              </div>
            </div>
          </div>

          {/* ONE LINE (Brian, 2026-09-22): the tabs on the left, Refresh and Add a project on the right,
              clear of the header's nav */}
          <div className="board-tabrow">
            <div className="flex items-center gap-1 flex-wrap">
              {TABS.map(([label, key]) => (
                <button key={key} className={`board-tab${tab === key ? " on" : ""}`} onClick={() => setTab(key)}><TabIcon tab={key} />{label}</button>
              ))}
            </div>
            <div className="flex-1" />
            <button className="btn btn-primary text-xs board-wait" disabled={busy !== null} onClick={() => refresh()} data-tip="Read every app and environment again from Cloud Run and each app's health endpoint">
              {busy === "*" ? <Strandbar progress={null} state="running" height={10} words={false} compact /> : "⟳ Refresh"}
            </button>
            <button className="btn btn-primary text-xs" onClick={() => setAdding(true)} data-tip="A new app: folder, private repo, environments">＋ Add a project</button>
          </div>

          {/* THE VAULT (secrets) sits above Brendan's knowledge vaults on the Vault tab */}
          {tab === "knowledge" && <VaultCard />}
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

          {/* CURRENT TASKS - what HELIX is doing right now, and what it finished (gold) or dropped (ember) */}
          <div id="sec-tasks" />
          <TasksSection folded={Boolean(folded.tasks)} onFold={() => fold("tasks")} />

          {/* PROJECTS - the first section of many: Slack, Google, Listeners join it as HELIX grows */}
          <section id="sec-projects" className={`board-section${folded.projects ? " folded" : ""}`}>
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
                    onAction={(app, kind, env) => kind === "dev" ? navigate({ name: "talk", project: app }) : setDrawer({ app, kind, env })} />
                ))}
              </div>
            )}
          </section>
          <section id="sec-next" className="board-section soon">
            <div className="board-section-head" style={{ cursor: "default" }}>
              <span className="board-section-chev">▸</span>
              <span className="board-section-title">SLACK · GOOGLE · LISTENERS</span>
              <span className="board-section-sub">the next sections: the company's channels, calendar and mail, and scheduled listeners that watch and act - coming as HELIX grows into the business</span>
            </div>
          </section>
          {company && (
            <div className="mt-2" id="sec-builds">
              <div className="board-kicker mb-2">LOCAL BUILDS · what HELIX made on this PC</div>
              <Menu tab="apps" embedded />
            </div>
          )}
          </>}
        </div>
      </div>
      {drawer && (() => { const c = company?.apps.find((x) => x.app === drawer.app); if (!c) return null;
        return drawer.kind === "git" ? <GitDrawer card={c} onClose={() => setDrawer(null)} onBoard={(b) => setBoard(b)} /> : <DeployDrawer card={c} onClose={() => setDrawer(null)} env={drawer.env} mode={drawer.kind === "create" ? "create" : "deploy"} />; })()}
      {adding && <AddProject onClose={() => setAdding(false)} />}
      {linking && (() => { const c = company?.apps.find((x) => x.app === linking); if (!c) return null;
        return <LinkProject card={c} onClose={() => setLinking(null)} onSaved={(b) => { setBoard(b); setLinking(null); refresh(c.app); }} />; })()}
    </div>
  );
}
