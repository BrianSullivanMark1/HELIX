// THE AVATAR — the orb's second body (Settings → Voice & look → The orb). A holographic digital
// face that condenses out of a curtain of falling 0s and 1s: a sphere of light with binary code
// streaming across its skin, scanlines and a bright rim, and on the side facing you a face - eyes
// that track the cursor and blink, brows that lift, LIPS that open and close with HELIX's voice.
//
// PULSES (Brian, 2026-09-19): the body throws a shockwave across its surface and swells at the
// moments that matter in a conversation - when it starts thinking, hard when a reply begins (the
// mouth opens on that beat), once per reply bubble that lands, and on the done / error wash. A
// pulse is a ring of light that leaves the face and wraps the sphere, plus a brief scale kick.
//
// Phases (orb_phase): "face" (the avatar), "core" (the code sphere without the face - the old
// "cell" value maps here), "storm" (arcs race the code). orb_hue shifts the palette, orb_energy
// the tempo. Brendan's star (Orb.tsx) is untouched; orb_style swaps between the two.
import { Canvas, useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";
import { HelixLayer, NeuralLayer } from "../pages/Console";
import "../pages/console.css";
import { baseLook } from "./Orb";

export interface OrganismLook { phase: "face" | "core" | "storm" | "cell"; hue: number; energy: number }

const VERT = /* glsl */`
uniform float uTime;
uniform float uPulse;     // ring position 0..1 (0 = at the face, 1 = wrapped round the back), <0 = none
uniform float uSwell;
varying vec3 vN;
varying vec3 vNw;
varying vec3 vP;
varying vec3 vW;
varying vec2 vUv;
vec3 hash3(vec3 p) { p = vec3(dot(p, vec3(127.1, 311.7, 74.7)), dot(p, vec3(269.5, 183.3, 246.1)), dot(p, vec3(113.5, 271.9, 124.6))); return -1.0 + 2.0 * fract(sin(p) * 43758.5453123); }
float noise(vec3 p) {
  vec3 i = floor(p), f = fract(p); vec3 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(dot(hash3(i + vec3(0,0,0)), f - vec3(0,0,0)), dot(hash3(i + vec3(1,0,0)), f - vec3(1,0,0)), u.x),
                 mix(dot(hash3(i + vec3(0,1,0)), f - vec3(0,1,0)), dot(hash3(i + vec3(1,1,0)), f - vec3(1,1,0)), u.x), u.y),
             mix(mix(dot(hash3(i + vec3(0,0,1)), f - vec3(0,0,1)), dot(hash3(i + vec3(1,0,1)), f - vec3(1,0,1)), u.x),
                 mix(dot(hash3(i + vec3(0,1,1)), f - vec3(0,1,1)), dot(hash3(i + vec3(1,1,1)), f - vec3(1,1,1)), u.x), u.y), u.z);
}
void main() {
  vec3 p = position;
  float d = noise(p * 1.4 + uTime * 0.2) * 0.04 + uSwell * 0.05;
  // the pulse ring: a bulge travelling from the front pole (+z in view space ~ the face) to the back
  vec4 vpos = modelViewMatrix * vec4(position, 1.0);
  vec3 vn = normalize(normalMatrix * normal);
  float ang = acos(clamp(vn.z, -1.0, 1.0)) / 3.14159;     // 0 at the face, 1 at the back
  if (uPulse >= 0.0) { float ring = 1.0 - smoothstep(0.0, 0.12, abs(ang - uPulse)); d += ring * 0.09 * (1.0 - uPulse * 0.6); }
  p += normal * d;
  vN = vn;
  vNw = normalize(mat3(modelMatrix) * normal);
  vP = p;
  vUv = uv;
  vW = (modelMatrix * vec4(p, 1.0)).xyz;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}
`;

const FRAG = /* glsl */`
precision highp float;
uniform float uTime;
uniform sampler2D uBits;
uniform vec3 uColor;
uniform vec3 uHueA;
uniform vec3 uHueB;
uniform float uPhaseFace;
uniform float uPhaseStorm;
uniform float uAttend;
uniform float uLevel;     // voice level, smoothed
uniform float uOpen;      // mouth opening 0..1
uniform float uBlink;
uniform float uBrow;      // brows lift 0..1
uniform vec2 uGaze;
uniform vec2 uFace;      // where the face sits on the sphere: it slides toward the mouse = the head turns
uniform float uTilt;     // head tilt, radians
uniform float uSmile;    // corners of the mouth: -1 down .. 1 up
uniform float uSquint;   // lids half-closed 0..1 (thinking, emphasis)
uniform float uEnergy;
uniform float uPulse;
uniform float uFlash;     // whole-body flash 0..1
varying vec3 vN;
varying vec3 vNw;
varying vec3 vP;
varying vec3 vW;
varying vec2 vUv;

float sdSeg(vec2 p, vec2 a, vec2 b) { vec2 pa = p - a, ba = b - a; float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0); return length(pa - ba * h); }

void main() {
  vec3 n = normalize(vNw);
  vec3 v = normalize(cameraPosition - vW);
  float fres = pow(1.0 - max(dot(n, v), 0.0), 2.4);
  float t = uTime * uEnergy;
  vec3 vn = normalize(vN);
  float front = smoothstep(0.1, 0.5, vn.z);

  // THE CODE: binary glyphs stream down the skin; two layers at different speeds, lit by the pulse
  vec2 uv1 = vec2(vUv.x * 3.0, vUv.y * 1.6 + t * 0.06);
  vec2 uv2 = vec2(vUv.x * 5.0 + 0.3, vUv.y * 2.6 - t * 0.11);
  float bits = texture2D(uBits, uv1).r * 0.75 + texture2D(uBits, uv2).r * 0.45;
  // scanlines and a latitude grid: the hologram
  float scan = 0.5 + 0.5 * sin(vUv.y * 420.0 + t * 3.0);
  float grid = (1.0 - smoothstep(0.0, 0.06, abs(fract(vUv.y * 18.0) - 0.5) * 2.0 - 0.94)) * 0.35
             + (1.0 - smoothstep(0.0, 0.05, abs(fract(vUv.x * 24.0) - 0.5) * 2.0 - 0.95)) * 0.2;

  vec3 base = mix(vec3(0.01, 0.025, 0.045), uHueA * 0.10, 0.7);
  vec3 col = base;
  float faceDim = 1.0 - 0.55 * front * uPhaseFace * (1.0 - smoothstep(0.35, 0.7, length(vn.xy)));
  col += uHueA * bits * (0.34 + 0.3 * uAttend) * (0.75 + 0.25 * scan) * faceDim;
  col += uHueA * grid * 0.25;
  col += mix(uHueB, uColor, 0.5) * fres * (0.9 + 0.5 * uAttend);
  float lit = max(dot(n, normalize(vec3(0.3, 0.6, 0.75))), 0.0);
  col *= 0.7 + 0.5 * lit;

  // THE PULSE: a ring of light leaving the face and wrapping the sphere
  float ang = acos(clamp(vn.z, -1.0, 1.0)) / 3.14159;
  if (uPulse >= 0.0) {
    float ring = 1.0 - smoothstep(0.0, 0.10, abs(ang - uPulse));
    col += mix(vec3(1.0), uColor, 0.4) * ring * (1.2 - uPulse * 0.8);
  }
  col += mix(uHueB, uColor, 0.5) * uFlash * 0.35;

  // THE STORM
  float arc = pow(bits, 2.0) * (0.5 + 0.5 * sin(t * 9.0 + vUv.y * 60.0 + vUv.x * 30.0));
  col += vec3(0.85, 0.95, 1.0) * arc * uPhaseStorm * 1.4;

  // THE FACE, in view space so it always faces you while the code turns beneath it
  if (uPhaseFace > 0.001) {
    vec2 uv = vn.xy - uFace;
    float ct = cos(uTilt), st = sin(uTilt);
    uv = vec2(uv.x * ct - uv.y * st, uv.x * st + uv.y * ct);
    vec2 gaze = uGaze * 0.11;
    vec3 ink = base * 0.35;
    vec3 glow = mix(uHueB, uColor, 0.5);
    // the code fades where the features sit so they read clean
    float clear = 0.0;
    for (int e = 0; e < 2; e++) {
      float side = e == 0 ? -1.0 : 1.0;
      vec2 c = vec2(side * 0.30, 0.16) + gaze;
      vec2 d = uv - c;
      // the eye: an almond, lids close it
      float lid = max(0.06, 1.0 - max(uBlink, uSquint * 0.55));
      float almond = length(vec2(d.x / 0.15, d.y / (0.085 * lid)));
      float eye = 1.0 - smoothstep(0.92, 1.0, almond);
      float r = length(d - gaze * 0.35);
      float iris = (1.0 - smoothstep(0.065, 0.085, r)) * smoothstep(0.03, 0.04, r);
      float irisRays = 0.7 + 0.3 * sin(atan(d.y, d.x) * 18.0 + t * 2.0);
      float pupil = 1.0 - smoothstep(0.026, 0.036, r);
      float spec = 1.0 - smoothstep(0.0, 0.022, length(d - vec2(0.03, 0.035)));
      vec3 eyeCol = mix(mix(ink, glow, 0.12), glow * 1.7 * irisRays, iris);
      eyeCol = mix(eyeCol, vec3(0.0), pupil);
      eyeCol += vec3(1.0) * spec * 0.9 * lid;
      float mask = eye * front * uPhaseFace;
      col = mix(col, eyeCol, mask);
      // the lid line, and the brow above it (lifts with uBrow)
      float lidLine = (1.0 - smoothstep(0.0, 0.012, abs(almond - 1.0) * 0.1)) * (1.0 - smoothstep(0.15, 0.19, abs(d.x)));
      col += glow * lidLine * 0.6 * front * uPhaseFace;
      float knit = max(0.0, -uBrow);                       // a furrow: the brows drop and the inner ends dip
      vec2 b = uv - vec2(side * (0.30 + knit * 0.03), 0.33 + uBrow * 0.05) - gaze * 0.5;
      float browCurve = b.y - (0.05 * (1.0 - b.x * b.x * 18.0)) - side * b.x * (0.15 + knit * 0.35);
      float brow = (1.0 - smoothstep(0.006, 0.016, abs(browCurve))) * (1.0 - smoothstep(0.15, 0.20, abs(b.x)));
      col += glow * brow * 1.1 * front * uPhaseFace;
      clear = max(clear, eye + brow);
    }
    // THE LIPS: two curves; the lower drops with uOpen, the corners stay; the cavity is dark
    vec2 m = uv - vec2(0.0, -0.30);
    float span = 1.0 - smoothstep(0.24, 0.31, abs(m.x));
    float corner = 1.0 - m.x * m.x * 9.0;                      // 1 at the centre, 0 at the corners
    float lift = uSmile * 0.075 * min(1.0, m.x * m.x * 9.0);   // the corners rise with a smile, fall with a frown
    float upper = m.y - lift + 0.03 * corner + 0.03 * uOpen * corner;
    float lower = m.y - lift + 0.03 * corner - 0.008 - 0.15 * uOpen * corner;
    float cavity = smoothstep(0.0, 0.006, -upper) * smoothstep(0.0, 0.006, lower) * span;   // between the lips
    float lipU = 1.0 - smoothstep(0.004, 0.011, abs(upper));
    float lipL = 1.0 - smoothstep(0.004, 0.011, abs(lower));
    col = mix(col, ink * 0.5, cavity * front * uPhaseFace);
    // a glow inside the open mouth, brighter with the voice: the voice is light
    float inner = cavity * (0.25 + 0.9 * uLevel) * (1.0 - smoothstep(0.0, 0.16 * uOpen + 0.02, abs(m.y + 0.06 * uOpen)));
    col += glow * inner * front * uPhaseFace;
    col += glow * max(lipU, lipL) * span * 1.3 * front * uPhaseFace;
    col += glow * (1.0 - smoothstep(0.0, 0.035, min(abs(upper), abs(lower)))) * span * 0.12 * front * uPhaseFace;
  }

  gl_FragColor = vec4(col, 1.0);
}
`;

function hueToRgb(h: number, s: number, l: number): THREE.Color {
  return new THREE.Color().setHSL(((h % 360) + 360) % 360 / 360, s, l);
}

/** A texture of 0s and 1s, re-rolled a few rows at a time so the code never sits still. */
function useBitsTexture(hue: number) {
  return useMemo(() => {
    const c = document.createElement("canvas");
    c.width = 512; c.height = 512;
    const g = c.getContext("2d")!;
    const tex = new THREE.CanvasTexture(c);
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    const cols = 28, rows = 30, cw = c.width / cols, rh = c.height / rows;
    const draw = (row: number) => {
      g.clearRect(0, row * rh, c.width, rh);
      g.font = `bold ${Math.floor(rh * 0.85)}px ui-monospace, Menlo, Consolas, monospace`;
      g.textBaseline = "top";
      for (let x = 0; x < cols; x++) {
        const v = Math.random();
        if (v < 0.5) continue;                                   // gaps: it is code, not a wall
        g.fillStyle = `rgba(255,255,255,${(0.35 + Math.random() * 0.65).toFixed(2)})`;
        g.fillText(Math.random() < 0.5 ? "0" : "1", x * cw + cw * 0.15, row * rh);
      }
    };
    for (let r = 0; r < rows; r++) draw(r);
    tex.needsUpdate = true;
    const id = window.setInterval(() => { for (let k = 0; k < 3; k++) draw(Math.floor(Math.random() * rows)); tex.needsUpdate = true; }, 90);
    (tex as THREE.CanvasTexture & { _stop?: () => void })._stop = () => window.clearInterval(id);
    return tex;
  }, [hue]); // eslint-disable-line react-hooks/exhaustive-deps
}

// THE PERFORMANCE: the face reads the line it is about to say and acts it out - a mouth shape per
// syllable, a pause at every comma, and an expression per sentence: brows up for a question, wide
// for an exclamation, a squint and a nod on the words that carry weight. Timed at speaking pace
// (~2.6 words/s), which is what the OS voice and the mimed orb both run at; the real voice may
// drift a little, and the face keeps a gentle idle flutter once its script runs out.
interface Beat { at: number; dur: number; open: number }
interface Line { at: number; end: number; brow: number; smile: number; tilt: number; glance: number; nods: number[] }
interface Script { beats: Beat[]; lines: Line[]; total: number }
const WEIGHT = /^(not|never|must|always|now|done|ready|live|prod|production|failed|error|warning|yes|no|stop|every|all|nothing)$/i;
function buildScript(text: string): Script {
  const beats: Beat[] = []; const lines: Line[] = [];
  let t = 0.25;
  const sentences = text.replace(/\s+/g, " ").match(/[^.!?]+[.!?]*/g) || [text];
  for (const raw of sentences) {
    const sent = raw.trim(); if (!sent) continue;
    const q = /\?$/.test(sent), bang = /!$/.test(sent);
    const line: Line = {
      at: t, end: t, brow: q ? 0.8 : bang ? 1 : 0.1 + Math.random() * 0.25,
      smile: bang ? 0.55 : q ? 0.05 : 0.15 + Math.random() * 0.15,
      tilt: q ? (Math.random() < 0.5 ? -1 : 1) * 0.09 : (Math.random() - 0.5) * 0.05,
      glance: Math.random() < 0.45 ? (Math.random() < 0.5 ? -1 : 1) : 0, nods: [],
    };
    for (const word of sent.split(" ")) {
      const bare = word.replace(/[^A-Za-z0-9']/g, "");
      if (!bare) continue;
      const heavy = WEIGHT.test(bare) || (bare.length > 2 && bare === bare.toUpperCase()) || /\d/.test(bare);
      if (heavy) line.nods.push(t);
      const syl = Math.max(1, Math.round(bare.length / 2.8));
      for (let k = 0; k < syl; k++) {
        const vowels = (bare.match(/[aeiouy]/gi) || []).length / Math.max(1, bare.length);
        const dur = 0.11 + Math.random() * 0.05 + (heavy ? 0.05 : 0);
        beats.push({ at: t, dur, open: Math.min(1, 0.35 + vowels * 0.9 + (heavy ? 0.2 : 0) + Math.random() * 0.15) });
        t += dur;
      }
      t += /[,;:]$/.test(word) ? 0.16 : 0.04;                      // a breath at the commas
    }
    line.end = t;
    t += q || bang ? 0.34 : 0.28;                                   // the full stop
    lines.push(line);
  }
  return { beats, lines, total: t };
}

function Body({ look, onPulse }: { look: OrganismLook; onPulse: (big: boolean) => void }) {
  const mesh = useRef<THREE.Mesh>(null!);
  const spores = useRef<THREE.Points>(null!);
  const gazeRef = useRef({ x: 0, y: 0 });
  const stateColor = useMemo(() => new THREE.Color(), []);
  const blink = useRef({ next: 3, t: -1 });
  const pulse = useRef({ t: -1, speed: 1 });
  const flash = useRef(0);
  const swell = useRef(0);
  const prev = useRef({ orb: "idle", hue: "none", bubbles: 0 });
  const mouth = useRef({ open: 0, flutter: 0 });
  const mouse = useRef({ x: 0, y: 0 });
  const perf = useRef<{ script: Script | null; t0: number; beat: number; nod: number; glanceT: number; endSmile: number }>({ script: null, t0: 0, beat: 0, nod: -1, glanceT: 0, endSmile: 0 });
  const expr = useRef({ brow: 0, smile: 0, tilt: 0, squint: 0, nod: 0, glance: 0 });
  // THE IDLE LIFE: every few seconds a small act - a glance aside, a brow flick, a double blink,
  // a head tilt, a half smile - so the face is alive between lines. Thinking gets its own acts:
  // eyes up and away, a furrow, a squint, as if reading something over your shoulder.
  const life = useRef({ next: 2.5, act: "", until: 0, gx: 0, gy: 0, brow: 0, tilt: 0, smile: 0, squint: 0, blinks: 0 });
  // the mouse anywhere on the page turns the head - not only over the canvas
  useEffect(() => {
    const move = (e: MouseEvent) => { mouse.current.x = (e.clientX / window.innerWidth) * 2 - 1; mouse.current.y = -((e.clientY / window.innerHeight) * 2 - 1); };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, []);
  const bits = useBitsTexture(look.hue);
  useEffect(() => () => (bits as THREE.CanvasTexture & { _stop?: () => void })._stop?.(), [bits]);
  const phase = look.phase === "cell" ? "core" : look.phase;

  const uniforms = useMemo(() => ({
    uTime: { value: 0 }, uSwell: { value: 0 }, uPulse: { value: -1 }, uFlash: { value: 0 },
    uBits: { value: bits },
    uColor: { value: new THREE.Color(0.16, 0.55, 1.0) },
    uHueA: { value: hueToRgb(look.hue, 0.85, 0.55) }, uHueB: { value: hueToRgb(look.hue + 40, 0.9, 0.62) },
    uPhaseFace: { value: phase === "face" ? 1 : 0 }, uPhaseStorm: { value: phase === "storm" ? 1 : 0 },
    uAttend: { value: 0 }, uLevel: { value: 0 }, uOpen: { value: 0 }, uBlink: { value: 0 }, uBrow: { value: 0 },
    uGaze: { value: new THREE.Vector2() }, uEnergy: { value: look.energy },
    uFace: { value: new THREE.Vector2() }, uTilt: { value: 0 }, uSmile: { value: 0 }, uSquint: { value: 0 },
  }), []); // eslint-disable-line react-hooks/exhaustive-deps

  const sporeGeo = useMemo(() => {
    const n = 320;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = 1.3 + Math.random() * 1.4, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
      pos[i * 3] = r * Math.sin(ph) * Math.cos(th); pos[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th); pos[i * 3 + 2] = r * Math.cos(ph);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return g;
  }, []);
  const glow = useMemo(() => {
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const g = c.getContext("2d")!; const gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(c);
  }, []);

  const firePulse = (big: boolean) => {
    pulse.current.t = 0; pulse.current.speed = big ? 1.1 : 1.8;
    flash.current = big ? 1 : 0.5; swell.current = big ? 1 : 0.45;
    onPulse(big);
  };

  useFrame(({ camera }, dtRaw) => {
    const pointer = mouse.current;
    const dt = Math.min(0.05, dtRaw);
    const s = useHelix.getState();
    const L = baseLook(s);
    const u = uniforms;
    u.uTime.value += dt;
    u.uBits.value = bits;
    u.uEnergy.value += (look.energy - u.uEnergy.value) * 0.05;
    u.uHueA.value.lerp(hueToRgb(look.hue, 0.85, 0.55), 0.05);
    u.uHueB.value.lerp(hueToRgb(look.hue + 40, 0.9, 0.62), 0.05);
    stateColor.setRGB(L.color[0], L.color[1], L.color[2]);
    u.uColor.value.lerp(stateColor, 0.06);
    u.uAttend.value += ((s.orb === "listening" ? 1 : 0) - u.uAttend.value) * 0.08;
    const speaking = s.orb === "speaking";
    const level = speaking ? 0.3 + s.level * 0.9 : s.orb === "transcribing" ? s.level * 0.6 : 0;
    u.uLevel.value += (level - u.uLevel.value) * 0.3;
    u.uPhaseFace.value += ((phase === "face" ? 1 : 0) - u.uPhaseFace.value) * 0.04;
    u.uPhaseStorm.value += ((phase === "storm" ? 1 : 0) - u.uPhaseStorm.value) * 0.04;

    // THE KEY MOMENTS -> pulses
    const p = prev.current;
    if (s.orb !== p.orb) {
      if (s.orb === "thinking") firePulse(false);
      if (s.orb === "speaking") {
        firePulse(true);
        const last = [...s.bubbles].reverse().find((b) => b.role === "helix");
        perf.current = { script: last ? buildScript(last.text) : null, t0: performance.now() / 1000, beat: 0, nod: -1, glanceT: 0, endSmile: 0 };
      }
      if (p.orb === "speaking") { perf.current.script = null; perf.current.endSmile = 1; }
      p.orb = s.orb;
    }
    if (s.bubbles.length !== p.bubbles) {
      const last = s.bubbles[s.bubbles.length - 1];
      if (s.bubbles.length > p.bubbles && last && last.role === "helix") firePulse(false);
      p.bubbles = s.bubbles.length;
    }
    if (s.hue !== p.hue) { if (s.hue === "done" || s.hue === "error") firePulse(true); p.hue = s.hue; }
    if (pulse.current.t >= 0) {
      pulse.current.t += dt * pulse.current.speed;
      u.uPulse.value = Math.min(1, pulse.current.t);
      if (pulse.current.t > 1) { pulse.current.t = -1; u.uPulse.value = -1; }
    }
    flash.current *= Math.pow(0.02, dt); u.uFlash.value = flash.current;
    swell.current *= Math.pow(0.05, dt); u.uSwell.value = swell.current;

    // THE LIPS: open with the voice; syllable flutter on top so it talks rather than yawns
    const m = mouth.current;
    const e = expr.current;
    const pf = perf.current;
    let target = 0;
    let brow = 0, smile = s.orb === "idle" ? 0.12 : 0, tilt = 0, squint = 0;
    // the idle / thinking acts
    const lf = life.current;
    const thinking = s.orb === "thinking";
    const now = u.uTime.value;
    if (!speaking) {
      lf.next -= dt;
      if (lf.next <= 0) {
        const acts = thinking
          ? ["lookup", "lookup", "furrow", "squint", "lookaside", "tilt"]
          : ["glance", "glance", "flick", "doubleblink", "tilt", "halfsmile", "settle"];
        lf.act = acts[Math.floor(Math.random() * acts.length)];
        lf.until = now + (thinking ? 1.2 + Math.random() * 1.6 : 0.6 + Math.random() * 1.2);
        lf.next = thinking ? 1.2 + Math.random() * 1.6 : 2.2 + Math.random() * 4;
        const side = Math.random() < 0.5 ? -1 : 1;
        lf.gx = 0; lf.gy = 0; lf.brow = 0; lf.tilt = 0; lf.smile = 0; lf.squint = 0;
        if (lf.act === "glance") { lf.gx = side * 0.7; lf.gy = (Math.random() - 0.4) * 0.4; }
        if (lf.act === "lookup") { lf.gx = side * 0.55; lf.gy = 0.75; lf.brow = 0.35; }
        if (lf.act === "lookaside") { lf.gx = side * 0.9; lf.gy = -0.1; lf.squint = 0.3; }
        if (lf.act === "flick") { lf.brow = 0.7; lf.until = now + 0.35; }
        if (lf.act === "furrow") { lf.brow = -0.6; lf.squint = 0.35; lf.gy = -0.2; }
        if (lf.act === "squint") { lf.squint = 0.6; lf.brow = -0.2; }
        if (lf.act === "tilt") { lf.tilt = side * 0.1; }
        if (lf.act === "halfsmile") { lf.smile = 0.5; lf.until = now + 1.4; }
        if (lf.act === "doubleblink") { lf.blinks = 2; blink.current.next = 0; }
      }
      if (now < lf.until) { brow += lf.brow; tilt += lf.tilt; smile += lf.smile; squint += lf.squint; e.glance += (lf.gx - e.glance) * 0.1; }
      else { e.glance *= Math.pow(0.05, dt); }
      if (thinking) { brow += -0.15; squint += 0.2; smile = 0.02; }
    }
    const lifeGy = !speaking && now < lf.until ? lf.gy : 0;
    const sc = pf.script;
    if (speaking && sc) {
      const tt = performance.now() / 1000 - pf.t0;   // wall clock: the voice does not slow down when the frames do
      while (pf.beat < sc.beats.length && sc.beats[pf.beat].at + sc.beats[pf.beat].dur < tt) pf.beat++;
      const b = sc.beats[pf.beat];
      if (b && tt >= b.at) target = b.open * Math.sin(Math.min(1, (tt - b.at) / b.dur) * Math.PI) * (0.6 + 0.4 * Math.min(1, u.uLevel.value + 0.5));
      else if (tt > sc.total) { m.flutter += dt * 11; target = 0.12 + 0.35 * Math.max(0, Math.sin(m.flutter)); }   // the script ran out, the voice has not
      const line = sc.lines.find((l) => tt >= l.at - 0.1 && tt < l.end + 0.3) || sc.lines[sc.lines.length - 1];
      if (line) {
        brow = line.brow; smile = line.smile; tilt = line.tilt;
        if (line.glance && tt < line.at + 0.7) e.glance += (line.glance * 0.6 - e.glance) * 0.12;
        const nodAt = line.nods.find((n) => tt >= n && tt < n + 0.35);
        if (nodAt !== undefined && pf.nod !== nodAt) { pf.nod = nodAt; e.nod = 1; squint = 0.5; }
      }
    } else if (speaking) {
      m.flutter += dt * (9 + s.level * 14);
      target = Math.min(1, 0.15 + u.uLevel.value * 0.9 + 0.25 * Math.max(0, Math.sin(m.flutter)) * u.uLevel.value);
      brow = 0.2;
    }
    if (!speaking && pf.endSmile > 0) { smile = 0.45 * pf.endSmile; pf.endSmile = Math.max(0, pf.endSmile - dt * 0.5); }
    m.open += (target - m.open) * (speaking ? 0.5 : 0.15);
    u.uOpen.value = m.open;
    e.brow += (brow - e.brow) * 0.12; e.smile += (smile - e.smile) * 0.08; e.tilt += (tilt - e.tilt) * 0.06;
    e.squint += (squint - e.squint) * 0.1; e.nod *= Math.pow(0.03, dt); if (speaking && !sc) e.glance *= Math.pow(0.05, dt);
    u.uBrow.value = e.brow + flash.current * 0.8;
    u.uSmile.value = e.smile; u.uSquint.value = e.squint;
    u.uTilt.value = e.tilt + pointer.x * 0.06;

    gazeRef.current.x += (pointer.x * 1.4 + e.glance - gazeRef.current.x) * 0.08;
    gazeRef.current.y += (pointer.y * 1.2 + lifeGy - e.nod * 0.5 - gazeRef.current.y) * 0.08;
    u.uGaze.value.set(gazeRef.current.x, gazeRef.current.y);
    // the head: the face slides toward the mouse across the sphere, and dips on a nod
    u.uFace.value.x += (pointer.x * 0.30 + e.glance * 0.05 - u.uFace.value.x) * 0.05;
    u.uFace.value.y += (pointer.y * 0.22 - e.nod * 0.07 - u.uFace.value.y) * 0.05;
    const b = blink.current;
    b.next -= dt;
    if (b.next <= 0) { b.t = 0; b.next = lf.blinks > 0 ? 0.32 : 2.2 + Math.random() * 3.5; if (lf.blinks > 0) lf.blinks--; }
    if (b.t >= 0) { b.t += dt; u.uBlink.value = Math.sin(Math.min(1, b.t / 0.22) * Math.PI); if (b.t > 0.22) { b.t = -1; u.uBlink.value = 0; } }

    mesh.current.rotation.y += dt * 0.1 * look.energy;
    mesh.current.rotation.x += ((-pointer.y * 0.12 + e.nod * 0.05) - mesh.current.rotation.x) * 0.04;
    mesh.current.scale.setScalar(1 + swell.current * 0.06);
    spores.current.rotation.y -= dt * 0.05; spores.current.rotation.z += dt * 0.02;
    const sm = spores.current.material as THREE.PointsMaterial;
    sm.color.copy(u.uHueA.value).lerp(stateColor, 0.4);
    sm.size = 0.03 + 0.03 * u.uLevel.value + 0.05 * flash.current;
    camera.lookAt(0, 0, 0);
  });

  const tap = () => void api.post("/api/shell/tap").catch(() => undefined);

  return (
    <group>
      <mesh ref={mesh} onClick={tap}>
        <sphereGeometry args={[1.02, 128, 128]} />
        <shaderMaterial vertexShader={VERT} fragmentShader={FRAG} uniforms={uniforms} />
      </mesh>
      <points ref={spores} geometry={sporeGeo} raycast={() => undefined}>
        <pointsMaterial map={glow} size={0.03} transparent opacity={0.7} depthWrite={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
    </group>
  );
}

/** THE CURTAIN: columns of 0s and 1s fall across the whole window and thin out to reveal the
 *  avatar - on open, and a short burst on every big pulse. */
function Curtain({ trigger, hue }: { trigger: number; hue: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = ref.current; if (!canvas) return;
    const ctx = canvas.getContext("2d"); if (!ctx) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = window.innerWidth, h = window.innerHeight;
    canvas.width = w * dpr; canvas.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const size = 16, cols = Math.ceil(w / size);
    const heads = Array.from({ length: cols }, () => -Math.random() * h);
    const speed = Array.from({ length: cols }, () => 380 + Math.random() * 520);
    const life = trigger === 0 ? 2.4 : 0.9;
    let t = 0, last = performance.now(), raf = 0, alive = true;
    const color = `hsl(${hue} 85% 60%)`;
    const step = (now: number) => {
      if (!alive) return;
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      const fade = t < life * 0.7 ? 1 : Math.max(0, 1 - (t - life * 0.7) / (life * 0.3));
      ctx.fillStyle = "rgba(8, 11, 15, 0.22)"; ctx.fillRect(0, 0, w, h);
      ctx.font = `${size}px ui-monospace, Menlo, Consolas, monospace`;
      ctx.textBaseline = "top";
      for (let i = 0; i < cols; i++) {
        heads[i] += speed[i] * dt;
        const y = heads[i];
        for (let k = 0; k < 10; k++) {
          const yy = y - k * size;
          if (yy < -size || yy > h) continue;
          const a = (k === 0 ? 1 : 0.55 * (1 - k / 10)) * fade;
          ctx.fillStyle = k === 0 ? `rgba(230,255,255,${a})` : color.replace(")", ` / ${a.toFixed(2)})`).replace("hsl(", "hsl(");
          ctx.fillText(Math.random() < 0.5 ? "0" : "1", i * size, yy);
        }
        if (y > h + size * 10) { heads[i] = -Math.random() * h * 0.5; }
      }
      if (t < life) raf = requestAnimationFrame(step);
      else { ctx.clearRect(0, 0, w, h); canvas.style.opacity = "0"; }
    };
    canvas.style.opacity = "1";
    raf = requestAnimationFrame(step);
    return () => { alive = false; cancelAnimationFrame(raf); };
  }, [trigger, hue]);
  return <canvas ref={ref} className="fixed inset-0" style={{ zIndex: 1, pointerEvents: "none", transition: "opacity 0.6s", mixBlendMode: "screen" }} />;
}

/** The room behind the face: the Console's helix and neural net, dimmed, so the avatar sits in
 *  the same world as the rest of HELIX (and the net still breaks under the mouse here). */
function Room() {
  const [colors, setColors] = useState<[string, string]>(["#3fe0e0", "#2a8cff"]);
  useEffect(() => {
    const read = () => void api.get<{ values?: Record<string, unknown> }>("/api/settings").then((d) => {
      const a = String(d.values?.helix_color_a || ""), b = String(d.values?.helix_color_b || "");
      setColors([a || "#3fe0e0", b || "#2a8cff"]);
    }).catch(() => undefined);
    read();
    window.addEventListener("helix-settings-saved", read);
    return () => window.removeEventListener("helix-settings-saved", read);
  }, []);
  return (
    <div className="board-stage" style={{ position: "fixed", zIndex: 0, opacity: 0.55, pointerEvents: "none" }} aria-hidden="true">
      <HelixLayer colors={colors} />
      <NeuralLayer />
      <div className="board-layer" style={{ background: "radial-gradient(ellipse at 50% 48%, rgba(8,11,15,0.7) 0%, rgba(8,11,15,0.25) 34%, transparent 60%)" }} />
    </div>
  );
}

export default function Organism({ look }: { look: OrganismLook }) {
  const [curtain, setCurtain] = useState(0);
  return (
    <>
      <Room />
      <div className="fixed inset-0" style={{ zIndex: 0 }}>
        <Canvas camera={{ position: [0, 0.2, 4.2], fov: 42 }} gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
          dpr={[1, 2]} style={{ pointerEvents: "auto" }}>
          <Body look={look} onPulse={(big) => { if (big) setCurtain((c) => c + 1); }} />
        </Canvas>
      </div>
      <Curtain trigger={curtain} hue={look.hue} />
    </>
  );
}
