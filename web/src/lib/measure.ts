// The ruler's geometry — pure TypeScript, no DOM. The camera panel's Measure mode is built on it:
//   • calibration from two clicks across a known length (a credit card, the HELIX marker…),
//   • a drag in TODAY's frame turned into millimetres THROUGH the calibration frame — the tracker's
//     similarity says how much closer or farther the camera has drifted since the calibration, so
//     the scale is exact for the similarity model (same plane, any drift/turn/zoom),
//   • the 1:1 hologram size, the 10 mm grid, the payload HELIX receives, and the panel's words.
//
// Units, once and for all: a point is normalized frame coordinates [0..1] (what the tracker's
// callers pass around); `mmPerPx` is millimetres per VIDEO pixel at the calibration frame (the
// camera's native width — 0.19 mm/px reads as a real number to a person); the tracker's transforms
// only ever contribute unit-free RATIOS (scaleOf), so tracker pixels and video pixels never mix.
// An uncalibrated panel has no scale and these functions are never asked for pixels.
import { relative, scaleOf, type Similarity } from "./track";

export type Norm = [number, number];

export interface Pt {
  x: number;
  y: number;
}

/** A known length the user can click across. The millimetres are the reference's, not ours:
 * ISO/IEC 7810 ID-1 for the card, the HELIX AR marker's outer square, the US Mint, IEC 60086. */
export interface Preset {
  key: string;
  label: string;
  mm: number; // 0 = the user types it
  reference: string; // the words that ride with the measurement ("card long edge")
}

export const PRESETS: Preset[] = [
  { key: "card_long", label: "Credit card, long edge — 85.60 mm", mm: 85.6, reference: "card long edge" },
  { key: "card_short", label: "Credit card, short edge — 53.98 mm", mm: 53.98, reference: "card short edge" },
  { key: "marker", label: "HELIX marker — 80.0 mm square", mm: 80, reference: "HELIX marker" },
  { key: "quarter", label: "US quarter — 24.26 mm", mm: 24.26, reference: "US quarter" },
  { key: "aa", label: "AA cell, length — 50.5 mm", mm: 50.5, reference: "AA cell length" },
  { key: "custom", label: "Custom length (mm)…", mm: 0, reference: "custom length" },
];

export const UNCALIBRATED_HINT =
  "Not calibrated yet — pick a reference, press Calibrate, and click its two ends (a credit card's long edge works).";

export interface Calibration {
  mmPerPx: number; // millimetres per video pixel, at the calibration frame
  T: Similarity; // the tracker's BASE→frame transform when the two clicks were made
  reference: string; // "card long edge", …
  mm: number; // the known length
  a: Norm; // the two clicks, normalized in that frame
  b: Norm;
  W: number; // the video's size in pixels — the normalized↔pixel bridge for this calibration
  H: number;
}

export interface Measurement {
  id: string;
  kind: "distance" | "box";
  label: string;
  T: Similarity; // the frame it was dragged in
  a: Norm;
  b: Norm;
  mm: number; // the distance (kind "distance")
  w: number; // the box's sides in mm (kind "box")
  h: number;
}

export interface MeasureItemPayload {
  kind: "distance" | "box";
  label: string;
  mm?: number;
  w_mm?: number;
  h_mm?: number;
}

/** POST /api/camera/{id}/measure — MAKER_FLOW §5. */
export interface MeasurePayload {
  mm_per_px: number;
  reference: string;
  items: MeasureItemPayload[];
}

export const MIN_CALIBRATION_PX = 4; // two clicks closer than this are the same click

// ----- calibration -----

/** Millimetres per pixel from two points a known distance apart (in the same pixel space).
 * 0 means "no calibration": the clicks sit on top of each other, or the length isn't a length. */
export function calibrate(p1: Pt, p2: Pt, mm: number): number {
  const d = Math.hypot(p2.x - p1.x, p2.y - p1.y);
  if (!(mm > 0) || !Number.isFinite(mm) || !(d >= MIN_CALIBRATION_PX)) return 0;
  return mm / d;
}

/** How much bigger a length measured NOW was in the calibration frame: s_cal / s_now. A length of
 * d pixels now spanned d·driftScale pixels when the calibration was made — so
 * mm = d · driftScale · mmPerPx, whatever the camera did in between. */
export function driftScale(cal: Similarity, now: Similarity): number {
  const k = scaleOf(relative(cal, now));
  return Number.isFinite(k) && k > 0 ? k : 1;
}

export function toPx(size: { W: number; H: number }, p: Norm): Pt {
  return { x: p[0] * size.W, y: p[1] * size.H };
}

/** A calibration from two clicks (normalized, in the frame whose transform is `T`). null when the
 * clicks can't calibrate anything. */
export function makeCalibration(
  a: Norm, b: Norm, mm: number, reference: string, T: Similarity, W: number, H: number,
): Calibration | null {
  if (!(W > 0) || !(H > 0)) return null;
  const size = { W, H };
  const mmPerPx = calibrate(toPx(size, a), toPx(size, b), mm);
  if (!(mmPerPx > 0)) return null;
  return { mmPerPx, T: { ...T }, reference, mm, a: [a[0], a[1]], b: [b[0], b[1]], W, H };
}

// ----- measuring through the calibration -----

/** The straight-line distance in mm between two points dragged in the frame `now`. */
export function lengthMm(cal: Calibration, now: Similarity, a: Norm, b: Norm): number {
  const p = toPx(cal, a);
  const q = toPx(cal, b);
  return Math.hypot(q.x - p.x, q.y - p.y) * driftScale(cal.T, now) * cal.mmPerPx;
}

/** A box dragged in the frame `now`: its sides in mm along that frame's axes. */
export function boxMm(cal: Calibration, now: Similarity, a: Norm, b: Norm): { w: number; h: number } {
  const p = toPx(cal, a);
  const q = toPx(cal, b);
  const k = driftScale(cal.T, now) * cal.mmPerPx;
  return { w: Math.abs(q.x - p.x) * k, h: Math.abs(q.y - p.y) * k };
}

/** A hologram placement's `size` (normalized frame-widths per millimetre) that puts 1 mm on the
 * plate at 1 mm on the desk when anchored in the frame `now`: 1 / (mmPerPx · drift · frameWidth).
 * Anchored in the calibration frame itself that is exactly 1 / (mmPerPx · frameWidth). */
export function trueScaleSize(cal: Calibration, now: Similarity): number {
  return 1 / (cal.mmPerPx * driftScale(cal.T, now) * cal.W);
}

// ----- the grid -----

export interface Segment {
  a: Norm; // in the calibration frame, normalized
  b: Norm;
  major: boolean; // every fifth line (50 mm at the 10 mm step)
}

/** The 10 mm grid over the calibrated plane: aligned with the calibration segment (so it lines up
 * with the card's edge), through the first click, covering `region` — points in the calibration
 * frame (the live view's corners, mapped back). Capped per axis so a wide view never asks for
 * thousands of lines; the cap keeps the lines nearest the region's centre. */
export function gridLines(cal: Calibration, region: Norm[], stepMm = 10, maxLines = 60): Segment[] {
  if (!(cal.mmPerPx > 0) || !(stepMm > 0) || region.length < 2) return [];
  const o = toPx(cal, cal.a);
  const bPx = toPx(cal, cal.b);
  let ux = bPx.x - o.x;
  let uy = bPx.y - o.y;
  const len = Math.hypot(ux, uy);
  if (len < 1e-6) {
    ux = 1;
    uy = 0;
  } else {
    ux /= len;
    uy /= len;
  }
  const vx = -uy;
  const vy = ux;
  const step = stepMm / cal.mmPerPx; // pixels per grid step
  let sMin = Infinity;
  let sMax = -Infinity;
  let tMin = Infinity;
  let tMax = -Infinity;
  for (const p of region) {
    const q = toPx(cal, p);
    const dx = q.x - o.x;
    const dy = q.y - o.y;
    const s = (dx * ux + dy * uy) / step;
    const t = (dx * vx + dy * vy) / step;
    if (s < sMin) sMin = s;
    if (s > sMax) sMax = s;
    if (t < tMin) tMin = t;
    if (t > tMax) tMax = t;
  }
  if (!Number.isFinite(sMin) || !Number.isFinite(tMin)) return [];
  const clamp = (lo: number, hi: number): [number, number] => {
    let a = Math.floor(lo);
    let b = Math.ceil(hi);
    if (b - a > maxLines) {
      const mid = Math.round((lo + hi) / 2);
      a = mid - Math.floor(maxLines / 2);
      b = mid + Math.ceil(maxLines / 2);
    }
    return [a, b];
  };
  const [s0, s1] = clamp(sMin, sMax);
  const [t0, t1] = clamp(tMin, tMax);
  const norm = (x: number, y: number): Norm => [x / cal.W, y / cal.H];
  const at = (s: number, t: number): Norm =>
    norm(o.x + (ux * s + vx * t) * step, o.y + (uy * s + vy * t) * step);
  const out: Segment[] = [];
  for (let i = s0; i <= s1; i++) out.push({ a: at(i, t0), b: at(i, t1), major: i % 5 === 0 });
  for (let j = t0; j <= t1; j++) out.push({ a: at(s0, j), b: at(s1, j), major: j % 5 === 0 });
  return out;
}

// ----- words and payloads -----

export function fmtMm(v: number): string {
  return (Math.round(v * 10) / 10).toFixed(1);
}

/** The scale to three significant figures — "0.19", and a close-up's "0.0523" stays a number. */
export function fmtScale(mmPerPx: number): string {
  return Number(mmPerPx.toPrecision(3)).toString();
}

export function scaleLine(cal: Calibration | null): string {
  if (!cal) return "Not calibrated — HELIX won't guess a size from pixels.";
  return `${fmtScale(cal.mmPerPx)} mm/px · calibrated on a ${cal.reference}`;
}

export function describe(m: Pick<Measurement, "kind" | "mm" | "w" | "h">): string {
  return m.kind === "box" ? `${fmtMm(m.w)} × ${fmtMm(m.h)} mm` : `${fmtMm(m.mm)} mm`;
}

/** "part 1", "part 2", … — the first name not already in the list. */
export function nextLabel(items: { label: string }[]): string {
  let n = items.length + 1;
  const taken = new Set(items.map((i) => i.label.trim().toLowerCase()));
  while (taken.has(`part ${n}`)) n++;
  return `part ${n}`;
}

export function measurePayload(cal: Calibration, items: Measurement[]): MeasurePayload {
  const round1 = (v: number) => Math.round(v * 10) / 10;
  return {
    mm_per_px: cal.mmPerPx,
    reference: cal.reference,
    items: items.map((m, i) => {
      const label = m.label.trim() || `part ${i + 1}`; // a blanked label still names its row
      return m.kind === "box"
        ? { kind: "box", label, w_mm: round1(m.w), h_mm: round1(m.h) }
        : { kind: "distance", label, mm: round1(m.mm) };
    }),
  };
}
