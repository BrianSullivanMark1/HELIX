// The camera PANEL — the live camera beside the conversation, and the AR surface.
//
// Docked next to the transcript or expanded to fill the window, it stays open while you work:
//   • pick a camera (and cycle them with ⇄), mirror the preview, go full-screen;
//   • Snap a still or record a Clip — with a question typed alongside, or bare (HELIX then
//     identifies the part);
//   • the model looks through it whenever the conversation needs eyes: a live panel answers a
//     look INSTANTLY (the backend's `camera.capture`), a held look shows a prompt banner and waits
//     for your click or "take the picture";
//   • a typed message carries the current view along (the 👁 chip) so "what's this pin?" just works;
//   • HELIX's callouts are drawn over the video and a chosen hologram is projected onto it — both
//     ride on the board through the tracker (lib/track.ts) as it moves;
//   • MEASURE mode (📐): calibrate on something of known size (a credit card, the HELIX marker),
//     then drag across a part for its length — shift-drag for a box — in real millimetres; the
//     scale is remembered with the tracker's frame so it stays right as the camera drifts, a
//     10 mm grid can be laid over the plane, and Send hands the labelled numbers to HELIX (or
//     answers a measurement HELIX asked for). Calibrated, a projected hologram lands at 1:1 and
//     its component layout is drawn as ghost pockets, so the real parts can be laid inside them.
//
// It NEVER closes itself on a camera error: a failure keeps the panel with a plain reason, Retry,
// the picker, and the demo board. Only ✕ (or HELIX, asked to) closes it.
import { useCallback, useEffect, useRef, useState } from "react";
import type * as THREE from "three";
import { api } from "../lib/api";
import {
  DEMO_ID, DEMO_LABEL, grabFrame, grabGray, listCameras, loadPrefs, newFrameId, openCamera,
  reason as explain, recordClip, savePrefs, syncPrefs, type CamPrefs, type Opened,
} from "../lib/camera";
import {
  PRESETS, UNCALIBRATED_HINT, boxMm, describe, fmtMm, lengthMm, makeCalibration, measurePayload,
  nextLabel, scaleLine, trueScaleSize, type Calibration, type Measurement, type Norm,
} from "../lib/measure";
import { drawMeasureLayer, drawOverlays } from "../lib/overlay";
import { layoutFromJson, useHelix, type CameraSession, type HologramLayout, type Shot } from "../lib/store";
import { IDENTITY, Tracker, angleOf, apply, relative, scaleOf, type Similarity } from "../lib/track";
import ArHologram, { type ArFrame, type Box, type Placement } from "./ArHologram";

const TRACK_MS = 45; // ~22 tracker updates a second
const SETTLE_MS = 700; // let exposure settle before an automatic first grab
const MIN_DRAG_PX = 4; // a shorter drag is a click, and a click measures nothing
const MAX_MEASUREMENTS = 40;

interface HologramRow {
  slug: string;
  name: string;
  stl: string;
  layout: HologramLayout | null;
}

/** Measure mode's state: the calibration, the measurements, the grid, and the calibration in
 * progress. Each anchored piece carries the tracker frame it was made in, so it rides the board. */
interface RulerState {
  on: boolean;
  cal: Calibration | null;
  items: Measurement[];
  grid: boolean;
  preset: string;
  customMm: string;
  calibrating: { mm: number; reference: string; first: { T: Similarity; a: Norm } | null } | null;
  hint: string; // one plain line — a refusal, an instruction, what was sent
}

const RULER_START: RulerState = {
  on: false, cal: null, items: [], grid: false, preset: PRESETS[0].key, customMm: "",
  calibrating: null, hint: "",
};

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

/** Keep the drag even when the pointer leaves the view. A pointer that is already gone (a
 * cancelled touch, a synthetic event) makes the browser throw — a drag is not worth a crash. */
function holdPointer(e: React.PointerEvent<HTMLElement>): void {
  try {
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  } catch {
    /* fine — the move/up handlers still run while the pointer stays over the panel */
  }
}

export default function CameraDock({ session, hidden = false }: { session: CameraSession; hidden?: boolean }) {
  const layout = useHelix((s) => s.cameraLayout);
  const captureOrder = useHelix((s) => s.captureOrder);
  const overlays = useHelix((s) => s.overlays);
  const hologram = useHelix((s) => s.hologram);
  const attachView = useHelix((s) => s.attachView);
  const busy = useHelix((s) => s.busy);
  const setStore = useHelix((s) => s.set);
  const ask = session.ask;
  const measureAsk = session.measure;

  const video = useRef<HTMLVideoElement>(null);
  const frameEl = useRef<HTMLDivElement>(null);
  const overlayCanvas = useRef<HTMLCanvasElement>(null);
  const work = useRef<HTMLCanvasElement | null>(null);
  const opened = useRef<Opened | null>(null);
  const tracker = useRef(new Tracker());
  const frameT = useRef(new Map<string, Similarity>());
  const arRef = useRef<ArFrame>({ T: { ...IDENTITY }, locked: false, tw: 320, th: 240 });
  const placeRef = useRef<Placement>({ x: 0.5, y: 0.5, size: 0.002, roll: 0, tiltX: 0, yaw: 0, T: { ...IDENTITY } });
  const boxRef = useRef<Box>({ w: 0, h: 0 });
  const handledRids = useRef(new Set<string>());
  const liveRef = useRef(false);
  const mountedRef = useRef(true);
  const pendingOrder = useRef<{ rid: string; frames: number; seconds: number } | null>(null);

  const [prefs, setPrefs] = useState<CamPrefs>(() => loadPrefs());
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [label, setLabel] = useState("");
  const [live, setLive] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("Waking the camera…");
  const [box, setBox] = useState<Box>({ w: 0, h: 0 });
  const [track, setTrack] = useState({ tracked: 0, locked: false });
  const [caption, setCaption] = useState("");
  const [recording, setRecording] = useState(0); // frames captured so far, 0 = idle
  const [holograms, setHolograms] = useState<HologramRow[]>([]);
  const [holoDims, setHoloDims] = useState<THREE.Vector3 | null>(null);
  const [sending, setSending] = useState(false);
  const [trueScale, setTrueScale] = useState(false); // the hologram sits at 1 mm = 1 mm
  const [ruler, setRuler] = useState<RulerState>(RULER_START);
  const rulerRef = useRef(ruler); // the drawing loop and pointer handlers read the latest without re-binding
  rulerRef.current = ruler;
  const draftRef = useRef<Measurement | null>(null); // the drag under way (drawn, not yet a row)
  const measureDrag = useRef<{ T: Similarity; a: Norm } | null>(null);
  const full = layout === "full";
  const mirror = prefs.mirror;

  // ----- geometry: where the video actually sits inside its container (object-fit: contain) -----
  const fitBox = useCallback(() => {
    const el = frameEl.current;
    const v = video.current;
    if (!el || !v) return;
    const cw = el.clientWidth;
    const ch = el.clientHeight;
    const vw = v.videoWidth || 16;
    const vh = v.videoHeight || 9;
    let w = cw;
    let h = (cw * vh) / vw;
    if (h > ch) {
      h = ch;
      w = (ch * vw) / vh;
    }
    const next = { w: Math.round(w), h: Math.round(h) };
    boxRef.current = next;
    setBox((old) => (old.w === next.w && old.h === next.h ? old : next));
  }, []);

  useEffect(() => {
    const el = frameEl.current;
    if (!el) return;
    const ro = new ResizeObserver(fitBox);
    ro.observe(el);
    fitBox();
    return () => ro.disconnect();
  }, [fitBox, full]);

  // ----- mirror-aware screen mapping -----
  const normToScreen = useCallback((nx: number, ny: number): [number, number] => {
    const b = boxRef.current;
    const x = (mirror ? 1 - nx : nx) * b.w;
    return [x, ny * b.h];
  }, [mirror]);

  const screenToNorm = useCallback((sx: number, sy: number): [number, number] => {
    const b = boxRef.current;
    const nx = b.w ? sx / b.w : 0.5;
    return [mirror ? 1 - nx : nx, b.h ? sy / b.h : 0.5];
  }, [mirror]);

  /** Frame F's normalized point → the screen, NOW. Unknown frames stay put. */
  const mapPoint = useCallback((frame: string, nx: number, ny: number): [number, number] => {
    const at = frameT.current.get(frame);
    if (!at) return normToScreen(nx, ny);
    const ar = arRef.current;
    const M = relative(ar.T, at);
    const [px, py] = apply(M, nx * ar.tw, ny * ar.th);
    return normToScreen(px / ar.tw, py / ar.th);
  }, [normToScreen]);

  const scaleFor = useCallback((frame: string): number => {
    const at = frameT.current.get(frame);
    return at ? scaleOf(relative(arRef.current.T, at)) : 1;
  }, []);

  /** A point normalized in the frame whose transform is `at` → the screen, NOW (the ruler's map). */
  const mapAt = useCallback((at: Similarity, nx: number, ny: number): [number, number] => {
    const ar = arRef.current;
    const M = relative(ar.T, at);
    const [px, py] = apply(M, nx * ar.tw, ny * ar.th);
    return normToScreen(px / ar.tw, py / ar.th);
  }, [normToScreen]);

  /** A point normalized in the LIVE frame → normalized in the frame `at` (the inverse). */
  const toFrame = useCallback((at: Similarity, nx: number, ny: number): [number, number] => {
    const ar = arRef.current;
    const M = relative(at, ar.T);
    const [px, py] = apply(M, nx * ar.tw, ny * ar.th);
    return [px / ar.tw, py / ar.th];
  }, []);

  /** Where a pointer event lands on the video, normalized (mirror-aware), or null off the video. */
  const pointerNorm = useCallback((e: { clientX: number; clientY: number }): Norm | null => {
    const el = frameEl.current;
    const b = boxRef.current;
    if (!el || !b.w || !b.h) return null;
    const r = el.getBoundingClientRect();
    const sx = e.clientX - r.left - (r.width - b.w) / 2;
    const sy = e.clientY - r.top - (r.height - b.h) / 2;
    const [nx, ny] = screenToNorm(sx, sy);
    return [clamp01(nx), clamp01(ny)];
  }, [screenToNorm]);

  // ----- the stream -----
  const stop = useCallback(() => {
    opened.current?.stop();
    opened.current = null;
    if (video.current) video.current.srcObject = null;
    liveRef.current = false;
    setLive(false);
  }, []);

  const reportLive = useCallback((on: boolean, lbl: string) => {
    void api.post(`/api/camera/${session.id}/live`, { on, label: lbl }).catch(() => undefined);
  }, [session.id]);

  /** A new stream restarts the tracker from a new BASE: a calibration made against the old one
   * would silently be wrong, so it goes — with a plain line saying why. */
  const resetRuler = useCallback(() => {
    draftRef.current = null;
    measureDrag.current = null;
    setTrueScale(false);
    setRuler((s) => (s.cal || s.items.length || s.calibrating
      ? { ...s, cal: null, items: [], calibrating: null, grid: false,
          hint: "New camera stream — the tracker restarted, so calibrate again before measuring." }
      : s));
  }, []);

  const open = useCallback(async (pick: { deviceId?: string; label?: string }) => {
    stop();
    setError("");
    setStatus("Waking the camera…");
    tracker.current.reset();
    resetRuler();
    try {
      const o = await openCamera(pick);
      if (!mountedRef.current) {
        o.stop();
        return;
      }
      opened.current = o;
      const v = video.current!;
      v.srcObject = o.stream;
      await v.play();
      setDeviceId(o.deviceId);
      setLabel(o.label);
      setStatus("");
      liveRef.current = true;
      setLive(true);
      fitBox();
      reportLive(true, o.label);
      if (!o.demo) {
        const next = { ...prefs, deviceId: o.deviceId, device: o.label };
        setPrefs(next);
        savePrefs(next);
      }
      setDevices(await listCameras());
    } catch (e) {
      setStatus("");
      setError(explain(e));
      reportLive(false, "");
      setDevices(await listCameras());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stop, fitBox, reportLive, resetRuler, session.id]);

  useEffect(() => {
    mountedRef.current = true;
    work.current = document.createElement("canvas");
    let cancelled = false;
    void (async () => {
      const merged = await syncPrefs(loadPrefs());
      if (cancelled) return;
      setPrefs(merged);
      savePrefs(merged, false);
      await open({ deviceId: merged.deviceId, label: merged.device });
    })();
    void api.get<{ holograms: { slug: string; name: string; stl: string; layout?: unknown }[] }>("/api/camera/holograms")
      .then((d) => setHolograms((d.holograms || []).map((h) => ({
        slug: h.slug, name: h.name, stl: h.stl, layout: layoutFromJson(h.layout),
      }))))
      .catch(() => undefined);
    return () => {
      cancelled = true;
      mountedRef.current = false;
      stop();
      // The panel is leaving; if the backend still thinks this session exists, tell it the
      // stream is gone (a closed session ignores this — its id no longer matches).
      if (useHelix.getState().camera?.id === session.id) reportLive(false, "");
      useHelix.getState().set({ cameraCapture: null });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ----- capture -----
  const capture = useCallback(async (opts?: { frames?: number; seconds?: number }): Promise<Shot | null> => {
    const v = video.current;
    if (!v || !liveRef.current || !v.videoWidth) return null;
    const frame = newFrameId();
    frameT.current.set(frame, tracker.current.snapshot());
    if (frameT.current.size > 16) frameT.current.delete(frameT.current.keys().next().value!);
    const frames = Math.max(1, Math.min(8, Math.round(opts?.frames ?? 1)));
    if (frames > 1) {
      setRecording(1);
      try {
        const blobs = await recordClip(v, frames, opts?.seconds || frames * 0.5, setRecording);
        return blobs.length ? { blobs, frame } : null;
      } finally {
        setRecording(0);
      }
    }
    const blob = await grabFrame(v);
    return blob ? { blobs: [blob], frame } : null;
  }, []);

  // Offer the capture to the rest of the face (the Console's "attach the view" on send).
  useEffect(() => {
    setStore({ cameraCapture: live ? capture : null });
  }, [live, capture, setStore]);

  const answerLook = useCallback(async (rid: string, frames: number, seconds: number) => {
    if (handledRids.current.has(rid)) return;
    handledRids.current.add(rid);
    const shot = await capture({ frames, seconds });
    if (!shot) {
      handledRids.current.delete(rid);
      return;
    }
    setSending(true);
    try {
      await api.sendFrames(session.id, shot.blobs, { rid, frame: shot.frame, seconds, mode: frames > 1 ? "clip" : "still" });
    } finally {
      setSending(false);
    }
  }, [capture, session.id]);

  // The backend's shutter: an instant look on a live panel, or the spoken "take the picture".
  useEffect(() => {
    if (!captureOrder) return;
    if (!liveRef.current) {
      pendingOrder.current = captureOrder;
      return;
    }
    void answerLook(captureOrder.rid, captureOrder.frames, captureOrder.seconds);
  }, [captureOrder, answerLook]);

  // A look that arrived with the panel (no wait): grab once the stream has settled. A held look
  // waits in the banner for a click or the spoken word. Orders that landed before we were live
  // fire now too.
  useEffect(() => {
    if (!live) return;
    if (pendingOrder.current) {
      const o = pendingOrder.current;
      pendingOrder.current = null;
      void answerLook(o.rid, o.frames, o.seconds);
    }
    if (ask && !ask.hold && !handledRids.current.has(ask.rid)) {
      const t = window.setTimeout(() => void answerLook(ask.rid, ask.frames, ask.seconds), SETTLE_MS);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [live, ask, answerLook]);

  const snap = useCallback(async (clip: boolean) => {
    const frames = clip ? Math.min(8, Math.max(3, Math.round(prefs.clipSeconds * 1.4))) : 1;
    const seconds = clip ? prefs.clipSeconds : 0;
    if (ask) {
      // A held look: this click IS the answer (a clip if it asked for one).
      await answerLook(ask.rid, clip ? Math.max(ask.frames, frames) : ask.frames, clip ? seconds : ask.seconds);
      return;
    }
    const shot = await capture({ frames, seconds });
    if (!shot) return;
    setSending(true);
    try {
      await api.sendFrames(session.id, shot.blobs, {
        caption: caption.trim(), frame: shot.frame, seconds, mode: clip ? "clip" : "still",
      });
      setCaption("");
    } finally {
      setSending(false);
    }
  }, [ask, answerLook, capture, caption, prefs.clipSeconds, session.id]);

  // ----- tracking + drawing loop -----
  useEffect(() => {
    let raf = 0;
    let lastTrack = 0;
    let lastUi = 0;
    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      const v = video.current;
      const c = overlayCanvas.current;
      if (!v || !c) return;
      if (liveRef.current && v.videoWidth && now - lastTrack >= TRACK_MS && work.current) {
        lastTrack = now;
        const gray = grabGray(v, work.current);
        if (gray) {
          const st = tracker.current.update(gray);
          arRef.current = { T: st.T, locked: st.locked, tw: gray.w, th: gray.h };
          if (now - lastUi > 400) {
            lastUi = now;
            setTrack({ tracked: st.tracked, locked: st.locked });
          }
        }
      }
      const b = boxRef.current;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      if (c.width !== Math.round(b.w * dpr) || c.height !== Math.round(b.h * dpr)) {
        c.width = Math.round(b.w * dpr);
        c.height = Math.round(b.h * dpr);
      }
      const ctx = c.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, b.w, b.h);
      const groups = useHelix.getState().overlays;
      if (groups.length) drawOverlays(ctx, groups, mapPoint, scaleFor, now);
      const r = rulerRef.current;
      if (r.on) {
        drawMeasureLayer(ctx, {
          cal: r.cal, items: r.items, draft: draftRef.current, grid: r.grid,
          pending: r.calibrating?.first ?? null,
        }, mapAt, toFrame, now);
      }
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [mapPoint, scaleFor, mapAt, toFrame]);

  // ----- the hologram: first placement + gestures -----
  /** Fold the tracker's motion since the anchor into the placement, so edits apply to NOW. */
  const reanchor = useCallback(() => {
    const p = placeRef.current;
    const ar = arRef.current;
    const M = relative(ar.T, p.T);
    const [px, py] = apply(M, p.x * ar.tw, p.y * ar.th);
    placeRef.current = {
      ...p, x: px / ar.tw, y: py / ar.th, size: p.size * scaleOf(M), roll: p.roll + angleOf(M),
      T: { ...ar.T },
    };
  }, []);

  /** True scale: 1 mm on the plate = 1 mm on the desk, from the calibration (the ↺ 1:1 button,
   * and where a hologram lands when the panel is already calibrated). */
  const snapTrueScale = useCallback((cal: Calibration | null = rulerRef.current.cal) => {
    if (!cal) return;
    reanchor();
    placeRef.current = { ...placeRef.current, size: trueScaleSize(cal, arRef.current.T) };
    setTrueScale(true);
  }, [reanchor]);

  const onHoloLoaded = useCallback((dims: THREE.Vector3) => {
    setHoloDims(dims);
    const cal = rulerRef.current.cal;
    const T = tracker.current.snapshot();
    const maxDim = Math.max(dims.x, dims.y, dims.z, 1);
    placeRef.current = {
      x: 0.5, y: 0.5, size: cal ? trueScaleSize(cal, T) : 0.32 / maxDim, roll: 0, tiltX: 0, yaw: 0, T,
    };
    setTrueScale(Boolean(cal));
  }, []);

  const drag = useRef<{ x: number; y: number; nx: number; ny: number; tilt: number; yaw: number; mode: "move" | "tilt" } | null>(null);

  // ----- measure mode: calibration clicks, measuring drags -----
  const measureDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = rulerRef.current;
    const v = video.current;
    const W = v?.videoWidth || 0;
    const H = v?.videoHeight || 0;
    const p = pointerNorm(e);
    if (!p) return;
    e.preventDefault();
    if (!liveRef.current || !W || !H) {
      setRuler((s) => ({ ...s, hint: "The camera isn't live yet — nothing to measure." }));
      return;
    }
    const T = tracker.current.snapshot();
    if (r.calibrating) {
      const c = r.calibrating;
      if (!c.first) {
        setRuler((s) => (s.calibrating
          ? { ...s, calibrating: { ...s.calibrating, first: { T, a: p } }, hint: "Now click the other end." }
          : s));
        return;
      }
      // The second click may come after the camera moved: read it in the first click's frame.
      const b = toFrame(c.first.T, p[0], p[1]);
      const cal = makeCalibration(c.first.a, b, c.mm, c.reference, c.first.T, W, H);
      if (!cal) {
        setRuler((s) => ({
          ...s, calibrating: { ...c, first: null },
          hint: "Those two clicks were on top of each other — click one end, then the other.",
        }));
        return;
      }
      setRuler((s) => ({ ...s, cal, calibrating: null, grid: s.grid, hint: "" }));
      if (hologram) snapTrueScale(cal);
      return;
    }
    if (!r.cal) {
      setRuler((s) => ({ ...s, hint: UNCALIBRATED_HINT })); // refused in words, never in pixels
      return;
    }
    holdPointer(e);
    measureDrag.current = { T, a: p };
    draftRef.current = null;
  };

  const measureMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = measureDrag.current;
    const r = rulerRef.current;
    if (!d || !r.cal) return;
    const p = pointerNorm(e);
    if (!p) return;
    const b = toFrame(d.T, p[0], p[1]);
    const asBox = e.shiftKey;
    const dims = asBox ? boxMm(r.cal, d.T, d.a, b) : { w: 0, h: 0 };
    draftRef.current = {
      id: "draft", kind: asBox ? "box" : "distance", label: "", T: d.T, a: d.a, b,
      mm: asBox ? 0 : lengthMm(r.cal, d.T, d.a, b), w: dims.w, h: dims.h,
    };
  };

  const measureUp = () => {
    const d = measureDrag.current;
    const draft = draftRef.current;
    measureDrag.current = null;
    draftRef.current = null;
    if (!d) return;
    if (!draft) return; // a click: nothing was dragged
    const [x1, y1] = mapAt(d.T, d.a[0], d.a[1]);
    const [x2, y2] = mapAt(d.T, draft.b[0], draft.b[1]);
    if (Math.hypot(x2 - x1, y2 - y1) < MIN_DRAG_PX) {
      setRuler((s) => ({ ...s, hint: "Drag across the part — a click alone measures nothing." }));
      return;
    }
    setRuler((s) => ({
      ...s, hint: "",
      items: [...s.items, { ...draft, id: newFrameId(), label: nextLabel(s.items) }].slice(-MAX_MEASUREMENTS),
    }));
  };

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (rulerRef.current.on) {
      measureDown(e);
      return;
    }
    if (!hologram) return;
    e.preventDefault();
    holdPointer(e);
    reanchor();
    const p = placeRef.current;
    drag.current = {
      x: e.clientX, y: e.clientY, nx: p.x, ny: p.y, tilt: p.tiltX, yaw: p.yaw,
      mode: e.shiftKey || e.button === 2 ? "tilt" : "move",
    };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (measureDrag.current) {
      measureMove(e);
      return;
    }
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (d.mode === "tilt") {
      placeRef.current = {
        ...placeRef.current,
        tiltX: Math.max(-1.4, Math.min(1.4, d.tilt + dy * 0.008)),
        yaw: d.yaw + (mirror ? -dx : dx) * 0.008,
      };
      return;
    }
    const b = boxRef.current;
    const [sx, sy] = normToScreen(d.nx, d.ny);
    const [nx, ny] = screenToNorm(sx + dx, sy + dy);
    if (b.w && b.h) placeRef.current = { ...placeRef.current, x: nx, y: ny };
  };
  const onPointerUp = () => {
    if (measureDrag.current) measureUp();
    drag.current = null;
  };
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!hologram) return;
    e.preventDefault();
    reanchor();
    const f = Math.pow(1.1, -e.deltaY / 100);
    placeRef.current = { ...placeRef.current, size: Math.max(1e-5, placeRef.current.size * f) };
    setTrueScale(false); // the user chose a size; ↺ 1:1 brings true scale back
  };
  const resetView = (iso: boolean) => {
    reanchor();
    placeRef.current = { ...placeRef.current, tiltX: iso ? 0.9 : 0, yaw: iso ? 0.6 : 0, roll: 0 };
  };

  // ----- measure mode: the panel's controls -----
  const toggleRuler = () => {
    draftRef.current = null;
    measureDrag.current = null;
    setRuler((s) => ({ ...s, on: !s.on, calibrating: null, hint: "" }));
  };
  const startCalibration = () => {
    const r = rulerRef.current;
    const preset = PRESETS.find((p) => p.key === r.preset) || PRESETS[0];
    const mm = preset.key === "custom" ? Number(r.customMm) : preset.mm;
    if (!(mm > 0) || !Number.isFinite(mm)) {
      setRuler((s) => ({ ...s, hint: "Type the custom length in millimetres first." }));
      return;
    }
    const reference = preset.key === "custom" ? `custom ${fmtMm(mm)} mm` : preset.reference;
    const thing = preset.key === "custom" ? `the ${fmtMm(mm)} mm length` : `the ${preset.reference}`;
    setRuler((s) => ({
      ...s, calibrating: { mm, reference, first: null },
      hint: `Click one end of ${thing}, then the other. It should lie in the same plane as the parts.`,
    }));
  };
  const stopCalibration = () => setRuler((s) => ({ ...s, calibrating: null, hint: "" }));
  const renameItem = (id: string, text: string) =>
    setRuler((s) => ({ ...s, items: s.items.map((m) => (m.id === id ? { ...m, label: text.slice(0, 40) } : m)) }));
  const removeItem = (id: string) => setRuler((s) => ({ ...s, items: s.items.filter((m) => m.id !== id) }));
  const clearItems = () => setRuler((s) => ({ ...s, items: [], hint: "" }));

  const sendMeasure = async () => {
    const r = rulerRef.current;
    if (!r.cal || !r.items.length) return;
    setSending(true);
    try {
      const out = await api.post<{ ok: boolean; line: string }>(
        `/api/camera/${session.id}/measure`, measurePayload(r.cal, r.items),
      );
      setRuler((s) => ({ ...s, hint: out.ok ? `Sent — ${out.line}` : "HELIX couldn't read that measurement." }));
    } catch {
      setRuler((s) => ({ ...s, hint: "Couldn't reach HELIX to send that — try again." }));
    } finally {
      setSending(false);
    }
  };
  const cancelMeasureAsk = () =>
    void api.post(`/api/camera/${session.id}/measure`, { cancel: true }).catch(() => undefined);

  // HELIX asked for a measurement (camera_measure, or a reloaded page with one parked): Measure
  // mode comes on with the prompt in its banner. The user's own toggle still works afterwards.
  useEffect(() => {
    if (measureAsk) setRuler((s) => (s.on ? s : { ...s, on: true, hint: "" }));
  }, [measureAsk]);

  // ----- picker / toggles -----
  const pickDevice = (id: string) => {
    handledRids.current.clear();
    const d = devices.find((x) => x.deviceId === id);
    void open({ deviceId: id, label: id === DEMO_ID ? DEMO_LABEL : d?.label });
  };
  const cycle = () => {
    const ids = [...devices.map((d) => d.deviceId), DEMO_ID];
    if (!ids.length) return;
    const i = ids.indexOf(deviceId);
    pickDevice(ids[(i + 1) % ids.length]);
  };
  const setPref = (patch: Partial<CamPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    savePrefs(next);
    if (patch.attachView !== undefined) setStore({ attachView: patch.attachView });
  };

  const close = () => void api.post(`/api/camera/${session.id}/cancel`).catch(() => undefined);
  const dismissAsk = () => void api.post(`/api/camera/${session.id}/cancel`, { keep_open: true }).catch(() => undefined);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const cam = useHelix.getState().camera;
      if (cam?.ask) {
        dismissAsk();
        return;
      }
      if (rulerRef.current.calibrating) {
        setRuler((s) => ({ ...s, calibrating: null, hint: "" }));
        return;
      }
      if (cam?.measure) cancelMeasureAsk();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id]);

  // ----- render -----
  const wake = session.wake || "HELIX";
  const rulerHint = ruler.hint
    || (ruler.calibrating
      ? (ruler.calibrating.first ? "Now click the other end." : "Click one end of the reference, then the other.")
      : ruler.cal
        ? "Drag across a part for its length, hold Shift for a box (w × h); name each row, then Send."
        : UNCALIBRATED_HINT);
  const hint = error
    ? ""
    : ruler.on
      ? rulerHint
      : ask
        ? ask.hold
          ? ask.ears
            ? `Say “take the picture” (or “cancel”), or use the buttons.`
            : `Take the picture when it's ready — Esc cancels this look.`
          : sending
            ? "Sending what I see…"
            : "Grabbing what the camera sees…"
        : live
          ? `Snap or record a clip, type a question first if you like — or just talk: “${wake}, what's this?”`
          : status;

  const shellStyle: React.CSSProperties = full
    ? { position: "fixed", left: 16, right: 16, top: 52, bottom: 152, zIndex: 26 }
    : { position: "fixed", right: 18, top: 58, width: 440, bottom: 128, zIndex: 26, maxWidth: "calc(100vw - 36px)" };
  const cursor = ruler.on ? "crosshair" : hologram ? (drag.current ? "grabbing" : "grab") : "default";
  const ghostCount = hologram?.layout?.components.length ?? 0;

  return (
    <div
      className="glass-hi rounded-2xl fade-up flex flex-col camera-dock"
      style={{
        ...shellStyle, padding: 12, gap: 8,
        // off the Console: kept alive but invisible and inert (visibility keeps its geometry)
        visibility: hidden ? "hidden" : "visible", pointerEvents: hidden ? "none" : "auto",
      }}
    >
      {/* header */}
      <div className="flex items-center gap-2">
        <span className="font-display text-[13px]" style={{ color: "var(--cyan)" }}>CAMERA</span>
        <span
          className="text-[11px]"
          title={track.locked ? `Tracking ${track.tracked} points` : "Tracking paused — hold still or add light"}
          style={{ color: live ? (track.locked ? "var(--done)" : "var(--working)") : "var(--muted)" }}
        >
          ● {live ? (track.locked ? "tracking" : "settling") : "off"}
        </span>
        <div className="flex-1" />
        <select
          className="text-[12px] py-1 px-2 elide"
          style={{ maxWidth: full ? 320 : 150 }}
          value={deviceId || ""}
          onChange={(e) => pickDevice(e.target.value)}
          title="Which camera"
        >
          {!deviceId && <option value="">Choose a camera…</option>}
          {devices.map((d, i) => (
            <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Camera ${i + 1}`}</option>
          ))}
          <option value={DEMO_ID}>{DEMO_LABEL}</option>
        </select>
        <button className="btn-nav px-2" title="Next camera" onClick={cycle}>⇄</button>
        <button
          className="btn-nav px-2"
          title={ruler.on ? "Measure mode is on — back to the plain view" : "Measure: calibrate on a card, then drag across parts for real millimetres"}
          style={{ color: ruler.on ? "var(--cyan)" : undefined }}
          onClick={toggleRuler}
        >
          📐
        </button>
        <button
          className="btn-nav px-2"
          title={mirror ? "Mirrored preview (on)" : "Mirror the preview"}
          style={{ color: mirror ? "var(--cyan)" : undefined }}
          onClick={() => setPref({ mirror: !mirror })}
        >
          ⇋
        </button>
        <button
          className="btn-nav px-2"
          title={full ? "Dock beside the conversation" : "Full screen (AR)"}
          onClick={() => setStore({ cameraLayout: full ? "dock" : "full" })}
        >
          {full ? "⤡" : "⤢"}
        </button>
        <button className="btn-nav px-2" title="Close the camera" onClick={close}>✕</button>
      </div>

      {/* the view */}
      <div
        ref={frameEl}
        className="relative flex-1 rounded-xl overflow-hidden flex items-center justify-center"
        style={{ background: "#05080b", border: "1px solid #1b2730", minHeight: 120, cursor }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => hologram && e.preventDefault()}
        onDoubleClick={() => hologram && !ruler.on && resetView(false)}
      >
        <video
          ref={video}
          muted
          playsInline
          autoPlay
          onLoadedMetadata={fitBox}
          style={{
            width: box.w || "100%", height: box.h || "100%", objectFit: "contain",
            transform: mirror ? "scaleX(-1)" : undefined, display: "block",
          }}
        />
        <div
          className="absolute"
          style={{ left: `calc(50% - ${box.w / 2}px)`, top: `calc(50% - ${box.h / 2}px)`, width: box.w, height: box.h, pointerEvents: "none" }}
        >
          {hologram && box.w > 0 && (
            <ArHologram
              stl={hologram.stl} layout={hologram.layout} arRef={arRef} placeRef={placeRef} boxRef={boxRef} box={box}
              mirror={mirror} onLoaded={onHoloLoaded}
            />
          )}
          <canvas ref={overlayCanvas} style={{ position: "absolute", inset: 0, width: box.w, height: box.h, pointerEvents: "none" }} />
        </div>

        {/* the model's prompt banners: a look it is waiting for, a measurement it asked for */}
        {(ask || measureAsk) && (
          <div className="absolute left-3 right-3 top-3 flex flex-col gap-2" style={{ pointerEvents: "none" }}>
            {ask && (
              <div
                className="rounded-xl px-3 py-2 text-[13px] fade-up"
                style={{ background: "rgba(5,8,11,0.86)", border: "1px solid var(--cyan-dim)", color: "var(--text)", pointerEvents: "auto" }}
              >
                <span style={{ color: "var(--cyan)" }}>HELIX: </span>{ask.prompt}
                {ask.frames > 1 && <span style={{ color: "var(--muted)" }}> · a {ask.seconds}s clip, {ask.frames} frames</span>}
              </div>
            )}
            {measureAsk && (
              <div
                className="rounded-xl px-3 py-2 text-[13px] fade-up flex items-center gap-2"
                style={{ background: "rgba(5,8,11,0.86)", border: "1px solid var(--cyan-dim)", color: "var(--text)", pointerEvents: "auto" }}
              >
                <span style={{ color: "var(--cyan)" }}>HELIX · measure:</span>
                <span className="flex-1">{measureAsk.prompt}</span>
                <button className="btn-nav px-1.5 py-0 text-[13px]" onClick={cancelMeasureAsk} title="Cancel this measurement (Esc)">✕</button>
              </div>
            )}
          </div>
        )}

        {/* overlay titles */}
        {overlays.length > 0 && overlays[overlays.length - 1].title && (
          <div
            className="absolute left-3 bottom-3 rounded-lg px-2.5 py-1 text-[12px]"
            style={{ background: "rgba(5,8,11,0.8)", border: "1px solid var(--line)", color: "var(--cyan)", pointerEvents: "none" }}
          >
            ✎ {overlays[overlays.length - 1].title}
          </div>
        )}

        {recording > 0 && (
          <div
            className="absolute right-3 top-3 rounded-lg px-2.5 py-1 text-[12px]"
            style={{ background: "rgba(90,10,10,0.85)", border: "1px solid #ff5d62", color: "#ffd0d2", pointerEvents: "none" }}
          >
            ● REC {recording}
          </div>
        )}

        {ruler.on && recording === 0 && (
          <div
            className="absolute right-3 bottom-3 rounded-lg px-2.5 py-1 text-[12px]"
            style={{ background: "rgba(5,8,11,0.8)", border: `1px solid ${ruler.cal ? "var(--done)" : "var(--line)"}`, color: ruler.cal ? "var(--done)" : "var(--muted)", pointerEvents: "none" }}
            title={scaleLine(ruler.cal)}
          >
            📐 {ruler.cal ? scaleLine(ruler.cal) : "not calibrated"}
          </div>
        )}

        {(error || (!live && status)) && (
          <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-[13px]" style={{ color: error ? "var(--amber)" : "var(--muted)", pointerEvents: "none" }}>
            {error || status}
          </div>
        )}
      </div>

      {/* measure mode: calibration, the grid, the list, Send */}
      {ruler.on && (
        <div className="flex flex-col gap-1.5 text-[12px]" style={{ color: "var(--muted)" }}>
          {/* the reference and the two-click calibration */}
          <div className="flex items-center gap-2">
            <select
              className="flex-1 text-[12px] py-1 px-2 elide"
              style={{ minWidth: 140, maxWidth: 360 }}
              value={ruler.preset}
              disabled={Boolean(ruler.calibrating)}
              onChange={(e) => setRuler((s) => ({ ...s, preset: e.target.value }))}
              title="What you will click across — something of known size, lying in the same plane as the parts"
            >
              {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
            </select>
            {ruler.preset === "custom" && (
              <input
                className="text-[12px] py-1 px-2 shrink-0"
                style={{ width: 72 }}
                placeholder="mm"
                inputMode="decimal"
                value={ruler.customMm}
                disabled={Boolean(ruler.calibrating)}
                onChange={(e) => setRuler((s) => ({ ...s, customMm: e.target.value }))}
                title="The known length, in millimetres"
              />
            )}
            {ruler.calibrating ? (
              <button className="btn text-[12px] shrink-0" onClick={stopCalibration} title="Stop calibrating (Esc)">✕ stop</button>
            ) : (
              <button className="btn btn-primary text-[12px] shrink-0" disabled={!live} onClick={startCalibration} title="Two clicks across the reference set the scale">
                {ruler.cal ? "Recalibrate" : "Calibrate"}
              </button>
            )}
          </div>
          {/* the grid, and Send */}
          <div className="flex items-center gap-2">
            <button
              className="btn-nav px-2 py-1 shrink-0"
              disabled={!ruler.cal}
              style={{ color: ruler.grid ? "var(--cyan)" : undefined, whiteSpace: "nowrap" }}
              onClick={() => setRuler((s) => ({ ...s, grid: !s.grid }))}
              title={ruler.cal ? "A 10 mm grid over the calibrated plane (it rides the board)" : "Calibrate first"}
            >
              ⊞ 10 mm grid
            </button>
            <span className="elide flex-1" title={scaleLine(ruler.cal)}>{scaleLine(ruler.cal)}</span>
            <button
              className="btn btn-primary text-[12px] shrink-0"
              style={{ whiteSpace: "nowrap" }}
              disabled={!ruler.cal || !ruler.items.length || sending}
              onClick={() => void sendMeasure()}
              title={measureAsk ? "Answer HELIX with these measurements" : "Send these measurements to HELIX"}
            >
              Send to HELIX{ruler.items.length ? ` (${ruler.items.length})` : ""}
            </button>
          </div>
          {ruler.items.length > 0 && (
            <div className="flex flex-col gap-1" style={{ maxHeight: full ? 160 : 92, overflowY: "auto" }}>
              {ruler.items.map((m) => (
                <div key={m.id} className="flex items-center gap-2">
                  <input
                    className="text-[12px] py-0.5 px-2"
                    style={{ width: 132 }}
                    value={m.label}
                    onChange={(e) => renameItem(m.id, e.target.value)}
                    title="Name this measurement — which part, which feature"
                  />
                  <span style={{ color: "var(--text)" }}>{describe(m)}</span>
                  <span className="elide flex-1">{m.kind === "box" ? "box" : "length"}</span>
                  <button className="btn-nav px-1.5 py-0" onClick={() => removeItem(m.id)} title="Remove this measurement">✕</button>
                </div>
              ))}
              {ruler.items.length > 1 && (
                <div className="flex items-center">
                  <div className="flex-1" />
                  <button className="btn-nav px-1.5 py-0" onClick={clearItems} title="Remove every measurement">clear all</button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* hologram bar */}
      {hologram && (
        <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
          <span className="elide" style={{ color: "var(--cyan)" }}>◈ {hologram.name}</span>
          {holoDims && <span>{Math.round(holoDims.x)}×{Math.round(holoDims.y)}×{Math.round(holoDims.z)} mm</span>}
          {trueScale && (
            <span
              className="shrink-0"
              style={{ color: "var(--done)", border: "1px solid var(--done)", borderRadius: 6, padding: "0 5px", fontSize: 11 }}
              title="True scale: 1 mm on the plate is 1 mm on the desk (from the calibration)"
            >
              1:1
            </span>
          )}
          {ghostCount > 0 && (
            <span className="shrink-0" title="The design's component layout, drawn as ghost pockets inside the shell">
              {ghostCount} ghost{ghostCount === 1 ? "" : "s"}
            </span>
          )}
          <span className="elide flex-1">
            {ruler.on
              ? "measure mode — turn 📐 off to move the hologram"
              : "drag to move · scroll to size · shift-drag to tilt · double-click resets"}
          </span>
          <button
            className="btn-nav px-1.5"
            disabled={!ruler.cal}
            onClick={() => snapTrueScale()}
            title={ruler.cal ? "Snap the hologram to true scale (1 mm = 1 mm)" : "Calibrate in Measure mode to place it at true scale"}
          >
            ↺ 1:1
          </button>
          <button className="btn-nav px-1.5" onClick={() => resetView(false)} title="Top view">▭</button>
          <button className="btn-nav px-1.5" onClick={() => resetView(true)} title="Isometric">◇</button>
          <button className="btn-nav px-1.5" onClick={() => setStore({ hologram: null })} title="Remove the hologram">✕</button>
        </div>
      )}

      {/* hint / error */}
      {(hint || error) && (
        <div className="text-[12px] elide" style={{ color: error ? "var(--amber)" : "var(--muted)" }} title={hint || error}>
          {error ? error : hint}
        </div>
      )}

      {/* controls */}
      <div className="flex items-center gap-2">
        {error ? (
          <>
            <button className="btn btn-primary text-[13px]" onClick={() => void open({ deviceId, label })}>Retry</button>
            <button className="btn text-[13px]" onClick={() => pickDevice(DEMO_ID)}>Use the demo board</button>
            <div className="flex-1" />
          </>
        ) : (
          <>
            <input
              className="flex-1 text-[13px] py-2"
              placeholder={ask ? "(answering HELIX's look)" : "Ask about this shot… (optional)"}
              value={caption}
              disabled={Boolean(ask)}
              onChange={(e) => setCaption(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void snap(false);
                }
              }}
            />
            <button
              className="btn btn-primary text-[13px] shrink-0"
              disabled={!live || recording > 0 || sending}
              onClick={() => void snap(false)}
              title={ask ? "Take the picture HELIX asked for" : "Snap a still"}
            >
              {ask ? "Take the picture" : "📷 Snap"}
            </button>
            <button
              className="btn text-[13px] shrink-0"
              disabled={!live || recording > 0 || sending}
              onClick={() => void snap(true)}
              title={`Record a ${prefs.clipSeconds}s clip (frames HELIX reads in order)`}
            >
              ⏺ Clip {prefs.clipSeconds}s
            </button>
            {ask && (
              <button className="btn-nav px-2 text-[13px]" onClick={dismissAsk} title="Cancel this look (Esc)">✕</button>
            )}
          </>
        )}
      </div>

      {/* second row: attach-view chip, hologram picker, clear */}
      <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
        <button
          className="btn-nav px-2 py-1"
          style={{ color: attachView ? "var(--cyan)" : "var(--muted)" }}
          title="Typed messages carry the current view while the camera is open"
          onClick={() => setPref({ attachView: !attachView })}
        >
          👁 {attachView ? "view rides with messages" : "messages go without the view"}
        </button>
        <div className="flex-1" />
        {holograms.length > 0 && (
          <select
            className="text-[12px] py-1 px-2"
            style={{ maxWidth: 200 }}
            value={hologram?.slug || ""}
            onChange={(e) => {
              const row = holograms.find((h) => h.slug === e.target.value);
              setStore({ hologram: row ? { slug: row.slug, name: row.name, stl: row.stl, layout: row.layout } : null });
            }}
            title="Project one of your holograms onto the view"
          >
            <option value="">Project a hologram…</option>
            {holograms.map((h) => <option key={h.slug} value={h.slug}>{h.name}{h.layout ? " ◫" : ""}</option>)}
          </select>
        )}
        {overlays.length > 0 && (
          <button className="btn-nav px-2 py-1" onClick={() => setStore({ overlays: [] })} title="Clear HELIX's callouts">
            ✎ clear
          </button>
        )}
        {busy && <span style={{ color: "var(--working)" }}>thinking…</span>}
      </div>
    </div>
  );
}
