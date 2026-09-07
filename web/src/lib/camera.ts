// Camera plumbing for the panel: which cameras exist, opening one (with the fallbacks a real
// desk full of webcams needs), remembering the choice, grabbing frames for the model and for the
// tracker, recording a clip, and a synthetic "demo board" stream for machines with no camera
// (or a camera another app is holding) so the AR layer can still be tried.
import { api } from "./api";
import type { Gray } from "./track";

export const DEMO_ID = "demo-pattern";
export const DEMO_LABEL = "Demo board (no camera needed)";

export interface CamPrefs {
  deviceId: string; // the browser's id for the remembered camera ("" = any)
  device: string; // its label — stable across sessions, ids are not
  mirror: boolean;
  clipSeconds: number;
  attachView: boolean;
}

const PREFS_KEY = "helix_camera_prefs";

export function loadPrefs(): CamPrefs {
  const base: CamPrefs = { deviceId: "", device: "", mirror: false, clipSeconds: 6, attachView: true };
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) Object.assign(base, JSON.parse(raw));
    const legacy = localStorage.getItem("helix_camera"); // the old modal's remembered id
    if (legacy && !base.deviceId) base.deviceId = legacy;
  } catch {
    /* storage unavailable — defaults */
  }
  return base;
}

/** Keep the choice locally (instant next time) AND in HELIX's settings (the Settings page shows it). */
export function savePrefs(p: CamPrefs, toServer = true): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
    localStorage.setItem("helix_camera_attach", p.attachView ? "1" : "0");
  } catch {
    /* fine */
  }
  if (toServer) {
    void api
      .put("/api/settings", {
        values: {
          camera_device: p.device,
          camera_mirror: p.mirror,
          camera_clip_seconds: p.clipSeconds,
          camera_attach_view: p.attachView,
        },
      })
      .catch(() => undefined);
  }
}

/** Server-side preferences (the Settings page may have changed them) folded over the local ones. */
export async function syncPrefs(local: CamPrefs): Promise<CamPrefs> {
  try {
    const d = await api.get<{ values: Record<string, unknown> }>("/api/settings");
    const v = d.values || {};
    const merged: CamPrefs = { ...local };
    if (typeof v.camera_mirror === "boolean") merged.mirror = v.camera_mirror;
    if (typeof v.camera_attach_view === "boolean") merged.attachView = v.camera_attach_view;
    if (typeof v.camera_clip_seconds === "number") merged.clipSeconds = v.camera_clip_seconds;
    if (typeof v.camera_device === "string" && v.camera_device && v.camera_device !== local.device) {
      merged.device = v.camera_device;
      merged.deviceId = ""; // resolve by label on open
    }
    return merged;
  } catch {
    return local;
  }
}

export async function listCameras(): Promise<MediaDeviceInfo[]> {
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === "videoinput");
  } catch {
    return [];
  }
}

export function reason(e: unknown): string {
  const name = (e as DOMException)?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Camera access is blocked. Allow the camera for HELIX (the address-bar camera icon, or Windows Settings → Privacy → Camera), then Retry.";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "No camera found. Plug one in or pick another, then Retry — or use the demo board.";
  if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError")
    return "The camera is in use by another app (Zoom, Teams, another tab). Close it, then Retry.";
  if (!navigator.mediaDevices?.getUserMedia)
    return "This window can't use cameras (no secure context). Open HELIX at http://127.0.0.1 or localhost.";
  return "The camera wouldn't start. Check it's connected and allowed, then Retry.";
}

export interface Opened {
  stream: MediaStream;
  deviceId: string;
  label: string;
  demo: boolean;
  stop: () => void;
}

const HD: MediaTrackConstraints = { width: { ideal: 1920 }, height: { ideal: 1080 } };

/**
 * Open a camera. Order: the exact remembered id → a device whose label matches the remembered
 * name (ids change, names don't) → any camera. A busy device gets a couple of quiet retries;
 * permission and no-camera errors surface as-is (retrying those just spams).
 */
export async function openCamera(pick: { deviceId?: string; label?: string }): Promise<Opened> {
  if (pick.deviceId === DEMO_ID) return demoStream();
  const ask = (c: MediaTrackConstraints) => navigator.mediaDevices.getUserMedia({ video: c, audio: false });
  const tries: MediaTrackConstraints[] = [];
  if (pick.deviceId) tries.push({ ...HD, deviceId: { exact: pick.deviceId } });
  if (pick.label) {
    const byLabel = (await listCameras()).find((d) => d.label && d.label === pick.label);
    if (byLabel && byLabel.deviceId !== pick.deviceId) tries.push({ ...HD, deviceId: { exact: byLabel.deviceId } });
  }
  tries.push({ ...HD });
  let last: unknown = null;
  for (const c of tries) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const stream = await ask(c);
        const track = stream.getVideoTracks()[0];
        const settings = track?.getSettings?.() || {};
        const id = (settings.deviceId as string) || (typeof c.deviceId === "object" && c.deviceId && "exact" in c.deviceId ? String(c.deviceId.exact) : "");
        const label = track?.label || (await listCameras()).find((d) => d.deviceId === id)?.label || "Camera";
        return { stream, deviceId: id, label, demo: false, stop: () => stream.getTracks().forEach((t) => t.stop()) };
      } catch (e) {
        last = e;
        const name = (e as DOMException)?.name || "";
        if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError") {
          await new Promise((r) => setTimeout(r, 350));
          continue; // busy: retry this constraint
        }
        if (name === "NotAllowedError" || name === "SecurityError") throw e; // no point trying others
        break; // stale id / over-constrained: fall through to the next constraint
      }
    }
  }
  throw last ?? new Error("camera unavailable");
}

// ----- the demo board -----
/** A synthetic PCB that slowly drifts and turns, with a blinking LED: enough texture for the
 * tracker to lock on, enough "electronics" for the model to talk about. */
export function demoStream(): Opened {
  const W = 1280;
  const H = 720;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  const t0 = performance.now();
  let alive = true;

  const draw = () => {
    if (!alive) return;
    const t = (performance.now() - t0) / 1000;
    ctx.fillStyle = "#1c1f22";
    ctx.fillRect(0, 0, W, H);
    // desk grain
    ctx.fillStyle = "rgba(255,255,255,0.03)";
    for (let i = 0; i < 40; i++) ctx.fillRect((i * 97) % W, (i * 53) % H, 60, 2);
    ctx.save();
    ctx.translate(W / 2 + Math.sin(t * 0.35) * 40, H / 2 + Math.cos(t * 0.27) * 24);
    ctx.rotate(Math.sin(t * 0.2) * 0.06);
    const s = 1 + Math.sin(t * 0.15) * 0.05;
    ctx.scale(s, s);
    // board
    ctx.fillStyle = "#1f6b3a";
    ctx.strokeStyle = "#0f3d22";
    ctx.lineWidth = 3;
    roundRect(ctx, -300, -180, 600, 360, 14);
    ctx.fill();
    ctx.stroke();
    // traces
    ctx.strokeStyle = "#c9a640";
    ctx.lineWidth = 2;
    for (let i = 0; i < 9; i++) {
      ctx.beginPath();
      ctx.moveTo(-280, -150 + i * 36);
      ctx.lineTo(-120 + (i % 3) * 30, -150 + i * 36);
      ctx.lineTo(-100 + (i % 3) * 30, -130 + i * 36);
      ctx.lineTo(60, -130 + i * 36);
      ctx.stroke();
    }
    // ESP32-style module with a can
    ctx.fillStyle = "#c8ccd0";
    roundRect(ctx, -60, -150, 190, 130, 6);
    ctx.fill();
    ctx.fillStyle = "#2a2e33";
    ctx.font = "bold 18px Inter, sans-serif";
    ctx.fillText("ESP32-WROOM-32", -50, -80);
    ctx.fillStyle = "#3a3f45";
    ctx.fillRect(100, -140, 24, 110);
    // header pins with labels
    const labels = ["3V3", "EN", "36", "39", "34", "35", "32", "33", "25", "26", "27", "14", "12", "GND", "13", "5V"];
    ctx.font = "11px Inter, sans-serif";
    labels.forEach((l, i) => {
      const y = -160 + i * 21;
      ctx.fillStyle = "#e8e0a0";
      ctx.beginPath();
      ctx.arc(230, y, 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#101010";
      ctx.beginPath();
      ctx.arc(230, y, 2.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#f3f3f3";
      ctx.fillText(l, 244, y + 4);
    });
    // capacitors, a resistor row, a button, an LED that blinks
    ctx.fillStyle = "#2f3a7a";
    for (let i = 0; i < 4; i++) {
      ctx.beginPath();
      ctx.arc(-220 + i * 44, 120, 14, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = "#3b3b3b";
    for (let i = 0; i < 6; i++) ctx.fillRect(-40 + i * 26, 100, 16, 34);
    ctx.fillStyle = "#d4d4d4";
    ctx.fillRect(150, 90, 40, 40);
    ctx.fillStyle = "#222";
    ctx.beginPath();
    ctx.arc(170, 110, 11, 0, Math.PI * 2);
    ctx.fill();
    const on = Math.floor(t * 2) % 2 === 0;
    ctx.fillStyle = on ? "#ff4d4d" : "#5a1d1d";
    ctx.shadowColor = on ? "#ff4d4d" : "transparent";
    ctx.shadowBlur = on ? 24 : 0;
    ctx.beginPath();
    ctx.arc(-250, -120, 9, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = "#ffffff";
    ctx.font = "10px Inter, sans-serif";
    ctx.fillText("D1", -258, -100);
    ctx.fillText("R1  R2  R3  R4  R5  R6", -38, 148);
    ctx.fillText("C1", -228, 146);
    ctx.fillText("SW1", 150, 145);
    ctx.fillText("HELIX DEMO BOARD v1", -290, 168);
    ctx.restore();
  };
  const timer = window.setInterval(draw, 1000 / 24);
  draw();
  const stream = canvas.captureStream(24);
  return {
    stream,
    deviceId: DEMO_ID,
    label: DEMO_LABEL,
    demo: true,
    stop: () => {
      alive = false;
      window.clearInterval(timer);
      stream.getTracks().forEach((t) => t.stop());
    },
  };
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// ----- frames -----
let frameCounter = 0;
export function newFrameId(): string {
  frameCounter += 1;
  return `f${Date.now().toString(36)}-${frameCounter}`;
}

/** One un-mirrored frame straight from the stream (markings read correctly), as a JPEG blob. */
export function grabFrame(video: HTMLVideoElement, quality = 0.92): Promise<Blob | null> {
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return Promise.resolve(null);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas.getContext("2d")!.drawImage(video, 0, 0);
  return new Promise((resolve) => canvas.toBlob((b) => resolve(b), "image/jpeg", quality));
}

/** The tracker's view: a small grayscale copy of the current frame. */
export function grabGray(video: HTMLVideoElement, work: HTMLCanvasElement, width = 320): Gray | null {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return null;
  const w = width;
  const h = Math.max(16, Math.round((vh / vw) * width));
  if (work.width !== w || work.height !== h) {
    work.width = w;
    work.height = h;
  }
  const ctx = work.getContext("2d", { willReadFrequently: true })!;
  ctx.drawImage(video, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  const data = new Float32Array(w * h);
  for (let i = 0, j = 0; i < data.length; i++, j += 4) {
    data[i] = px[j] * 0.299 + px[j + 1] * 0.587 + px[j + 2] * 0.114;
  }
  return { w, h, data };
}

/** Record `frames` frames spread evenly over `seconds`, in time order. */
export async function recordClip(
  video: HTMLVideoElement, frames: number, seconds: number, onProgress?: (done: number) => void,
): Promise<Blob[]> {
  const n = Math.max(2, Math.min(8, Math.round(frames)));
  const span = Math.max(0.5, Math.min(15, seconds));
  const gap = (span * 1000) / (n - 1);
  const out: Blob[] = [];
  for (let i = 0; i < n; i++) {
    const b = await grabFrame(video, 0.88);
    if (b) out.push(b);
    onProgress?.(i + 1);
    if (i < n - 1) await new Promise((r) => setTimeout(r, gap));
  }
  return out;
}
