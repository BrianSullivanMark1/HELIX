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
import type React from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";
import { useJobs } from "../lib/jobs";
import { radioBeat } from "./Radio";
import { ContextGuard, FrameThrottle, HelixBackdrop, NeuralLayer, usePerfLevel } from "../pages/Console";
import { dprCap, perf } from "../lib/perf";
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
uniform float uWide;      // mouth width 0.5 (an O) .. 1.25 (an E)
uniform float uTeeth;     // teeth showing 0..1 (F, V, S)
uniform float uTongue;    // tongue up 0..1 (L, T, D, N)
uniform float uBlink;
uniform float uBrow;      // brows lift 0..1
uniform vec2 uGaze;
uniform vec2 uFace;      // where the face sits on the sphere: it slides toward the mouse = the head turns
uniform float uTilt;     // head tilt, radians
uniform float uSmile;    // corners of the mouth: -1 down .. 1 up
uniform float uSquint;   // lids half-closed 0..1 (thinking, emphasis)
uniform float uWink;     // one eye shut: sign = which eye (-1 left, 1 right), magnitude = how far
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
      float wink = side * uWink > 0.0 ? abs(uWink) : 0.0;
      float lid = max(0.06, 1.0 - max(max(uBlink, uSquint * 0.55), wink));
      float almond = length(vec2(d.x / 0.15, d.y / (0.085 * lid)));
      float eye = 1.0 - smoothstep(0.92, 1.0, almond);
      vec2 dg = d - gaze * 0.35;
      float r = length(dg);
      float ang = atan(dg.y, dg.x);
      // the iris: a big warm disc with a darker limbal ring, fibres, and a soft inner gradient
      float irisR = 0.082 + 0.006 * uSmile;                  // a smile widens the eye a touch
      float iris = (1.0 - smoothstep(irisR - 0.006, irisR + 0.004, r)) * smoothstep(0.026, 0.036, r);
      float limbal = smoothstep(irisR - 0.018, irisR - 0.004, r) * (1.0 - smoothstep(irisR - 0.004, irisR + 0.004, r));
      float fibres = 0.78 + 0.22 * sin(ang * 26.0 + t * 0.8) * sin(ang * 7.0 - t * 0.3);
      float inner = 1.0 - smoothstep(0.03, irisR, r);
      float pupil = 1.0 - smoothstep(0.024, 0.034, r);
      vec3 irisCol = mix(glow * 1.5 * fibres, mix(uHueB, vec3(1.0), 0.35) * 1.3, inner * 0.55);
      irisCol = mix(irisCol, ink * 0.6, limbal * 0.8);
      float spec = 1.0 - smoothstep(0.0, 0.020, length(d - vec2(0.028, 0.036)));
      float spec2 = 1.0 - smoothstep(0.0, 0.010, length(d - vec2(-0.022, -0.026)));
      vec3 white = mix(ink, glow, 0.16) + vec3(0.06);
      vec3 eyeCol = mix(white, irisCol, iris);
      eyeCol = mix(eyeCol, vec3(0.0), pupil);
      eyeCol += vec3(1.0) * (spec * 0.95 + spec2 * 0.45) * lid;
      float mask = eye * front * uPhaseFace;
      col = mix(col, eyeCol, mask);
      // the lid lines: the upper lid heavier (lashes), the lower a faint line; a crease above
      float lidLine = (1.0 - smoothstep(0.0, 0.012, abs(almond - 1.0) * 0.1)) * (1.0 - smoothstep(0.15, 0.19, abs(d.x)));
      float upperHalf = smoothstep(-0.01, 0.02, d.y);
      col += glow * lidLine * (0.35 + 0.65 * upperHalf) * front * uPhaseFace;
      float crease = (1.0 - smoothstep(0.004, 0.012, abs(d.y - 0.11 - 0.02 * uBrow + d.x * d.x * 2.2))) * (1.0 - smoothstep(0.10, 0.15, abs(d.x)));
      col += glow * crease * 0.22 * front * uPhaseFace;
      // the brow: a tapered stroke, thick over the eye, thinning to the ends; lifts with uBrow,
      // knits with a furrow; a soft glow under it so it reads as a shape, not a line
      float knit = max(0.0, -uBrow);
      vec2 b = uv - vec2(side * (0.30 + knit * 0.03), 0.34 + uBrow * 0.05) - gaze * 0.5;
      float browCurve = b.y - (0.05 * (1.0 - b.x * b.x * 18.0)) - side * b.x * (0.12 + knit * 0.35) - 0.02 * uSmile * (1.0 - b.x * b.x * 20.0);
      float thick = 0.016 * (1.0 - smoothstep(0.0, 0.18, abs(b.x + side * 0.03))) + 0.005;
      float brow = (1.0 - smoothstep(thick * 0.6, thick, abs(browCurve))) * (1.0 - smoothstep(0.16, 0.21, abs(b.x)));
      col += glow * brow * 1.15 * front * uPhaseFace;
      col += glow * (1.0 - smoothstep(0.0, 0.05, abs(browCurve))) * (1.0 - smoothstep(0.14, 0.2, abs(b.x))) * 0.10 * front * uPhaseFace;
      // a cheek that warms with a smile
      float cheek = (1.0 - smoothstep(0.0, 0.16, length(uv - vec2(side * 0.36, -0.12)))) * max(0.0, uSmile) * 0.35;
      col += mix(glow, vec3(1.0, 0.7, 0.6), 0.3) * cheek * front * uPhaseFace;
      clear = max(clear, eye + brow);
    }
    // THE MOUTH: two lips around a real cavity. The upper lip lifts a little and the lower lip
    // drops a lot with uOpen (a jaw, not a slot); the corners rise with a smile; inside is dark
    // with a tongue of light that brightens with the voice, and a band of teeth under the top lip.
    vec2 m = uv - vec2(0.0, -0.30);
    float W = 0.27 * uWide;
    float xr = clamp(m.x / W, -1.0, 1.0);
    float c = 1.0 - xr * xr;                                    // 1 at the centre, 0 at the corners
    float span = 1.0 - smoothstep(W - 0.01, W + 0.02, abs(m.x));
    float lift = uSmile * 0.075 * xr * xr;                       // the corners rise with a smile, fall with a frown
    float bow = 0.008 * exp(-m.x * m.x * 900.0);                 // the cupid's bow
    float yU = lift + 0.011 - bow + uOpen * 0.045 * c;           // the upper lip's lower edge
    float yL = lift - 0.011 - uOpen * 0.17 * c;                  // the lower lip's upper edge
    float gap = max(0.0, yU - yL);
    float cavity = smoothstep(0.0, 0.005, m.y - yL) * (1.0 - smoothstep(-0.005, 0.0, m.y - yU)) * span * step(0.004, gap);
    float openK = smoothstep(0.004, 0.03, gap);                  // 0 closed .. 1 open: one soft line when shut, two lips when open
    float lipU = (1.0 - smoothstep(0.004, 0.012, abs(m.y - yU))) * openK;
    float lipL = (1.0 - smoothstep(0.005, 0.016, abs(m.y - yL - 0.004 * c))) * openK;       // the lower lip is fuller
    float lipShine = (1.0 - smoothstep(0.0, 0.012, abs(m.y - yL - 0.012 * c))) * c * 0.35 * (0.4 + 0.6 * openK);
    float seam = (1.0 - smoothstep(0.004, 0.012, abs(m.y - lift - bow * 0.5))) * (1.0 - openK);   // the closed mouth: one calm line
    col = mix(col, mix(base, ink, 0.55), cavity * front * uPhaseFace);
    float tongueBase = cavity * (0.12 + 0.6 * uLevel) * (1.0 - smoothstep(0.0, max(gap, 0.02) * 0.9, m.y - yL));
    float tongueUp = cavity * uTongue * (1.0 - smoothstep(0.0, max(gap, 0.02) * 0.7, abs(m.y - (yL + gap * 0.55)))) * (1.0 - smoothstep(0.0, W * 0.5, abs(m.x)));
    col += mix(glow, vec3(1.0, 0.55, 0.6), 0.35) * (tongueBase * 0.7 + tongueUp * 0.9) * front * uPhaseFace;
    float teethBand = cavity * (1.0 - smoothstep(0.0, 0.016, yU - m.y)) * max(smoothstep(0.03, 0.08, gap), uTeeth * step(0.004, gap));
    col += vec3(0.85, 0.95, 1.0) * teethBand * 0.6 * front * uPhaseFace;
    col += glow * max(max(lipU, lipL), seam) * span * 1.05 * front * uPhaseFace;
    col += mix(glow, vec3(1.0), 0.5) * lipShine * span * front * uPhaseFace;
    // dimples at the corners when smiling
    float dimple = max(0.0, uSmile) * (1.0 - smoothstep(0.0, 0.05, length(vec2(abs(m.x) - W - 0.02, m.y - lift * 1.1))));
    col += glow * dimple * 0.5 * front * uPhaseFace;
    col += glow * (1.0 - smoothstep(0.0, 0.04, min(abs(m.y - yU), abs(m.y - yL)))) * span * 0.12 * front * uPhaseFace;
    // THE VOICE, seen: a waveform of light on the sphere below the mouth while it speaks
    float wv = uLevel * uOpen;
    float wy = -0.52 + 0.035 * wv * sin(m.x * 60.0 + t * 30.0) * c;
    float wave = (1.0 - smoothstep(0.0, 0.006 + 0.004 * wv, abs(uv.y - wy))) * span * smoothstep(0.02, 0.2, wv);
    col += glow * wave * 0.8 * front * uPhaseFace;
  }
  // THE HALO: a thin ring of light around the equator, ticking with the syllables
  float halo = 1.0 - smoothstep(0.0, 0.012, abs(vn.y - 0.62 + 0.03 * sin(t * 0.7)));
  float ticks = 0.5 + 0.5 * sin(atan(vn.x, vn.z) * 24.0 + t * 2.0);
  col += mix(uHueB, uColor, 0.6) * halo * (0.25 + 0.55 * uOpen * ticks + 0.3 * uFlash) * (0.6 + 0.4 * front);

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
    c.width = 384; c.height = 384;
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
    const id = window.setInterval(() => { if (document.hidden) return; for (let k = 0; k < 2; k++) draw(Math.floor(Math.random() * rows)); tex.needsUpdate = true; }, 140);
    (tex as THREE.CanvasTexture & { _stop?: () => void })._stop = () => window.clearInterval(id);
    return tex;
  }, [hue]); // eslint-disable-line react-hooks/exhaustive-deps
}

// THE PERFORMANCE: the face reads the line it is about to say and acts it out - a mouth shape per
// syllable, a pause at every comma, and an expression per sentence: brows up for a question, wide
// for an exclamation, a squint and a nod on the words that carry weight. Timed at speaking pace
// (~2.6 words/s), which is what the OS voice and the mimed orb both run at; the real voice may
// drift a little, and the face keeps a gentle idle flutter once its script runs out.
interface Beat { at: number; dur: number; open: number; wide: number; teeth: number; tongue: number; hold?: boolean }
// THE MOUTH SHAPES (visemes), South-Park style: one shape per sound, snapped to, held, then the
// next. A a wide open jaw, O a round mouth, E/I a wide grin, M/B/P shut, F/V teeth on the lip,
// L/T/D/N the tongue up behind the teeth, S/Z/etc. a narrow slit with teeth.
const VISEME: Record<string, [number, number, number, number]> = {           // open, wide, teeth, tongue
  a: [0.85, 1.0, 0, 0], e: [0.45, 1.22, 0.2, 0], i: [0.35, 1.25, 0.3, 0], y: [0.35, 1.2, 0.3, 0],
  o: [0.7, 0.62, 0, 0], u: [0.5, 0.52, 0, 0], w: [0.4, 0.5, 0, 0], q: [0.45, 0.55, 0, 0],
  m: [0.0, 1.0, 0, 0], b: [0.0, 1.0, 0, 0], p: [0.0, 1.0, 0, 0],
  f: [0.15, 1.05, 1, 0], v: [0.15, 1.05, 1, 0],
  l: [0.45, 0.95, 0, 1], t: [0.35, 1.0, 0.4, 1], d: [0.35, 1.0, 0.3, 1], n: [0.3, 1.0, 0.2, 1],
  s: [0.22, 1.1, 0.8, 0], z: [0.22, 1.1, 0.8, 0], c: [0.3, 1.05, 0.5, 0], k: [0.4, 1.0, 0.2, 0], g: [0.4, 1.0, 0.2, 0],
  j: [0.3, 0.95, 0.5, 0], x: [0.3, 1.05, 0.5, 0], h: [0.4, 1.0, 0, 0], r: [0.35, 0.9, 0.1, 0],
};
function visemesFor(word: string, at: number, dur: number, heavy: boolean): Beat[] {
  const letters = word.toLowerCase().replace(/[^a-z]/g, "");
  if (!letters) return [];
  // merge doubles and vowel runs so "moon" is m-oo-n, not m-o-o-n
  const groups: string[] = [];
  for (const ch of letters) { const last = groups[groups.length - 1]; if (last && (last[0] === ch || (/[aeiou]/.test(last[0]) && /[aeiou]/.test(ch)))) continue; groups.push(ch); }
  const each = dur / groups.length;
  return groups.map((ch, i) => {
    const v = VISEME[ch] || [0.3, 1.0, 0, 0];
    return { at: at + i * each, dur: Math.max(0.04, each * 0.95), open: Math.min(1, v[0] * (heavy ? 1.15 : 1)), wide: v[1], teeth: v[2], tongue: v[3] };
  });
}
interface Line { at: number; end: number; brow: number; smile: number; tilt: number; glance: number; nods: number[]; squint?: number }
interface Script { beats: Beat[]; lines: Line[]; total: number }
const WEIGHT = /^(not|never|must|always|now|done|ready|live|prod|production|failed|error|warning|yes|no|stop|every|all|nothing)$/i;
// the mood of a sentence, from its words: bright words lift the brows and the corners, dark ones
// lower them; "checking" words get the eyes-up look of someone working on it
const BRIGHT = /\b(great|awesome|done|ready|live|perfect|nice|good|love|happy|yes|sure|thanks|welcome|cool|works|passed|green|clean|success|glad)\b/i;
const DARK = /\b(sorry|failed|error|cannot|can't|couldn't|down|broken|wrong|missing|refused|denied|problem|unfortunately|no\b)/i;
const CHECKING = /\b(check|checking|look|looking|grab|grabbing|fetch|fetching|read|reading|one (sec|second|moment)|hold on|wait|let me)\b/i;
function mood(sentence: string): { brow: number; smile: number; squint: number } {
  const m = { brow: 0, smile: 0, squint: 0 };
  if (BRIGHT.test(sentence)) { m.brow += 0.35; m.smile += 0.5; }
  if (DARK.test(sentence)) { m.brow -= 0.45; m.smile -= 0.35; }
  if (CHECKING.test(sentence)) { m.brow += 0.2; m.squint += 0.3; }
  return m;
}
/** The same script, but timed by the voice itself: one entry per spoken word with its offset and
 *  duration. Syllables are spread across the word's real duration; sentence expressions come from
 *  the punctuation as before. Used when the backend synthesized the line for the page. */
function buildTimedScript(words: { t: number; d: number; w: string }[]): Script {
  const beats: Beat[] = []; const lines: Line[] = [];
  let line: Line | null = null;
  let sentence = "";
  for (const wd of words) {
    const bare = wd.w.replace(/[^A-Za-z0-9']/g, "");
    sentence += wd.w + " ";
    if (!line) {
      line = { at: wd.t, end: wd.t, brow: 0.1 + Math.random() * 0.25, smile: 0.15 + Math.random() * 0.15,
               tilt: (Math.random() - 0.5) * 0.05, glance: Math.random() < 0.45 ? (Math.random() < 0.5 ? -1 : 1) : 0, nods: [] };
    }
    const heavy = WEIGHT.test(bare) || (bare.length > 2 && bare === bare.toUpperCase()) || /\d/.test(bare);
    if (heavy) line.nods.push(wd.t);
    beats.push(...visemesFor(bare, wd.t, Math.max(0.08, wd.d), heavy));
    line.end = wd.t + wd.d;
    if (/[.!?]$/.test(wd.w)) {
      const q = /\?$/.test(wd.w), bang = /!$/.test(wd.w);
      const md = mood(sentence);
      line.brow = (q ? 0.8 : bang ? 1 : line.brow) + md.brow; line.smile = (bang ? 0.55 : q ? 0.05 : line.smile) + md.smile; line.squint = md.squint;
      line.tilt = q ? (Math.random() < 0.5 ? -1 : 1) * 0.09 : line.tilt;
      lines.push(line); line = null; sentence = "";
    }
  }
  if (line) { const md = mood(sentence); line.brow += md.brow; line.smile += md.smile; line.squint = md.squint; lines.push(line); }
  const last = words[words.length - 1];
  return { beats, lines, total: last ? last.t + last.d + 0.2 : 0 };
}

function buildScript(text: string): Script {
  const beats: Beat[] = []; const lines: Line[] = [];
  let t = 0.25;
  const sentences = text.replace(/\s+/g, " ").match(/[^.!?]+[.!?]*/g) || [text];
  for (const raw of sentences) {
    const sent = raw.trim(); if (!sent) continue;
    const q = /\?$/.test(sent), bang = /!$/.test(sent);
    const md = mood(sent);
    const line: Line = {
      at: t, end: t, brow: (q ? 0.8 : bang ? 1 : 0.1 + Math.random() * 0.25) + md.brow,
      smile: (bang ? 0.55 : q ? 0.05 : 0.2 + Math.random() * 0.15) + md.smile, squint: md.squint,
      tilt: q ? (Math.random() < 0.5 ? -1 : 1) * 0.09 : (Math.random() - 0.5) * 0.05,
      glance: Math.random() < 0.45 ? (Math.random() < 0.5 ? -1 : 1) : 0, nods: [],
    };
    for (const word of sent.split(" ")) {
      const bare = word.replace(/[^A-Za-z0-9']/g, "");
      if (!bare) continue;
      const heavy = WEIGHT.test(bare) || (bare.length > 2 && bare === bare.toUpperCase()) || /\d/.test(bare);
      if (heavy) line.nods.push(t);
      const syl = Math.max(1, Math.round(bare.length / 2.8));
      const wdur = syl * (0.11 + Math.random() * 0.05 + (heavy ? 0.05 : 0));
      beats.push(...visemesFor(bare, t, wdur, heavy));
      t += wdur;
      t += /[,;:]$/.test(word) ? 0.16 : 0.04;                      // a breath at the commas
    }
    line.end = t;
    t += q || bang ? 0.34 : 0.28;                                   // the full stop
    lines.push(line);
  }
  return { beats, lines, total: t };
}

/** THE CORTEX: a net of neurons over the skull, behind and above the face. It breathes, signals
 *  run along the links as bright sparks and hop on, and the mouse is a hand in it: neurons near the
 *  cursor light up and lean toward it, and a click sends a wave across the whole net. Colors stay
 *  in the hue (never white-out): dim links, lit nodes, hot sparks. */
export function Cortex({ hue, mouse, level }: { hue: number; mouse: React.MutableRefObject<{ x: number; y: number }>; level: React.MutableRefObject<number> }) {
  const group = useRef<THREE.Group>(null!);
  const N = 170, R = 1.07, LINK = 0.40, MAXFIRE = 28;
  const data = useMemo(() => {
    const home: THREE.Vector3[] = [];
    while (home.length < N) {
      const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2;
      const r = Math.sqrt(1 - u * u);
      const v = new THREE.Vector3(r * Math.cos(th), u, r * Math.sin(th));
      if (v.z > 0.45 && v.y < 0.6 && v.y > -0.7) continue;      // not over the face
      if (v.y < -0.75) continue;                                 // not under the chin
      home.push(v.multiplyScalar(R));
    }
    const links: [number, number][] = [];
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) if (home[i].distanceTo(home[j]) < LINK) links.push([i, j]);
    const ppos = new Float32Array(N * 3), pcol = new Float32Array(N * 3), psize = new Float32Array(N);
    const lpos = new Float32Array(links.length * 6), lcol = new Float32Array(links.length * 6);
    const pg = new THREE.BufferGeometry(); pg.setAttribute("position", new THREE.BufferAttribute(ppos, 3)); pg.setAttribute("color", new THREE.BufferAttribute(pcol, 3));
    const lg = new THREE.BufferGeometry(); lg.setAttribute("position", new THREE.BufferAttribute(lpos, 3)); lg.setAttribute("color", new THREE.BufferAttribute(lcol, 3));
    const spos = new Float32Array(MAXFIRE * 3), scol = new Float32Array(MAXFIRE * 3);
    const sg = new THREE.BufferGeometry(); sg.setAttribute("position", new THREE.BufferAttribute(spos, 3)); sg.setAttribute("color", new THREE.BufferAttribute(scol, 3));
    const pos = home.map((v) => v.clone());
    const adj: number[][] = Array.from({ length: N }, () => []);
    links.forEach(([a, b], i) => { adj[a].push(i); adj[b].push(i); });
    return { home, pos, links, adj, pg, lg, sg, ppos, pcol, psize, lpos, lcol, spos, scol,
             heat: new Float32Array(N), fire: [] as { link: number; t: number; dir: number; hopped: boolean }[], wave: -1, waveFrom: new THREE.Vector3() };
  }, []);
  const glowTex = useMemo(() => {
    const c = document.createElement("canvas"); c.width = c.height = 32;
    const g = c.getContext("2d")!; const gr = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(0.35, "rgba(255,255,255,0.55)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 32, 32);
    return new THREE.CanvasTexture(c);
  }, []);
  const tmp = useMemo(() => new THREE.Vector3(), []);
  const tmp2 = useMemo(() => new THREE.Vector3(), []);
  const dim = useMemo(() => new THREE.Color(), []);
  const lit = useMemo(() => new THREE.Color(), []);
  const hot = useMemo(() => new THREE.Color(), []);
  const time = useRef(0);
  // a click anywhere: a wave from where the cursor is on the head
  useEffect(() => {
    const down = () => { data.wave = 0; };
    window.addEventListener("mousedown", down);
    return () => window.removeEventListener("mousedown", down);
  }, [data]);
  useFrame(({ camera }, dtRaw) => {
    const dt = Math.min(0.05, dtRaw);
    const d = data;
    time.current += dt;
    const h = ((hue % 360) + 360) % 360 / 360;
    dim.setHSL(h, 0.85, 0.30); lit.setHSL(h, 0.9, 0.62); hot.setHSL((h + 0.08) % 1, 1.0, 0.78);
    group.current.rotation.y = 0.18 * Math.sin(time.current * 0.11);           // a slow sway, never across the face
    group.current.updateMatrixWorld();
    const mx = mouse.current.x, my = mouse.current.y;
    let nearest = -1, nearestD = 9;
    for (let i = 0; i < N; i++) {
      tmp.copy(d.home[i]).applyMatrix4(group.current.matrixWorld);
      const facing = tmp.z > -0.15;                                             // toward the camera
      tmp2.copy(tmp).project(camera);
      const dist = Math.hypot(tmp2.x - mx, (tmp2.y - my) * 0.75);
      const near = facing ? Math.max(0, 1 - dist / 0.30) : 0;
      if (facing && dist < nearestD) { nearestD = dist; nearest = i; }
      d.heat[i] = Math.max(d.heat[i] * Math.pow(0.12, dt), near * near);
    }
    // a wave on its own every so often (the docked head has no hand in it)
    if (d.wave < 0 && Math.random() < dt * 0.12) { d.wave = 0; d.waveFrom.copy(d.home[Math.floor(Math.random() * N)]); }
    // the wave from a click
    if (d.wave >= 0) {
      if (d.wave === 0 && nearest >= 0 && mouse.current.x !== 0) d.waveFrom.copy(d.home[nearest]);
      d.wave += dt * 2.6;
      for (let i = 0; i < N; i++) {
        const ring = Math.abs(d.home[i].distanceTo(d.waveFrom) - d.wave * 1.2);
        if (ring < 0.16) d.heat[i] = Math.max(d.heat[i], 1 - ring / 0.16);
      }
      if (d.wave > 1.6) d.wave = -1;
    }
    // neurons lean toward the cursor and breathe; the mesh positions follow
    for (let i = 0; i < N; i++) {
      const hm = d.home[i], p = d.pos[i], ht = d.heat[i];
      const breathe = 1 + 0.012 * Math.sin(time.current * 1.3 + i * 0.7);
      tmp.copy(hm).multiplyScalar(breathe);
      if (ht > 0.02 && nearest >= 0) tmp.lerp(d.home[nearest], ht * 0.22);       // lean toward the hand
      p.lerp(tmp, 1 - Math.pow(0.02, dt));
      d.ppos[i * 3] = p.x; d.ppos[i * 3 + 1] = p.y; d.ppos[i * 3 + 2] = p.z;
    }
    // signals: born now and then (more with the voice, more under the hand), run, hop once
    const born = dt * (2.2 + 5 * level.current + (nearest >= 0 && d.heat[nearest] > 0.5 ? 6 : 0));
    if (Math.random() < born && d.links.length && d.fire.length < MAXFIRE) {
      const from = nearest >= 0 && d.heat[nearest] > 0.5 && d.adj[nearest].length ? d.adj[nearest][Math.floor(Math.random() * d.adj[nearest].length)] : Math.floor(Math.random() * d.links.length);
      d.fire.push({ link: from, t: 0, dir: Math.random() < 0.5 ? 1 : -1, hopped: false });
    }
    for (const f of d.fire) {
      f.t += dt * 1.9;
      if (!f.hopped && f.t > 0.8) {
        f.hopped = true;
        const [a, b] = d.links[f.link];
        const head = f.dir > 0 ? b : a;
        d.heat[head] = Math.max(d.heat[head], 0.85);
        if (Math.random() < 0.55 && d.fire.length < MAXFIRE) {
          const options = d.adj[head].filter((k) => k !== f.link);
          if (options.length) { const nx = options[Math.floor(Math.random() * options.length)]; d.fire.push({ link: nx, t: 0, dir: d.links[nx][0] === head ? 1 : -1, hopped: false }); }
        }
      }
    }
    d.fire = d.fire.filter((f) => f.t < 1);
    const litLink = new Float32Array(d.links.length);
    d.spos.fill(0); d.scol.fill(0);
    d.fire.forEach((f, k) => {
      litLink[f.link] = Math.max(litLink[f.link], 1 - Math.abs(f.t - 0.5) * 0.6);
      const [a, b] = d.links[f.link];
      const A = d.pos[f.dir > 0 ? a : b], B = d.pos[f.dir > 0 ? b : a];
      tmp.copy(A).lerp(B, Math.min(1, f.t)).multiplyScalar(1.01);
      d.spos[k * 3] = tmp.x; d.spos[k * 3 + 1] = tmp.y; d.spos[k * 3 + 2] = tmp.z;
      d.scol[k * 3] = hot.r; d.scol[k * 3 + 1] = hot.g; d.scol[k * 3 + 2] = hot.b;
    });
    for (let i = 0; i < N; i++) {
      const ht = Math.min(1, d.heat[i]);
      d.pcol[i * 3] = dim.r + (lit.r - dim.r) * ht + hot.r * ht * ht * 0.5;
      d.pcol[i * 3 + 1] = dim.g + (lit.g - dim.g) * ht + hot.g * ht * ht * 0.5;
      d.pcol[i * 3 + 2] = dim.b + (lit.b - dim.b) * ht + hot.b * ht * ht * 0.5;
    }
    d.links.forEach(([a, b], i) => {
      const A = d.pos[a], B = d.pos[b];
      d.lpos[i * 6] = A.x; d.lpos[i * 6 + 1] = A.y; d.lpos[i * 6 + 2] = A.z; d.lpos[i * 6 + 3] = B.x; d.lpos[i * 6 + 4] = B.y; d.lpos[i * 6 + 5] = B.z;
      const ht = Math.min(1, Math.max(litLink[i], (d.heat[a] + d.heat[b]) * 0.5));
      const k = 0.22 + 0.78 * ht;
      const r = dim.r * k + lit.r * ht * 0.7, g = dim.g * k + lit.g * ht * 0.7, bl = dim.b * k + lit.b * ht * 0.7;
      d.lcol[i * 6] = r; d.lcol[i * 6 + 1] = g; d.lcol[i * 6 + 2] = bl; d.lcol[i * 6 + 3] = r; d.lcol[i * 6 + 4] = g; d.lcol[i * 6 + 5] = bl;
    });
    (d.pg.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (d.pg.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;
    (d.lg.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (d.lg.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;
    (d.sg.getAttribute("position") as THREE.BufferAttribute).needsUpdate = true;
    (d.sg.getAttribute("color") as THREE.BufferAttribute).needsUpdate = true;
  });
  return (
    <group ref={group}>
      <lineSegments geometry={data.lg} raycast={() => undefined}>
        <lineBasicMaterial vertexColors transparent opacity={0.7} blending={THREE.AdditiveBlending} depthWrite={false} />
      </lineSegments>
      <points geometry={data.pg} raycast={() => undefined}>
        <pointsMaterial map={glowTex} vertexColors size={0.07} transparent opacity={0.9} depthWrite={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
      <points geometry={data.sg} raycast={() => undefined}>
        <pointsMaterial map={glowTex} vertexColors size={0.11} transparent opacity={1} depthWrite={false} depthTest={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
    </group>
  );
}

export type OrganismMode = "full" | "mini" | "dj" | "stage";

/** THE ORBITS (Brian, 2026-09-22: "get rid of the outline wrapping his face"): the docked head's
 *  company is not a lattice but three thin tilted rings with motes riding them like electrons - the
 *  head is a nucleus at work. The rings precess slowly; the motes run faster the busier HELIX is
 *  (the same level the lattice used to fire on), and a burst of them scatters on a big pulse.
 *  One line loop per ring, one points cloud; nothing per node, nothing blurred. */
function Orbits({ hue, level }: { hue: number; level: React.MutableRefObject<number> }) {
  const RINGS = 3, PER = 5, R = 1.42;
  const rings = useRef<THREE.Group[]>([]);
  const data = useMemo(() => {
    const tilts = Array.from({ length: RINGS }, (_, i) => new THREE.Euler(0.9 + i * 0.55, i * 1.9, 0.35 - i * 0.5));
    const seg = 96, ring = new Float32Array((seg + 1) * 3);
    for (let i = 0; i <= seg; i++) { const a = (i / seg) * Math.PI * 2; ring[i * 3] = Math.cos(a) * R; ring[i * 3 + 1] = 0; ring[i * 3 + 2] = Math.sin(a) * R; }
    const rg = new THREE.BufferGeometry(); rg.setAttribute("position", new THREE.BufferAttribute(ring, 3));
    const N = RINGS * PER;
    const pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
    const pg = new THREE.BufferGeometry(); pg.setAttribute("position", new THREE.BufferAttribute(pos, 3)); pg.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const phase = Array.from({ length: N }, (_, i) => (i % PER) / PER * Math.PI * 2 + Math.random() * 0.4);
    const speed = Array.from({ length: RINGS }, (_, i) => 0.55 + i * 0.22);
    return { tilts, rg, pg, pos, col, phase, speed, t: 0, tmp: new THREE.Vector3(), q: new THREE.Quaternion(), c: new THREE.Color(), c2: new THREE.Color() };
  }, []);
  const glowTex = useMemo(() => {
    const c = document.createElement("canvas"); c.width = c.height = 32;
    const g = c.getContext("2d")!; const gr = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(0.3, "rgba(255,255,255,0.6)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 32, 32);
    return new THREE.CanvasTexture(c);
  }, []);
  useFrame((_, dtRaw) => {
    const dt = Math.min(0.05, dtRaw);
    const d = data;
    const busy = Math.max(0, Math.min(1, level.current));
    d.t += dt * (1 + busy * 2.2);
    const h = ((hue % 360) + 360) % 360 / 360;
    d.c.setHSL(h, 0.9, 0.7); d.c2.setHSL((h + 0.1) % 1, 1, 0.85);
    for (let r = 0; r < RINGS; r++) {
      const g = rings.current[r]; if (!g) continue;
      g.rotation.set(d.tilts[r].x + 0.12 * Math.sin(d.t * 0.21 + r), d.tilts[r].y + d.t * 0.07 * (r % 2 ? -1 : 1), d.tilts[r].z);
      g.updateMatrixWorld();
      for (let k = 0; k < PER; k++) {
        const i = r * PER + k;
        const a = d.phase[i] + d.t * d.speed[r] * (r % 2 ? -1 : 1);
        d.tmp.set(Math.cos(a) * R, 0, Math.sin(a) * R).applyMatrix4(g.matrixWorld);
        d.pos[i * 3] = d.tmp.x; d.pos[i * 3 + 1] = d.tmp.y; d.pos[i * 3 + 2] = d.tmp.z;
        // the motes in front of the head burn brighter (they are nearer the light)
        const front = (d.tmp.z / R + 1) / 2;
        const mix = 0.35 + 0.65 * front + busy * 0.3;
        d.col[i * 3] = d.c.r * (1 - front) + d.c2.r * front * mix; d.col[i * 3 + 1] = d.c.g * (1 - front) + d.c2.g * front * mix; d.col[i * 3 + 2] = d.c.b * (1 - front) + d.c2.b * front * mix;
      }
    }
    (d.pg.attributes.position as THREE.BufferAttribute).needsUpdate = true;
    (d.pg.attributes.color as THREE.BufferAttribute).needsUpdate = true;
  });
  const ringColor = useMemo(() => new THREE.Color().setHSL(((hue % 360) + 360) % 360 / 360, 0.8, 0.6), [hue]);
  return (
    <group>
      {Array.from({ length: RINGS }, (_, i) => (
        <group key={i} ref={(el) => { if (el) rings.current[i] = el; }}>
          <lineLoop geometry={data.rg} raycast={() => undefined}><lineBasicMaterial color={ringColor} transparent opacity={0.22 + i * 0.03} depthWrite={false} blending={THREE.AdditiveBlending} /></lineLoop>
        </group>
      ))}
      <points geometry={data.pg} raycast={() => undefined}>
        <pointsMaterial map={glowTex} vertexColors size={0.15} transparent opacity={0.95} depthWrite={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
    </group>
  );
}

function Body({ look, onPulse, mini = false, mode = mini ? "mini" : "full", roam, box }: { look: OrganismLook; onPulse: (big: boolean) => void; mini?: boolean; mode?: OrganismMode; roam?: React.MutableRefObject<{ x: number; y: number; r: number } | null>; box?: React.RefObject<HTMLElement | null> }) {
  const mesh = useRef<THREE.Mesh>(null!);
  // THE DJ (the radio deck) and THE STAGE (the face leaping to the front of the Console): their
  // own acts ride on top of the face's life - a head-bang on the kick, a shrug, a grin, a wink,
  // the zoom-off before a music video; on the stage the head roams and the lattice follows it.
  const dj = useRef({ bang: 0, groove: 0, shrug: { t: -1 }, mood: { act: "", t: 0, dur: 0, next: 4 }, wink: { t: -1, side: 1 }, off: { t: -1, dir: 1 }, peek: { t: -1 }, ease: 0 });
  const roamT = useRef(0);
  const spores = useRef<THREE.Points>(null!);
  const gazeRef = useRef({ x: 0, y: 0 });
  const stateColor = useMemo(() => new THREE.Color(), []);
  const blink = useRef({ next: 3, t: -1 });
  const pulse = useRef({ t: -1, speed: 1 });
  const flash = useRef(0);
  const swell = useRef(0);
  const prev = useRef({ orb: "idle", hue: "none", bubbles: 0 });
  const mouth = useRef({ open: 0, flutter: 0, wide: 1, teeth: 0, tongue: 0, snap: false });
  const mouse = useRef({ x: 0, y: 0 });
  const levelRef = useRef(0);
  const errand = useRef({ k: 0, target: 0, spin: 0 });
  // THE WHEEL (the docked face): every few seconds an act - a spin, a bounce, a think, a peek -
  // so the little head in the corner is alive while you work elsewhere
  const wheel = useRef({ next: 3, act: "", t: 0, dur: 0 });
  const rig = useRef<THREE.Group>(null!);
  const perf = useRef<{ script: Script | null; t0: number; beat: number; nod: number; glanceT: number; endSmile: number }>({ script: null, t0: 0, beat: 0, nod: -1, glanceT: 0, endSmile: 0 });
  const expr = useRef({ brow: 0, smile: 0, tilt: 0, squint: 0, nod: 0, glance: 0 });
  // THE IDLE LIFE: every few seconds a small act - a glance aside, a brow flick, a double blink,
  // a head tilt, a half smile - so the face is alive between lines. Thinking gets its own acts:
  // eyes up and away, a furrow, a squint, as if reading something over your shoulder.
  const local = useRef<{ until: number; audio: HTMLAudioElement | null; ctx: AudioContext | null; analyser: AnalyserNode | null; buf: Uint8Array | null }>({ until: 0, audio: null, ctx: null, analyser: null, buf: null });   // a Say line performed here
  useEffect(() => {
    const onSay = (e: Event) => {
      const d = (e as CustomEvent).detail as { text?: string; words?: { t: number; d: number; w: string }[]; audio?: HTMLAudioElement } | undefined;
      const text = String(d?.text || "");
      if (!text) return;
      if (d?.audio) {
        // the voice's own timing: the script runs on the audio's clock, the level on its signal
        const audio = d.audio;
        const script = d.words && d.words.length ? buildTimedScript(d.words) : buildScript(text);
        const start = () => {
          perf.current = { script, t0: 0, beat: 0, nod: -1, glanceT: 0, endSmile: 0 };
          local.current.audio = audio;
          local.current.until = Infinity;
          prev.current.orb = "speaking";
          firePulseRef.current(true);
          try {
            if (!local.current.ctx) local.current.ctx = new AudioContext();
            const ctx = local.current.ctx;
            const src = ctx.createMediaElementSource(audio);
            const an = ctx.createAnalyser(); an.fftSize = 512; an.smoothingTimeConstant = 0.5;
            src.connect(an); an.connect(ctx.destination);
            local.current.analyser = an; local.current.buf = new Uint8Array(an.frequencyBinCount);
            void ctx.resume();
          } catch { local.current.analyser = null; }
        };
        const stop = () => { local.current.until = 0; local.current.audio = null; local.current.analyser = null; };
        audio.addEventListener("playing", start, { once: true });
        audio.addEventListener("ended", stop, { once: true });
        audio.addEventListener("error", stop, { once: true });
        audio.addEventListener("pause", stop, { once: true });
        return;
      }
      const script = buildScript(text);
      perf.current = { script, t0: performance.now() / 1000, beat: 0, nod: -1, glanceT: 0, endSmile: 0 };
      local.current.until = performance.now() / 1000 + script.total + 0.4;
      prev.current.orb = "speaking";           // the orb event that follows must not restart the script
      firePulseRef.current(true);
    };
    window.addEventListener("helix-say", onSay);
    return () => window.removeEventListener("helix-say", onSay);
  }, []);
  useEffect(() => {
    if (mode !== "dj") return;
    const onCue = (e: Event) => {
      const cue = String((e as CustomEvent).detail?.cue || "");
      const d = dj.current;
      if (cue === "video-in") { d.wink = { t: 0, side: Math.random() < 0.5 ? -1 : 1 }; d.off = { t: 0, dir: Math.random() < 0.5 ? -1 : 1 }; }
      if (cue === "resume") { d.peek = { t: 0 }; }
      if (cue === "wink") { d.wink = { t: 0, side: 1 }; }
      if (cue === "back") { d.off = { t: -1, dir: 1 }; d.peek = { t: -1 }; }
    };
    window.addEventListener("helix-dj", onCue);
    return () => window.removeEventListener("helix-dj", onCue);
  }, [mode]);
  const firePulseRef = useRef<(big: boolean) => void>(() => undefined);
  const life = useRef({ next: 2.5, act: "", until: 0, gx: 0, gy: 0, brow: 0, tilt: 0, smile: 0, squint: 0, blinks: 0 });
  // the mouse anywhere on the page turns the head - not only over the canvas
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const el = box?.current;
      if (el) {
        const r = el.getBoundingClientRect();
        mouse.current.x = Math.max(-1.6, Math.min(1.6, ((e.clientX - r.left) / Math.max(1, r.width)) * 2 - 1));
        mouse.current.y = Math.max(-1.6, Math.min(1.6, -(((e.clientY - r.top) / Math.max(1, r.height)) * 2 - 1)));
        return;
      }
      mouse.current.x = (e.clientX / window.innerWidth) * 2 - 1; mouse.current.y = -((e.clientY / window.innerHeight) * 2 - 1);
    };
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, [box]);
  const bits = useBitsTexture(look.hue);
  useEffect(() => () => (bits as THREE.CanvasTexture & { _stop?: () => void })._stop?.(), [bits]);
  const phase = look.phase === "cell" ? "core" : look.phase;

  const uniforms = useMemo(() => ({
    uTime: { value: 0 }, uSwell: { value: 0 }, uPulse: { value: -1 }, uFlash: { value: 0 },
    uBits: { value: bits },
    uColor: { value: new THREE.Color(0.16, 0.55, 1.0) },
    uHueA: { value: hueToRgb(look.hue, 0.85, 0.55) }, uHueB: { value: hueToRgb(look.hue + 40, 0.9, 0.62) },
    uPhaseFace: { value: phase === "face" ? 1 : 0 }, uPhaseStorm: { value: phase === "storm" ? 1 : 0 },
    uAttend: { value: 0 }, uLevel: { value: 0 }, uOpen: { value: 0 }, uWide: { value: 1 }, uTeeth: { value: 0 }, uTongue: { value: 0 }, uBlink: { value: 0 }, uBrow: { value: 0 },
    uGaze: { value: new THREE.Vector2() }, uEnergy: { value: look.energy },
    uFace: { value: new THREE.Vector2() }, uTilt: { value: 0 }, uSmile: { value: 0 }, uSquint: { value: 0 }, uWink: { value: 0 },
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
  firePulseRef.current = firePulse;

  useFrame(({ camera }, dtRaw) => {
    const pointer = mouse.current;
    const dt = Math.min(0.05, dtRaw);
    const s = useHelix.getState();
    const L = baseLook(s);
    // R3F hands the material a COPY of the uniforms object: vectors and colors stay shared (they are
    // the same instances) but every scalar is copied by value, so writing uniforms.uOpen.value here
    // never reached the shader - the lips, blinks, brows and pulses were all dead. Write to the
    // material's own uniforms, always.
    const u = (mesh.current.material as THREE.ShaderMaterial).uniforms as typeof uniforms;
    u.uTime.value += dt;
    u.uBits.value = bits;
    u.uEnergy.value += (look.energy - u.uEnergy.value) * 0.05;
    u.uHueA.value.lerp(hueToRgb(look.hue, 0.85, 0.55), 0.05);
    u.uHueB.value.lerp(hueToRgb(look.hue + 40, 0.9, 0.62), 0.05);
    stateColor.setRGB(L.color[0], L.color[1], L.color[2]);
    u.uColor.value.lerp(stateColor, 0.06);
    u.uAttend.value += ((s.orb === "listening" ? 1 : 0) - u.uAttend.value) * 0.08;
    const localSay = performance.now() / 1000 < local.current.until;
    const speaking = s.orb === "speaking" || localSay;
    let level = speaking ? 0.3 + s.level * 0.9 : s.orb === "transcribing" ? s.level * 0.6 : 0;
    const an = local.current.analyser, buf = local.current.buf;
    if (localSay && an && buf) {
      // the measured voice: low-mid energy of the playing line, 0..1
      an.getByteFrequencyData(buf as Uint8Array<ArrayBuffer>);
      let sum = 0; const n = Math.min(48, buf.length);
      for (let i = 2; i < n; i++) sum += buf[i];
      level = Math.min(1, (sum / (n - 2)) / 110);
    }
    u.uLevel.value += (level - u.uLevel.value) * 0.3;
    levelRef.current = u.uLevel.value;
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
      if (p.orb === "speaking" && !localSay) { perf.current.script = null; perf.current.endSmile = 1; }
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
    let brow = 0.05, smile = s.orb === "idle" ? 0.30 : 0.1, tilt = 0, squint = 0;
    // the idle / thinking acts
    const lf = life.current;
    const wh = wheel.current;
    // WORKING (Brian, 2026-09-22): the little head in the corner should look like it is working,
    // not just looking at the mouse. When HELIX is busy - a reply in flight, or anything running in
    // CURRENT TASKS - the wheel leans on think/scan acts, they come faster, and a small pulse of
    // light runs across the head every couple of seconds.
    const jobsLive = useJobs.getState().order.some((i) => { const j = useJobs.getState().jobs[i]; return j && j.state === "running" && !j.quiet; });
    const working = s.busy || s.orb === "thinking" || jobsLive;
    if (mini) {
      wh.next -= dt * (working ? 2.2 : 1);
      if (wh.next <= 0 && !wh.act) {
        const acts = working
          ? ["think", "scan", "think", "hmm", "nod", "scan", "think", "spin"]
          : ["spin", "bounce", "think", "peek", "nod", "spin", "bounce", "scan"];
        wh.act = acts[Math.floor(Math.random() * acts.length)]; wh.t = 0;
        wh.dur = wh.act === "think" ? 2.6 : wh.act === "spin" ? 1.3 : wh.act === "bounce" ? 1.1 : wh.act === "scan" ? 1.8 : wh.act === "hmm" ? 1.5 : 1.6;
        wh.next = (working ? 1.2 : 4) + Math.random() * (working ? 2 : 6);
      }
      if (wh.act) { wh.t += dt; if (wh.t >= wh.dur) wh.act = ""; }
      if (working && Math.sin(u.uTime.value * 2.6) > 0.985) flash.current = Math.max(flash.current, 0.35);
    }
    const thinking = s.orb === "thinking" || (mini && (wh.act === "think" || wh.act === "hmm"));
    const now = u.uTime.value;
    if (!speaking) {
      lf.next -= dt;
      if (lf.next <= 0) {
        const acts = thinking
          ? ["lookup", "lookup", "furrow", "squint", "lookaside", "tilt"]
          : ["glance", "glance", "flick", "doubleblink", "tilt", "halfsmile", "settle"];
        lf.act = acts[Math.floor(Math.random() * acts.length)];
        lf.until = now + (thinking ? 1.2 + Math.random() * 1.6 : 0.6 + Math.random() * 1.2);
        lf.next = thinking ? 1.2 + Math.random() * 1.6 : (mini ? 1.4 : 2.2) + Math.random() * (mini ? 2.5 : 4);
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
      if (mini && wh.act === "scan") { const k = wh.t / wh.dur; e.glance = Math.sin(k * Math.PI * 6) * 0.9; squint += 0.25; brow += 0.15; }   // eyes sweep left-right: reading
      if (mini && wh.act === "hmm") { target = Math.max(target, 0.18 + 0.1 * Math.sin(wh.t * 7)); tilt += 0.08; }
    }
    const lifeGy = !speaking && now < lf.until ? lf.gy : 0;
    const sc = pf.script;
    if (speaking && sc) {
      const tt = local.current.audio ? local.current.audio.currentTime : performance.now() / 1000 - pf.t0;   // the audio's clock when we have it; wall clock otherwise
      while (pf.beat < sc.beats.length && sc.beats[pf.beat].at + sc.beats[pf.beat].dur < tt) pf.beat++;
      const b = sc.beats[pf.beat];
      if (b && tt >= b.at) {
        // South Park: snap to the shape and hold it for the sound; a short ease at the edges
        const k = (tt - b.at) / b.dur;
        const edge = Math.min(1, k / 0.18, (1 - k) / 0.18);
        target = b.open * (0.55 + 0.45 * edge) * (an ? 0.45 + 0.7 * u.uLevel.value : 0.75 + 0.25 * Math.min(1, u.uLevel.value + 0.5));
        m.wide = b.wide; m.teeth = b.teeth; m.tongue = b.tongue; m.snap = true;
      } else if (tt > sc.total) { m.flutter += dt * 11; target = 0.12 + 0.35 * Math.max(0, Math.sin(m.flutter)); m.wide = 1; m.teeth = 0; m.tongue = 0; m.snap = false; }   // the script ran out, the voice has not
      else { m.wide = 1; m.teeth = 0; m.tongue = 0; m.snap = false; }
      const line = sc.lines.find((l) => tt >= l.at - 0.1 && tt < l.end + 0.3) || sc.lines[sc.lines.length - 1];
      if (line) {
        brow = line.brow; smile = line.smile; tilt = line.tilt; squint = line.squint || 0;
        if (line.glance && tt < line.at + 0.7) e.glance += (line.glance * 0.6 - e.glance) * 0.12;
        const nodAt = line.nods.find((n) => tt >= n && tt < n + 0.35);
        if (nodAt !== undefined && pf.nod !== nodAt) { pf.nod = nodAt; e.nod = 1; squint = 0.5; }
      }
    } else if (speaking) {
      m.flutter += dt * (9 + s.level * 14);
      target = Math.min(1, 0.15 + u.uLevel.value * 0.9 + 0.25 * Math.max(0, Math.sin(m.flutter)) * u.uLevel.value);
      brow = 0.2;
    }
    // THE DJ'S ACTS
    const D = dj.current;
    let djTilt = 0, djBang = 0, djShrug = 0, djSway = 0, djBob = 0, djZ = 0, djX = 0, djScale = 1, djSpin = 0;
    if (mode === "dj") {
      const beat = radioBeat();
      D.ease += ((beat.playing ? 1 : 0) - D.ease) * (1 - Math.pow(0.05, dt));
      const bpm = 96;
      D.groove += dt * (bpm / 60) * Math.PI * (0.5 + 0.5 * D.ease);
      if (beat.kick) D.bang = 1;
      D.bang *= Math.pow(0.02, dt);
      djBang = D.bang * (0.25 + 0.35 * beat.level);                              // the head-bang: a sharp dip that recovers
      djSway = Math.sin(D.groove * 0.5) * 0.18 * D.ease;                          // the groove: side to side on the half beat
      djBob = Math.abs(Math.sin(D.groove)) * 0.06 * D.ease;
      djTilt = Math.sin(D.groove * 0.25) * 0.12 * D.ease;
      // the shrug: every 6-10 s while it plays, shoulders (the whole head) hop up and tip
      if (D.shrug.t < 0 && D.ease > 0.5 && Math.random() < dt * 0.14) D.shrug.t = 0;
      if (D.shrug.t >= 0) { D.shrug.t += dt; const k = Math.min(1, D.shrug.t / 0.7); djShrug = Math.sin(k * Math.PI) ; if (D.shrug.t > 0.7) D.shrug.t = -1; }
      // the happy faces: grin, eyes-shut groove, brows-up delight, a tongue-out cheeky one, a wink
      const M = D.mood;
      M.next -= dt;
      if (!M.act && M.next <= 0 && D.ease > 0.4) { const acts = ["grin", "shut", "delight", "cheeky", "wink", "grin", "delight"]; M.act = acts[Math.floor(Math.random() * acts.length)]; M.t = 0; M.dur = M.act === "wink" ? 0.7 : 1.6 + Math.random() * 1.6; M.next = 3 + Math.random() * 4; if (M.act === "wink") D.wink = { t: 0, side: Math.random() < 0.5 ? -1 : 1 }; }
      if (M.act) { M.t += dt; const k = Math.sin(Math.min(1, M.t / M.dur) * Math.PI);
        if (M.act === "grin") { smile = Math.max(smile, 0.95 * k); brow = Math.max(brow, 0.35 * k); }
        if (M.act === "shut") { squint = Math.max(squint, 1.6 * k); smile = Math.max(smile, 0.7 * k); tilt += 0.1 * k; }
        if (M.act === "delight") { brow = Math.max(brow, 0.9 * k); smile = Math.max(smile, 0.8 * k); target = Math.max(target, 0.35 * k); }
        if (M.act === "cheeky") { smile = Math.max(smile, 0.6 * k); target = Math.max(target, 0.3 * k); u.uTongue.value = Math.max(u.uTongue.value, 0.9 * k); squint = Math.max(squint, 0.3 * k); }
        if (M.t >= M.dur) M.act = ""; }
      if (D.ease > 0.5) smile = Math.max(smile, 0.35);
      // the wink (also the first beat of the video intro)
      if (D.wink.t >= 0) { D.wink.t += dt; const k = Math.sin(Math.min(1, D.wink.t / 0.55) * Math.PI); u.uWink.value = D.wink.side * k; if (D.wink.t > 0.55) { D.wink.t = -1; u.uWink.value = 0; } } else u.uWink.value += (0 - u.uWink.value) * 0.3;
      // THE ZOOM-OFF: after the wink the head rushes toward you and out of frame, spinning
      if (D.off.t >= 0) { D.off.t += dt; const k = Math.max(0, Math.min(1, (D.off.t - 0.35) / 0.75)); const ek = k * k; djZ = ek * 3.2; djX = D.off.dir * ek * ek * 5; djScale = 1 + ek * 0.6; djSpin = ek * 2.2 * D.off.dir; if (D.off.t > 3) { /* stays out until "back" */ djZ = 3.2; djX = D.off.dir * 5; } }
      // THE PEEK (resume): the head slides in from the side, snaps a nod, and slides out
      if (D.peek.t >= 0) { D.peek.t += dt; const k = Math.min(1, D.peek.t / 1.6); const inout = Math.sin(k * Math.PI); djX = -3.6 + 3.2 * inout; djZ = 0.6 * inout; djBang = Math.max(djBang, k > 0.4 && k < 0.6 ? 0.5 : 0); smile = Math.max(smile, 0.9 * inout); brow = Math.max(brow, 0.6 * inout); if (D.peek.t > 1.6) D.peek.t = -1; }
    } else if (u.uWink.value !== 0) u.uWink.value += (0 - u.uWink.value) * 0.3;
    if (!speaking && pf.script) { pf.script = null; pf.endSmile = 1; }
    if (!speaking && pf.endSmile > 0) { smile = 0.45 * pf.endSmile; pf.endSmile = Math.max(0, pf.endSmile - dt * 0.5); }
    m.open += (target - m.open) * (speaking ? (m.snap ? 0.75 : 0.5) : 0.15);
    u.uOpen.value = m.open;
    u.uWide.value += ((speaking ? m.wide : 1) - u.uWide.value) * 0.55;
    u.uTeeth.value += ((speaking ? m.teeth : 0) - u.uTeeth.value) * 0.5;
    u.uTongue.value += ((speaking ? m.tongue : 0) - u.uTongue.value) * 0.5;
    e.brow += (brow - e.brow) * 0.12; e.smile += (smile - e.smile) * 0.08; e.tilt += (tilt - e.tilt) * 0.06;
    e.squint += (squint - e.squint) * 0.1; e.nod *= Math.pow(0.03, dt); if (speaking && !sc) e.glance *= Math.pow(0.05, dt);
    u.uBrow.value = e.brow + flash.current * 0.8;
    u.uSmile.value = e.smile; u.uSquint.value = e.squint;
    u.uTilt.value = e.tilt + pointer.x * 0.06 + djTilt;

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

    // THE ERRAND: while thinking the face recedes and turns away (off to fetch), and when it has
    // something to say it comes back through the lattice with a pulse. `errand` eases 0..1.
    const er = errand.current;
    const wantAway = thinking ? 1 : 0;
    if (wantAway !== er.target) { er.target = wantAway; if (!wantAway) { firePulse(true); er.spin = 1; } }
    er.k += (er.target - er.k) * (1 - Math.pow(0.02, dt));
    er.spin *= Math.pow(0.05, dt);
    const away = er.k;
    mesh.current.rotation.y += dt * (0.1 * look.energy + away * 1.6 + er.spin * 3.5);
    mesh.current.position.y = 0.012 * Math.sin(u.uTime.value * 1.1) + 0.006 * Math.sin(u.uTime.value * 0.37);   // it breathes
    mesh.current.rotation.x += ((-pointer.y * 0.12 + e.nod * 0.05) - mesh.current.rotation.x) * 0.04;
    mesh.current.scale.setScalar(1 + swell.current * 0.06);
    // the whole head (face, cortex, spores) goes on the errand together
    if (mini) {
      // the docked head: it idles with a bob, and the wheel's acts play on top
      const k = wh.act ? Math.min(1, wh.t / wh.dur) : 0;
      const spinA = wh.act === "spin" ? (1 - Math.cos(k * Math.PI)) * Math.PI : 0;                 // one full turn, eased
      const bounce = wh.act === "bounce" ? Math.abs(Math.sin(k * Math.PI * 3)) * 0.35 * (1 - k * 0.5) : 0;
      const peek = wh.act === "peek" ? Math.sin(k * Math.PI) * 0.55 : 0;
      const nod = wh.act === "nod" ? Math.sin(k * Math.PI * 4) * 0.18 : wh.act === "scan" ? Math.sin(k * Math.PI * 6) * 0.05 : 0;
      const lean = wh.act === "think" || wh.act === "hmm" ? Math.sin(k * Math.PI) * 0.12 : 0;
      rig.current.position.set(peek, 0.08 * Math.sin(u.uTime.value * 1.6) + bounce + lean * 0.5, 0);
      rig.current.rotation.set(nod - lean, spinA + peek * 0.6 + (wh.act === "scan" ? Math.sin(k * Math.PI * 6) * 0.25 : 0), lean * 0.5);
      rig.current.scale.setScalar(1 + bounce * 0.15);
    } else if (mode === "dj") {
      rig.current.position.set(djSway * 1.2 + djX, djBob + djShrug * 0.22 - djBang * 0.12, djZ);
      rig.current.rotation.set(djBang * 0.9 - djShrug * 0.1, djSway * 0.5 + djSpin, djSway * 0.35 + djShrug * 0.12 + djTilt * 0.4);
      rig.current.scale.set(djScale, djScale * (1 + djShrug * 0.06), djScale);
    } else if (mode === "stage") {
      // THE STAGE: the head roams the window on a slow figure-eight, leaning into its turns; the
      // lattice's hole follows it (roam), the console sits pushed back behind. Trippy on purpose.
      roamT.current += dt;
      const rt = roamT.current;
      // THE LEAP: it starts small at the bottom-right, where the docked head was, and runs to the
      // front over the first 0.8 s (a CSS transform on the canvas' parent would mis-size the
      // canvas - R3F measures the transformed box - so the run is done here, in the scene)
      const leap = 1 - Math.pow(1 - Math.min(1, rt / 0.8), 3);
      // in shot, always: a modest roam in the upper two thirds, the head at Talk's distance (it
      // used to come so close on a wide screen that it ran off the edges and hid the buttons)
      const rx = 0.6 * Math.sin(rt * 0.21) + 0.18 * Math.sin(rt * 0.53), ry = 0.22 + 0.22 * Math.sin(rt * 0.33 + 1) + 0.08 * Math.sin(rt * 0.77);
      const wx = rx + away * 1.4, wy = ry + away * 0.9, wz = 0.1 + 0.2 * Math.sin(rt * 0.17) - away * 3.2;
      rig.current.position.set(2.6 + (wx - 2.6) * leap, -1.6 + (wy + 1.6) * leap, -2.5 + (wz + 2.5) * leap);
      rig.current.rotation.set(-away * 0.3 - ry * 0.1, away * 0.8 + rx * 0.12 + (1 - leap) * 2.5, -rx * 0.06);
      rig.current.scale.setScalar((1.0 - away * 0.35) * (0.5 + 0.5 * leap));
      if (roam) {
        // project the head's centre to window fractions for the 2D lattice
        const v = new THREE.Vector3(rig.current.position.x, rig.current.position.y, rig.current.position.z).project(camera);
        roam.current = { x: (v.x + 1) / 2, y: (1 - v.y) / 2, r: 0.30 * (1.0 - away * 0.35) * (4.2 / (4.2 - rig.current.position.z)) };
      }
    } else {
      rig.current.position.set(away * 1.4, away * 0.9, -away * 3.2);
      rig.current.rotation.set(-away * 0.3, away * 0.8, 0);
      rig.current.scale.setScalar(1 - away * 0.35);
    }
    u.uFlash.value = Math.max(u.uFlash.value, er.spin * 0.6);
    spores.current.rotation.y -= dt * 0.05; spores.current.rotation.z += dt * 0.02;
    const sm = spores.current.material as THREE.PointsMaterial;
    sm.color.copy(u.uHueA.value).lerp(stateColor, 0.4);
    sm.size = 0.03 + 0.03 * u.uLevel.value + 0.05 * flash.current;
    camera.lookAt(0, 0, 0);
  });

  const tap = () => void api.post("/api/shell/tap").catch(() => undefined);

  return (
    <group ref={rig}>
      <mesh ref={mesh} onClick={tap}>
        <sphereGeometry args={[1.02, 96, 96]} />
        <shaderMaterial vertexShader={VERT} fragmentShader={FRAG} uniforms={uniforms} />
      </mesh>
      <points ref={spores} geometry={sporeGeo} raycast={() => undefined}>
        <pointsMaterial map={glow} size={0.03} transparent opacity={0.7} depthWrite={false} blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
      {/* the docked head and the head up front keep the orbits; the Talk page keeps its cortex (it fires with thought) */}
      {/* the orbits everywhere (Brian, 2026-09-21: the cortex's nets never got the look and cost the GPU); the cortex stays in the file, one word away */}
      <Orbits hue={look.hue} level={levelRef} />
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
    const dpr = Math.min(1.25, window.devicePixelRatio || 1);
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
      else { ctx.clearRect(0, 0, w, h); canvas.style.opacity = "0"; window.setTimeout(() => { if (alive) canvas.style.display = "none"; }, 700); }   // a blended full-window layer costs compositing even when empty
    };
    canvas.style.opacity = "1"; canvas.style.display = "block";
    raf = requestAnimationFrame(step);
    return () => { alive = false; cancelAnimationFrame(raf); };
  }, [trigger, hue]);
  return <canvas ref={ref} className="fixed inset-0" style={{ zIndex: 1, pointerEvents: "none", transition: "opacity 0.6s", mixBlendMode: "screen" }} />;
}

/** The room behind the face: the Console's helix and neural net, dimmed, so the avatar sits in
 *  the same world as the rest of HELIX (and the net still breaks under the mouse here). */
function useHelixColors() {
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
  return colors;
}

/** The room's 2D half: the neural net and the vignette. The helix rides inside the face's canvas. */
function Room({ follow, z = 0, breach = false }: { follow?: React.MutableRefObject<{ x: number; y: number; r: number } | null>; z?: number; breach?: boolean }) {
  return (
    <div className="board-stage" style={{ position: "fixed", zIndex: z, opacity: 0.9, pointerEvents: "none" }} aria-hidden="true">
      {/* the stage (breach): a sparse sheet the head breaks through and that heals behind it - not the crowded 3D shell of the Talk page */}
      {/* Talk: a light net, 2D, only when the machine can afford Full (the 3D shell of 2.6x nodes is gone with the cortex) */}
      {(breach || perf.level === "full") && <NeuralLayer density={breach ? 1.15 : 0.9} keepOut={{ x: 0.5, y: 0.5, r: 0.36 }} follow={follow} depth={false} breach={breach} />}
    </div>
  );
}

export default function Organism({ look, mini = false, onClick, mode: modeIn, onClose }: { look: OrganismLook; mini?: boolean; onClick?: () => void; mode?: OrganismMode; onClose?: () => void }) {
  const [curtain, setCurtain] = useState(0);
  const [epoch, setEpoch] = useState(0);     // a lost context that never comes back: remount the canvas
  const colors = useHelixColors();
  const mode: OrganismMode = modeIn || (mini ? "mini" : "full");
  const level = usePerfLevel();
  const cap = dprCap();
  const roam = useRef<{ x: number; y: number; r: number } | null>(null);
  const box = useRef<HTMLDivElement | null>(null);
  if (mode === "mini") {
    return (
      <div className="face-dock" onClick={onClick} data-tip="Click me: I come to the front" role="button">
        {/* the camera sits back so a bounce, a spin or a peek never leaves the canvas (it clipped) */}
        <Canvas key={epoch} frameloop="demand" camera={{ position: [0, 0.1, 4.6], fov: 40 }} gl={{ antialias: true, alpha: true, preserveDrawingBuffer: true }} dpr={[1, Math.min(cap, 1.25)]} style={{ pointerEvents: "none" }}
          onCreated={({ gl }) => { gl.domElement.addEventListener("webglcontextlost", () => window.setTimeout(() => setEpoch((e) => e + 1), 2500)); }}>
          <ContextGuard />
          <FrameThrottle />
          <Body look={look} onPulse={() => undefined} mini />
        </Canvas>
      </div>
    );
  }
  if (mode === "dj") {
    // THE DJ: the same face, inside the radio deck's stage; the mouse and clicks work on the head
    // relative to the stage; the beat drives the acts. One extra WebGL context, released with the deck.
    return (
      <div ref={box} className="dj-face" aria-hidden="true">
        <Canvas key={epoch} frameloop="demand" camera={{ position: [0, 0.1, 3.6], fov: 40 }} gl={{ antialias: true, alpha: true }} dpr={[1, cap]} style={{ pointerEvents: "none" }}
          onCreated={({ gl }) => { gl.domElement.addEventListener("webglcontextlost", () => window.setTimeout(() => setEpoch((e) => e + 1), 2500)); }}>
          <ContextGuard />
          <FrameThrottle />
          <group position={[0, -0.6, -6]} scale={0.55}><HelixBackdrop colors={colors} position={[0, 0, 0]} scale={1} tempo={1.3} spinRate={1.6} bendable={false} /></group>
          <Body look={look} onPulse={() => undefined} mode="dj" box={box} />
        </Canvas>
      </div>
    );
  }
  const stage = mode === "stage";
  return (
    <>
      <Room follow={stage ? roam : undefined} z={stage ? 31 : 0} breach={stage} />
      {stage && <div className="stage-veil" onClick={onClose} />}
      <div className={`fixed inset-0${stage ? "" : " face-full"}`} style={{ zIndex: stage ? 32 : 0, pointerEvents: stage ? "none" : undefined }}>
        <Canvas key={epoch} frameloop="demand" camera={{ position: [0, 0.2, 4.2], fov: 42 }} gl={{ antialias: true, alpha: true, powerPreference: "high-performance", preserveDrawingBuffer: true }}
          dpr={[1, cap]} style={{ pointerEvents: stage ? "none" : "auto" }}
          onCreated={({ gl, scene }) => {
            scene.fog = new THREE.Fog("#080b0f", 10, 42);     // the strands run off into the dark, never cut
            let timer = 0;
            gl.domElement.addEventListener("webglcontextlost", () => { timer = window.setTimeout(() => setEpoch((e) => e + 1), 2500); });
            gl.domElement.addEventListener("webglcontextrestored", () => { window.clearTimeout(timer); });
          }}>
          <ContextGuard />
          <FrameThrottle />
          {/* the strands: one wide behind the face, two thin ones crossing it, all turning with the mouse */}
          <group position={[0, -0.2, -7]} scale={0.8}>
            <HelixBackdrop colors={colors} position={[0, 0.3, 0]} scale={1} tempo={stage ? 1.6 : 1.15} spinRate={stage ? 1.8 : 1.2} />
          </group>
          {/* the far strands never bend under the mouse (nobody can tell at that depth; it was 3600 projections a frame) */}
          {level !== "minimal" && (
            <group position={[-7.5, 1.5, -17]} rotation={[0.2, 0, 0.35]} scale={0.8}>
              <HelixBackdrop colors={colors} position={[0, 0, 0]} scale={1} tempo={1.4} spinRate={stage ? 3 : 2.2} bendable={false} />
            </group>
          )}
          {level === "full" && (
            <group position={[8, -2, -19]} rotation={[-0.1, 0, -0.45]} scale={0.7}>
              <HelixBackdrop colors={colors} position={[0, 0, 0]} scale={1} tempo={1.6} spinRate={stage ? 3.6 : 2.6} bendable={false} />
            </group>
          )}
          <Body look={look} onPulse={(big) => { if (big) setCurtain((c) => c + 1); }} mode={stage ? "stage" : "full"} roam={roam} />
        </Canvas>
      </div>
      {stage && (
        <div className="stage-bar">
          <span className="stage-word">HELIX · UP FRONT</span>
          <button className="task-btn" onClick={onClick}>Talk to me</button>
          <button className="task-btn dim" onClick={onClose}>Back to work · Esc</button>
        </div>
      )}
      <Curtain trigger={curtain} hue={look.hue} />
    </>
  );
}
