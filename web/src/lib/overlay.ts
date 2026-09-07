// Drawing the model's callouts over the live view — and the user's ruler. Coordinates arrive
// normalized to the frame the model saw (or the frame a measurement was dragged in); `map` turns
// them into screen pixels for NOW (through the tracker), so the drawing rides on the board. Text
// stays upright and readable no matter how the board turned.
import { describe, fmtMm, gridLines, type Calibration, type Measurement, type Norm } from "./measure";
import type { OverlayGroup, OverlayItem } from "./store";
import type { Similarity } from "./track";

export type MapFn = (frame: string, x: number, y: number) => [number, number];
export type ScaleFn = (frame: string) => number; // how much bigger the frame's content is now
/** A point normalized in the frame whose BASE→frame transform is `at` → the screen, NOW. */
export type MapAtFn = (at: Similarity, x: number, y: number) => [number, number];
/** A point normalized in the LIVE frame → normalized in the frame `at` (the inverse of MapAtFn). */
export type ToFrameFn = (at: Similarity, x: number, y: number) => [number, number];

const PALETTE: Record<string, string> = {
  cyan: "#3fe0e0",
  green: "#3fe07a",
  amber: "#ffcf45",
  red: "#ff5d62",
  blue: "#5aa8ff",
  magenta: "#ff5df2",
  white: "#f2f6f8",
};

function color(c?: string): string {
  if (!c) return PALETTE.cyan;
  const k = c.toLowerCase();
  if (PALETTE[k]) return PALETTE[k];
  if (/^#[0-9a-f]{3,8}$/i.test(c)) return c;
  return PALETTE.cyan;
}

function tag(ctx: CanvasRenderingContext2D, x: number, y: number, text: string, col: string, above = true) {
  ctx.save();
  ctx.font = "600 12px Inter, 'Segoe UI', sans-serif";
  const pad = 5;
  const w = ctx.measureText(text).width + pad * 2;
  const h = 18;
  const bx = Math.round(x - w / 2);
  const by = Math.round(above ? y - h - 6 : y + 6);
  ctx.fillStyle = "rgba(5,8,11,0.85)";
  ctx.strokeStyle = col;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(bx, by, w, h, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = col;
  ctx.textBaseline = "middle";
  ctx.fillText(text, bx + pad, by + h / 2 + 0.5);
  ctx.restore();
}

function arrowHead(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, size: number) {
  const a = Math.atan2(y2 - y1, x2 - x1);
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - size * Math.cos(a - 0.45), y2 - size * Math.sin(a - 0.45));
  ctx.lineTo(x2 - size * Math.cos(a + 0.45), y2 - size * Math.sin(a + 0.45));
  ctx.closePath();
  ctx.fill();
}

export function drawItem(ctx: CanvasRenderingContext2D, frame: string, it: OverlayItem, map: MapFn, k: number, pulse: number) {
  const col = color(it.color);
  ctx.save();
  ctx.strokeStyle = col;
  ctx.fillStyle = col;
  ctx.lineWidth = 2;
  ctx.shadowColor = col;
  ctx.shadowBlur = 6 + pulse * 6;
  ctx.lineJoin = "round";
  const x = it.x ?? 0.5;
  const y = it.y ?? 0.5;
  switch (it.kind) {
    case "box": {
      const w = it.w ?? 0.1;
      const h = it.h ?? 0.1;
      const c = [map(frame, x, y), map(frame, x + w, y), map(frame, x + w, y + h), map(frame, x, y + h)];
      ctx.beginPath();
      ctx.moveTo(c[0][0], c[0][1]);
      for (let i = 1; i < 4; i++) ctx.lineTo(c[i][0], c[i][1]);
      ctx.closePath();
      ctx.stroke();
      ctx.globalAlpha = 0.12;
      ctx.fill();
      ctx.globalAlpha = 1;
      if (it.text) tag(ctx, (c[0][0] + c[1][0]) / 2, Math.min(c[0][1], c[1][1]), it.text, col);
      break;
    }
    case "circle": {
      const r = it.r ?? 0.04;
      const [cx, cy] = map(frame, x, y);
      const [ex] = map(frame, x + r, y);
      const [, ey] = map(frame, x, y + r);
      const pr = Math.max(6, Math.hypot(ex - cx, ey - cy)) * k;
      ctx.beginPath();
      ctx.arc(cx, cy, pr, 0, Math.PI * 2);
      ctx.stroke();
      if (it.text) tag(ctx, cx, cy - pr, it.text, col);
      break;
    }
    case "arrow": {
      const [x1, y1] = map(frame, x, y);
      const [x2, y2] = map(frame, it.x2 ?? x + 0.1, it.y2 ?? y + 0.1);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      arrowHead(ctx, x1, y1, x2, y2, 11);
      if (it.text) tag(ctx, x1, y1, it.text, col, y1 <= y2);
      break;
    }
    case "label": {
      const [px, py] = map(frame, x, y);
      ctx.beginPath();
      ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fill();
      if (it.text) tag(ctx, px, py, it.text, col);
      break;
    }
    case "pin": {
      const [px, py] = map(frame, x, y);
      const r = 7 + pulse * 2;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(px, py, 2, 0, Math.PI * 2);
      ctx.fill();
      if (it.text) tag(ctx, px, py - r, it.text, col);
      break;
    }
    case "wire": {
      const pts = (it.points || []).filter((p) => p.length >= 2);
      if (pts.length < 2) break;
      ctx.lineWidth = 3;
      ctx.setLineDash([9, 6]);
      ctx.lineDashOffset = -pulse * 15;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const [px, py] = map(frame, p[0], p[1]);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
      ctx.setLineDash([]);
      const [sx, sy] = map(frame, pts[0][0], pts[0][1]);
      const [ex, ey] = map(frame, pts[pts.length - 1][0], pts[pts.length - 1][1]);
      ctx.beginPath();
      ctx.arc(sx, sy, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(ex, ey, 4, 0, Math.PI * 2);
      ctx.fill();
      if (it.text) {
        const mid = pts[Math.floor(pts.length / 2)];
        const [mx, my] = map(frame, mid[0], mid[1]);
        tag(ctx, mx, my, it.text, col);
      }
      break;
    }
  }
  ctx.restore();
}

export function drawOverlays(
  ctx: CanvasRenderingContext2D, groups: OverlayGroup[], map: MapFn, scale: ScaleFn, t: number,
) {
  const pulse = (Math.sin(t / 380) + 1) / 2;
  for (const g of groups) {
    const k = scale(g.frame);
    for (const it of g.items) drawItem(ctx, g.frame, it, map, k, pulse);
  }
}

// ----- the ruler -----------------------------------------------------------------------------

/** Everything Measure mode draws: each piece anchored to the frame it was made in, so it rides. */
export interface MeasureLayer {
  cal: Calibration | null;
  items: Measurement[];
  draft: Measurement | null; // the drag in progress (its mm read live, no label yet)
  grid: boolean; // the 10 mm grid over the calibrated plane
  pending: { T: Similarity; a: Norm } | null; // the first calibration click, waiting for the second
}

const RULER = "#3fe07a"; // the calibration: green — "this is the known length"
const MEASURE = "#f2f6f8"; // the user's measurements: white
const GRID = "#3fe0e0";

function tick(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, size: number) {
  // perpendicular end ticks on a distance line, so the endpoints read like a ruler's
  const a = Math.atan2(y2 - y1, x2 - x1) + Math.PI / 2;
  const dx = Math.cos(a) * size;
  const dy = Math.sin(a) * size;
  ctx.beginPath();
  ctx.moveTo(x1 - dx, y1 - dy);
  ctx.lineTo(x1 + dx, y1 + dy);
  ctx.moveTo(x2 - dx, y2 - dy);
  ctx.lineTo(x2 + dx, y2 + dy);
  ctx.stroke();
}

function drawMeasurement(ctx: CanvasRenderingContext2D, m: Measurement, mapAt: MapAtFn, col: string, text: string) {
  ctx.save();
  ctx.strokeStyle = col;
  ctx.fillStyle = col;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.shadowColor = "rgba(0,0,0,0.6)";
  ctx.shadowBlur = 4;
  if (m.kind === "box") {
    const c = [
      mapAt(m.T, m.a[0], m.a[1]), mapAt(m.T, m.b[0], m.a[1]),
      mapAt(m.T, m.b[0], m.b[1]), mapAt(m.T, m.a[0], m.b[1]),
    ];
    ctx.beginPath();
    ctx.moveTo(c[0][0], c[0][1]);
    for (let i = 1; i < 4; i++) ctx.lineTo(c[i][0], c[i][1]);
    ctx.closePath();
    ctx.stroke();
    ctx.globalAlpha = 0.1;
    ctx.fill();
    ctx.globalAlpha = 1;
    const top = c.reduce((best, p) => (p[1] < best[1] ? p : best), c[0]);
    const cx = (c[0][0] + c[2][0]) / 2;
    tag(ctx, cx, top[1], text, col);
  } else {
    const [x1, y1] = mapAt(m.T, m.a[0], m.a[1]);
    const [x2, y2] = mapAt(m.T, m.b[0], m.b[1]);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    tick(ctx, x1, y1, x2, y2, 6);
    tag(ctx, (x1 + x2) / 2, Math.min(y1, y2), text, col);
  }
  ctx.restore();
}

/** Measure mode's drawing pass: the grid, the calibration, the measurements, the drag under way. */
export function drawMeasureLayer(
  ctx: CanvasRenderingContext2D, layer: MeasureLayer, mapAt: MapAtFn, toFrame: ToFrameFn, t: number,
) {
  const pulse = (Math.sin(t / 300) + 1) / 2;
  const cal = layer.cal;
  if (cal && layer.grid) {
    // the live view's corners, mapped back into the calibration frame → the region to cover
    const region: Norm[] = [[0, 0], [1, 0], [1, 1], [0, 1]].map(([x, y]) => toFrame(cal.T, x, y));
    ctx.save();
    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    for (const s of gridLines(cal, region)) {
      const [x1, y1] = mapAt(cal.T, s.a[0], s.a[1]);
      const [x2, y2] = mapAt(cal.T, s.b[0], s.b[1]);
      ctx.globalAlpha = s.major ? 0.45 : 0.16;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }
    ctx.restore();
  }
  if (cal) {
    // the known length itself, faint once the grid is off, so the calibration is never a secret
    const [x1, y1] = mapAt(cal.T, cal.a[0], cal.a[1]);
    const [x2, y2] = mapAt(cal.T, cal.b[0], cal.b[1]);
    ctx.save();
    ctx.strokeStyle = RULER;
    ctx.fillStyle = RULER;
    ctx.lineWidth = 1.5;
    ctx.globalAlpha = layer.grid ? 0.95 : 0.55;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.setLineDash([]);
    tick(ctx, x1, y1, x2, y2, 5);
    if (layer.grid) tag(ctx, (x1 + x2) / 2, Math.max(y1, y2), `${fmtMm(cal.mm)} mm · ${cal.reference}`, RULER, false);
    ctx.restore();
  }
  if (layer.pending) {
    const [px, py] = mapAt(layer.pending.T, layer.pending.a[0], layer.pending.a[1]);
    ctx.save();
    ctx.strokeStyle = RULER;
    ctx.fillStyle = RULER;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(px, py, 6 + pulse * 3, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(px, py, 2, 0, Math.PI * 2);
    ctx.fill();
    tag(ctx, px, py - 8, "now click the other end", RULER);
    ctx.restore();
  }
  for (const m of layer.items) drawMeasurement(ctx, m, mapAt, MEASURE, `${m.label} · ${describe(m)}`);
  if (layer.draft) drawMeasurement(ctx, layer.draft, mapAt, "#ffcf45", describe(layer.draft));
}
