// AR-lite tracking for the camera panel: keeps callouts and holograms glued to the thing in front
// of the lens as the board (or the camera) drifts, turns, or comes closer.
//
// Pure TypeScript, no DOM here (the panel feeds it grayscale frames): Shi-Tomasi corners →
// pyramidal Lucas–Kanade optical flow with a forward–backward check → a RANSAC-fitted 2D
// similarity (scale + rotation + translation) per frame, composed into one running transform
// from a BASE frame to "now". Anything anchored to a captured frame F maps to the live view via
// T_now ∘ inv(T_F). A webcam looking at a board on a desk is close enough to a plane that a
// similarity holds; when the tracks collapse (a hand sweeps through, the light changes) the
// transform freezes instead of flying off, and tracking resumes from wherever the scene is.
//
// Runs comfortably at 20 fps on a 320-px-wide frame: ~120 points × 3 pyramid levels.

export interface Similarity {
  a: number; // scale·cos θ
  b: number; // scale·sin θ
  tx: number;
  ty: number;
}

export const IDENTITY: Similarity = { a: 1, b: 0, tx: 0, ty: 0 };

export function apply(t: Similarity, x: number, y: number): [number, number] {
  return [t.a * x - t.b * y + t.tx, t.b * x + t.a * y + t.ty];
}

/** first, then second — (second ∘ first). */
export function compose(second: Similarity, first: Similarity): Similarity {
  return {
    a: second.a * first.a - second.b * first.b,
    b: second.b * first.a + second.a * first.b,
    tx: second.a * first.tx - second.b * first.ty + second.tx,
    ty: second.b * first.tx + second.a * first.ty + second.ty,
  };
}

export function invert(t: Similarity): Similarity {
  const d = t.a * t.a + t.b * t.b || 1e-9;
  const a = t.a / d;
  const b = -t.b / d;
  return { a, b, tx: -(a * t.tx - b * t.ty), ty: -(b * t.tx + a * t.ty) };
}

export function scaleOf(t: Similarity): number {
  return Math.hypot(t.a, t.b);
}

export function angleOf(t: Similarity): number {
  return Math.atan2(t.b, t.a);
}

export interface Gray {
  w: number;
  h: number;
  data: Float32Array; // row-major, 0..255
}

export interface TrackState {
  /** BASE → now. */
  T: Similarity;
  /** How many points survived this frame (0 = nothing to track yet / lost). */
  tracked: number;
  /** True while the last update produced a fresh, trusted motion estimate. */
  locked: boolean;
}

// --- tuning ---------------------------------------------------------------------------------
const LEVELS = 3;
const WIN = 3; // LK window half-size → 7×7
const LK_ITERS = 10;
const LK_EPS = 0.02;
const MAX_POINTS = 140;
const MIN_POINTS = 24; // below this we re-detect
const MIN_INLIERS = 8; // below this the motion isn't trusted
const RANSAC_ITERS = 64;
const RANSAC_TOL = 1.6; // px at level 0
const FB_TOL = 1.0; // forward-backward consistency, px
const REDETECT_EVERY = 45; // frames — refresh points even when healthy so drift can't ossify

// --- image helpers --------------------------------------------------------------------------
function downsample(src: Gray): Gray {
  const w = src.w >> 1;
  const h = src.h >> 1;
  const out = new Float32Array(w * h);
  const s = src.data;
  const sw = src.w;
  for (let y = 0; y < h; y++) {
    const r0 = 2 * y * sw;
    const r1 = r0 + sw;
    for (let x = 0; x < w; x++) {
      const c = 2 * x;
      out[y * w + x] = 0.25 * (s[r0 + c] + s[r0 + c + 1] + s[r1 + c] + s[r1 + c + 1]);
    }
  }
  return { w, h, data: out };
}

/** A light 3×3 blur: LK derivatives on raw camera noise wobble; this steadies them. */
function blur(src: Gray): Gray {
  const { w, h, data: s } = src;
  const out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    const ym = Math.max(0, y - 1) * w;
    const y0 = y * w;
    const yp = Math.min(h - 1, y + 1) * w;
    for (let x = 0; x < w; x++) {
      const xm = Math.max(0, x - 1);
      const xp = Math.min(w - 1, x + 1);
      out[y0 + x] =
        (s[ym + xm] + 2 * s[ym + x] + s[ym + xp] +
          2 * s[y0 + xm] + 4 * s[y0 + x] + 2 * s[y0 + xp] +
          s[yp + xm] + 2 * s[yp + x] + s[yp + xp]) / 16;
    }
  }
  return { w, h, data: out };
}

function pyramid(g: Gray): Gray[] {
  const out = [blur(g)];
  for (let i = 1; i < LEVELS; i++) out.push(blur(downsample(out[i - 1])));
  return out;
}

function sample(img: Gray, x: number, y: number): number {
  // bilinear, clamped
  const w = img.w;
  const h = img.h;
  if (x < 0) x = 0;
  if (y < 0) y = 0;
  if (x > w - 1.001) x = w - 1.001;
  if (y > h - 1.001) y = h - 1.001;
  const x0 = x | 0;
  const y0 = y | 0;
  const fx = x - x0;
  const fy = y - y0;
  const d = img.data;
  const i = y0 * w + x0;
  return (
    d[i] * (1 - fx) * (1 - fy) +
    d[i + 1] * fx * (1 - fy) +
    d[i + w] * (1 - fx) * fy +
    d[i + w + 1] * fx * fy
  );
}

// --- corners --------------------------------------------------------------------------------
export function detectCorners(img: Gray, max = MAX_POINTS): Float32Array {
  const { w, h, data } = img;
  const ix = new Float32Array(w * h);
  const iy = new Float32Array(w * h);
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      ix[i] = (data[i + 1] - data[i - 1]) * 0.5;
      iy[i] = (data[i + w] - data[i - w]) * 0.5;
    }
  }
  // structure tensor over a 5×5 window → min eigenvalue (Shi–Tomasi)
  const score = new Float32Array(w * h);
  const R = 2;
  let maxScore = 0;
  for (let y = 4; y < h - 4; y++) {
    for (let x = 4; x < w - 4; x++) {
      let sxx = 0;
      let sxy = 0;
      let syy = 0;
      for (let dy = -R; dy <= R; dy++) {
        const row = (y + dy) * w + x;
        for (let dx = -R; dx <= R; dx++) {
          const gx = ix[row + dx];
          const gy = iy[row + dx];
          sxx += gx * gx;
          sxy += gx * gy;
          syy += gy * gy;
        }
      }
      const tr = (sxx + syy) * 0.5;
      const det = Math.sqrt(Math.max(0, ((sxx - syy) * 0.5) ** 2 + sxy * sxy));
      const s = tr - det;
      score[y * w + x] = s;
      if (s > maxScore) maxScore = s;
    }
  }
  if (maxScore <= 0) return new Float32Array(0);
  const thresh = maxScore * 0.01;
  // non-max suppression + bucketing so points cover the frame, not one busy corner of it
  const bx = 8;
  const by = 6;
  const per = Math.max(2, Math.ceil(max / (bx * by)) + 1);
  const buckets: number[][] = Array.from({ length: bx * by }, () => []);
  for (let y = 5; y < h - 5; y++) {
    for (let x = 5; x < w - 5; x++) {
      const i = y * w + x;
      const s = score[i];
      if (s < thresh) continue;
      let isMax = true;
      for (let dy = -2; dy <= 2 && isMax; dy++) {
        for (let dx = -2; dx <= 2; dx++) {
          if ((dx || dy) && score[i + dy * w + dx] > s) {
            isMax = false;
            break;
          }
        }
      }
      if (!isMax) continue;
      const b = Math.min(by - 1, ((y / h) * by) | 0) * bx + Math.min(bx - 1, ((x / w) * bx) | 0);
      buckets[b].push(i);
    }
  }
  const chosen: number[] = [];
  for (const list of buckets) {
    list.sort((p, q) => score[q] - score[p]);
    for (let k = 0; k < Math.min(per, list.length); k++) chosen.push(list[k]);
  }
  chosen.sort((p, q) => score[q] - score[p]);
  const n = Math.min(max, chosen.length);
  const pts = new Float32Array(n * 2);
  for (let k = 0; k < n; k++) {
    pts[2 * k] = chosen[k] % w;
    pts[2 * k + 1] = (chosen[k] / w) | 0;
  }
  return pts;
}

// --- optical flow ---------------------------------------------------------------------------
/** Track one point from `prev` to `cur` pyramids. Returns [x, y] or null. */
function lk(prev: Gray[], cur: Gray[], px: number, py: number): [number, number] | null {
  let gx = 0;
  let gy = 0;
  for (let L = LEVELS - 1; L >= 0; L--) {
    const P = prev[L];
    const C = cur[L];
    const s = 1 / (1 << L);
    const x = px * s;
    const y = py * s;
    if (x < WIN + 1 || y < WIN + 1 || x > P.w - WIN - 2 || y > P.h - WIN - 2) return null;
    // gradients + template from the previous image (fixed per level)
    const n = (2 * WIN + 1) ** 2;
    const tIx = new Float32Array(n);
    const tIy = new Float32Array(n);
    const tI = new Float32Array(n);
    let g11 = 0;
    let g12 = 0;
    let g22 = 0;
    let k = 0;
    for (let dy = -WIN; dy <= WIN; dy++) {
      for (let dx = -WIN; dx <= WIN; dx++) {
        const sx = x + dx;
        const sy = y + dy;
        const ix = (sample(P, sx + 1, sy) - sample(P, sx - 1, sy)) * 0.5;
        const iy = (sample(P, sx, sy + 1) - sample(P, sx, sy - 1)) * 0.5;
        tIx[k] = ix;
        tIy[k] = iy;
        tI[k] = sample(P, sx, sy);
        g11 += ix * ix;
        g12 += ix * iy;
        g22 += iy * iy;
        k++;
      }
    }
    const det = g11 * g22 - g12 * g12;
    if (det < 1e-3) return null; // flat / edge-only patch: unobservable
    const inv11 = g22 / det;
    const inv12 = -g12 / det;
    const inv22 = g11 / det;
    let vx = 0;
    let vy = 0;
    for (let it = 0; it < LK_ITERS; it++) {
      let b1 = 0;
      let b2 = 0;
      k = 0;
      const cx = x + gx + vx;
      const cy = y + gy + vy;
      if (cx < 1 || cy < 1 || cx > C.w - 2 || cy > C.h - 2) return null;
      for (let dy = -WIN; dy <= WIN; dy++) {
        for (let dx = -WIN; dx <= WIN; dx++) {
          const it_ = tI[k] - sample(C, cx + dx, cy + dy);
          b1 += it_ * tIx[k];
          b2 += it_ * tIy[k];
          k++;
        }
      }
      const dxv = inv11 * b1 + inv12 * b2;
      const dyv = inv12 * b1 + inv22 * b2;
      vx += dxv;
      vy += dyv;
      if (dxv * dxv + dyv * dyv < LK_EPS * LK_EPS) break;
    }
    if (L > 0) {
      gx = 2 * (gx + vx);
      gy = 2 * (gy + vy);
    } else {
      gx += vx;
      gy += vy;
    }
  }
  const nx = px + gx;
  const ny = py + gy;
  if (!Number.isFinite(nx) || !Number.isFinite(ny)) return null;
  return [nx, ny];
}

// --- similarity fit -------------------------------------------------------------------------
function fitSimilarity(
  src: Float32Array, dst: Float32Array, idx: number[],
): Similarity | null {
  // closed-form least squares: q = s·R·p + t, via complex-number regression
  const n = idx.length;
  if (n < 2) return null;
  let mpx = 0;
  let mpy = 0;
  let mqx = 0;
  let mqy = 0;
  for (const i of idx) {
    mpx += src[2 * i];
    mpy += src[2 * i + 1];
    mqx += dst[2 * i];
    mqy += dst[2 * i + 1];
  }
  mpx /= n;
  mpy /= n;
  mqx /= n;
  mqy /= n;
  let num_re = 0;
  let num_im = 0;
  let den = 0;
  for (const i of idx) {
    const px = src[2 * i] - mpx;
    const py = src[2 * i + 1] - mpy;
    const qx = dst[2 * i] - mqx;
    const qy = dst[2 * i + 1] - mqy;
    // conj(p) · q
    num_re += px * qx + py * qy;
    num_im += px * qy - py * qx;
    den += px * px + py * py;
  }
  if (den < 1e-6) return null;
  const a = num_re / den;
  const b = num_im / den;
  return { a, b, tx: mqx - (a * mpx - b * mpy), ty: mqy - (b * mpx + a * mpy) };
}

export function ransacSimilarity(
  src: Float32Array, dst: Float32Array, count: number,
): { T: Similarity; inliers: number[] } | null {
  if (count < 2) return null;
  let best: number[] = [];
  let seed = 12345 + count;
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };
  const tol2 = RANSAC_TOL * RANSAC_TOL;
  for (let it = 0; it < RANSAC_ITERS; it++) {
    const i = (rnd() * count) | 0;
    let j = (rnd() * count) | 0;
    if (j === i) j = (j + 1) % count;
    const t = fitSimilarity(src, dst, [i, j]);
    if (!t) continue;
    const s = scaleOf(t);
    if (s < 0.5 || s > 2.0) continue; // one frame can't double the size — a bad sample
    const inl: number[] = [];
    for (let k = 0; k < count; k++) {
      const [x, y] = apply(t, src[2 * k], src[2 * k + 1]);
      const dx = x - dst[2 * k];
      const dy = y - dst[2 * k + 1];
      if (dx * dx + dy * dy <= tol2) inl.push(k);
    }
    if (inl.length > best.length) best = inl;
    if (best.length > count * 0.9) break;
  }
  if (best.length < MIN_INLIERS) return null;
  const T = fitSimilarity(src, dst, best);
  if (!T) return null;
  // one refinement pass on the refit's inliers
  const inl: number[] = [];
  for (let k = 0; k < count; k++) {
    const [x, y] = apply(T, src[2 * k], src[2 * k + 1]);
    const dx = x - dst[2 * k];
    const dy = y - dst[2 * k + 1];
    if (dx * dx + dy * dy <= tol2 * 1.5) inl.push(k);
  }
  const T2 = inl.length >= MIN_INLIERS ? fitSimilarity(src, dst, inl) : T;
  return { T: T2 || T, inliers: inl.length >= MIN_INLIERS ? inl : best };
}

// --- the tracker ----------------------------------------------------------------------------
export class Tracker {
  private prev: Gray[] | null = null;
  private pts: Float32Array<ArrayBufferLike> = new Float32Array(0);
  private count = 0;
  private sinceDetect = 0;
  private lostFrames = 0;
  T: Similarity = { ...IDENTITY };
  state: TrackState = { T: { ...IDENTITY }, tracked: 0, locked: false };

  reset(): void {
    this.prev = null;
    this.pts = new Float32Array(0);
    this.count = 0;
    this.T = { ...IDENTITY };
    this.state = { T: { ...IDENTITY }, tracked: 0, locked: false };
  }

  /** A copy of BASE→now, to remember alongside a captured frame. */
  snapshot(): Similarity {
    return { ...this.T };
  }

  /** Feed one grayscale frame (all frames the same size). */
  update(frame: Gray): TrackState {
    const cur = pyramid(frame);
    if (this.prev === null || this.count < MIN_POINTS || this.sinceDetect >= REDETECT_EVERY) {
      if (this.prev !== null && this.count >= MIN_POINTS) {
        // healthy refresh: track this frame first so the new points start from the new image
        this.step(cur);
      }
      this.pts = detectCorners(cur[0]);
      this.count = this.pts.length / 2;
      this.sinceDetect = 0;
      this.prev = cur;
      this.state = { T: { ...this.T }, tracked: this.count, locked: this.count >= MIN_INLIERS };
      return this.state;
    }
    this.step(cur);
    this.prev = cur;
    return this.state;
  }

  private step(cur: Gray[]): void {
    const prev = this.prev!;
    const n = this.count;
    const src = new Float32Array(n * 2);
    const dst = new Float32Array(n * 2);
    let m = 0;
    for (let k = 0; k < n; k++) {
      const x = this.pts[2 * k];
      const y = this.pts[2 * k + 1];
      const fwd = lk(prev, cur, x, y);
      if (!fwd) continue;
      const back = lk(cur, prev, fwd[0], fwd[1]);
      if (!back) continue;
      if (Math.hypot(back[0] - x, back[1] - y) > FB_TOL) continue;
      src[2 * m] = x;
      src[2 * m + 1] = y;
      dst[2 * m] = fwd[0];
      dst[2 * m + 1] = fwd[1];
      m++;
    }
    this.sinceDetect++;
    const fit = m >= MIN_INLIERS ? ransacSimilarity(src, dst, m) : null;
    if (!fit) {
      // lost this frame: freeze, keep the surviving points, and re-detect soon
      this.lostFrames++;
      this.count = m;
      this.pts = dst.slice(0, 2 * m);
      if (this.lostFrames > 3) this.count = 0; // forces a re-detect next frame
      this.state = { T: { ...this.T }, tracked: m, locked: false };
      return;
    }
    this.lostFrames = 0;
    this.T = compose(fit.T, this.T);
    // keep only the inliers, at their new positions
    const keep = fit.inliers;
    const pts = new Float32Array(keep.length * 2);
    for (let k = 0; k < keep.length; k++) {
      pts[2 * k] = dst[2 * keep[k]];
      pts[2 * k + 1] = dst[2 * keep[k] + 1];
    }
    this.pts = pts;
    this.count = keep.length;
    this.state = { T: { ...this.T }, tracked: keep.length, locked: true };
  }
}

/** Frame F → now, given the transform remembered when F was captured. */
export function relative(now: Similarity, atCapture: Similarity): Similarity {
  return compose(now, invert(atCapture));
}
