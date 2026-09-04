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
//     ride on the board through the tracker (lib/track.ts) as it moves.
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
import { drawOverlays } from "../lib/overlay";
import { useHelix, type CameraSession, type Shot } from "../lib/store";
import { IDENTITY, Tracker, angleOf, apply, relative, scaleOf, type Similarity } from "../lib/track";
import ArHologram, { type ArFrame, type Box, type Placement } from "./ArHologram";

const TRACK_MS = 45; // ~22 tracker updates a second
const SETTLE_MS = 700; // let exposure settle before an automatic first grab

interface HologramRow {
  slug: string;
  name: string;
  stl: string;
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
  const full = layout === "full";
  const mirror = prefs.mirror;

  // ----- geometry: where the video actually sits inside its container (object-fit: contain) -----
  const measure = useCallback(() => {
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
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, [measure, full]);

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
    const ar = arRef.current;
    const at = frameT.current.get(frame);
    if (!at) return normToScreen(nx, ny);
    const M = relative(ar.T, at);
    const [px, py] = apply(M, nx * ar.tw, ny * ar.th);
    return normToScreen(px / ar.tw, py / ar.th);
  }, [normToScreen]);

  const scaleFor = useCallback((frame: string): number => {
    const at = frameT.current.get(frame);
    return at ? scaleOf(relative(arRef.current.T, at)) : 1;
  }, []);

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

  const open = useCallback(async (pick: { deviceId?: string; label?: string }) => {
    stop();
    setError("");
    setStatus("Waking the camera…");
    tracker.current.reset();
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
      measure();
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
  }, [stop, measure, reportLive, session.id]);

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
    void api.get<{ holograms: HologramRow[] }>("/api/camera/holograms")
      .then((d) => setHolograms(d.holograms || []))
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
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [mapPoint, scaleFor]);

  // ----- the hologram: first placement + gestures -----
  const onHoloLoaded = useCallback((dims: THREE.Vector3) => {
    setHoloDims(dims);
    const maxDim = Math.max(dims.x, dims.y, dims.z, 1);
    placeRef.current = {
      x: 0.5, y: 0.5, size: 0.32 / maxDim, roll: 0, tiltX: 0, yaw: 0, T: tracker.current.snapshot(),
    };
  }, []);

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

  const drag = useRef<{ x: number; y: number; nx: number; ny: number; tilt: number; yaw: number; mode: "move" | "tilt" } | null>(null);
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!hologram) return;
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    reanchor();
    const p = placeRef.current;
    drag.current = {
      x: e.clientX, y: e.clientY, nx: p.x, ny: p.y, tilt: p.tiltX, yaw: p.yaw,
      mode: e.shiftKey || e.button === 2 ? "tilt" : "move",
    };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
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
    drag.current = null;
  };
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!hologram) return;
    e.preventDefault();
    reanchor();
    const f = Math.pow(1.1, -e.deltaY / 100);
    placeRef.current = { ...placeRef.current, size: Math.max(1e-5, placeRef.current.size * f) };
  };
  const resetView = (iso: boolean) => {
    reanchor();
    placeRef.current = { ...placeRef.current, tiltX: iso ? 0.9 : 0, yaw: iso ? 0.6 : 0, roll: 0 };
  };

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
      if (e.key === "Escape" && useHelix.getState().camera?.ask) dismissAsk();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id]);

  // ----- render -----
  const wake = session.wake || "HELIX";
  const hint = error
    ? ""
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
          style={{ maxWidth: full ? 320 : 190 }}
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
        style={{ background: "#05080b", border: "1px solid #1b2730", minHeight: 120, cursor: hologram ? (drag.current ? "grabbing" : "grab") : "default" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => hologram && e.preventDefault()}
        onDoubleClick={() => hologram && resetView(false)}
      >
        <video
          ref={video}
          muted
          playsInline
          autoPlay
          onLoadedMetadata={measure}
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
              stl={hologram.stl} arRef={arRef} placeRef={placeRef} boxRef={boxRef} box={box}
              mirror={mirror} onLoaded={onHoloLoaded}
            />
          )}
          <canvas ref={overlayCanvas} style={{ position: "absolute", inset: 0, width: box.w, height: box.h, pointerEvents: "none" }} />
        </div>

        {/* the model's prompt banner */}
        {ask && (
          <div
            className="absolute left-3 right-3 top-3 rounded-xl px-3 py-2 text-[13px] fade-up"
            style={{ background: "rgba(5,8,11,0.86)", border: "1px solid var(--cyan-dim)", color: "var(--text)", pointerEvents: "auto" }}
          >
            <span style={{ color: "var(--cyan)" }}>HELIX: </span>{ask.prompt}
            {ask.frames > 1 && <span style={{ color: "var(--muted)" }}> · a {ask.seconds}s clip, {ask.frames} frames</span>}
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

        {(error || (!live && status)) && (
          <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-[13px]" style={{ color: error ? "var(--amber)" : "var(--muted)", pointerEvents: "none" }}>
            {error || status}
          </div>
        )}
      </div>

      {/* hologram bar */}
      {hologram && (
        <div className="flex items-center gap-2 text-[12px]" style={{ color: "var(--muted)" }}>
          <span className="elide" style={{ color: "var(--cyan)" }}>◈ {hologram.name}</span>
          {holoDims && <span>{Math.round(holoDims.x)}×{Math.round(holoDims.y)}×{Math.round(holoDims.z)} mm</span>}
          <span className="elide flex-1">drag to move · scroll to size · shift-drag to tilt · double-click resets</span>
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
              setStore({ hologram: row ? { slug: row.slug, name: row.name, stl: row.stl } : null });
            }}
            title="Project one of your holograms onto the view"
          >
            <option value="">Project a hologram…</option>
            {holograms.map((h) => <option key={h.slug} value={h.slug}>{h.name}</option>)}
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
