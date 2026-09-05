// The app's one store: everything the event stream drives, plus routing.
import { create } from "zustand";

export type OrbState = "idle" | "listening" | "transcribing" | "thinking" | "speaking";
export type Hue = "none" | "working" | "done" | "error";

export interface Visual {
  type: "table" | "chart" | "products";
  [k: string]: unknown;
}

// ----- the Amazon cart panel -----
export interface CartLine {
  label: string;
  title: string;
  asin: string;
  quantity: number;
  price: number | null;
  image: string;
  url: string;
  project: string;
  note: string;
}

export interface CartSnapshot {
  items: CartLine[];
  count: number;
  estimated_total: number | null;
  unpriced: number;
  driver: boolean; // HELIX can drive its own Chrome window (else the link fallback)
  opening: boolean; // a handoff is in flight
  last_handoff: { at: string; how: string; count: number; subtotal: string } | null;
}

export interface BubbleAction {
  id: string;
  label: string;
  style: "danger" | "plain" | "primary";
}

export interface Bubble {
  id: string;
  role: "user" | "helix" | "system";
  text: string;
  visuals: Visual[];
  sources: { line: string }[];
  actions: BubbleAction[];
  images: string[]; // served URLs (/api/images/<id>) — the photo you took, the frame HELIX saw
  used?: string; // the action label that was clicked (buttons collapse after use)
}

export interface VoiceState {
  supported: boolean;
  enabled: boolean;
  label: string;
  tone: "on" | "off" | "warn";
  idle_line: string;
  muted: boolean;
  listening: boolean;
  wake: string;
}

export interface LegendItem {
  slug: string;
  name: string;
  state: "building" | "done" | "error";
}

/** The nightly dream session (self-improvement while you sleep): running now, and one line about it. */
export interface DreamState {
  running: boolean;
  line: string;
}

export interface Attachment {
  id: string;
  name: string;
  image: boolean;
  preview?: string;
}

export type Page =
  | { name: "console" }
  | { name: "menu" }
  | { name: "settings" }
  | { name: "vault"; slug: string; title: string }
  | { name: "studio"; slug: string; title: string }
  | { name: "viewer"; slug: string; title: string; url: string; server?: boolean };

interface ConnectModal {
  service: string;
  label: string;
  reason: string;
  fields: { key: string; label: string; hint: string }[];
}

// ----- the camera panel -----
/** A look the model parked on the panel: wait for the user (hold) or grab at once. */
export interface CameraAsk {
  rid: string;
  prompt: string;
  hold: boolean;
  frames: number;
  seconds: number;
  ears: boolean; // the camera grammar is listening ("take the picture" / "cancel")
}

export interface CameraSession {
  id: string;
  prompt: string;
  ears: boolean;
  manual: boolean;
  ask: CameraAsk | null;
  wake: string;
}

/** "Capture now": the backend's shutter — the voice grammar, or an instant look on a live panel. */
export interface CaptureOrder {
  rid: string;
  frames: number;
  seconds: number;
  n: number; // bumps per order so an identical order still fires
}

export type OverlayKind = "box" | "circle" | "arrow" | "label" | "pin" | "wire";

export interface OverlayItem {
  kind: OverlayKind;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  r?: number;
  x2?: number;
  y2?: number;
  points?: number[][];
  text?: string;
  color?: string;
}

/** One drawing the model made, anchored to the frame it was looking at. */
export interface OverlayGroup {
  frame: string;
  title: string;
  items: OverlayItem[];
  n: number;
}

export interface HologramSpec {
  slug: string;
  name: string;
  stl: string;
}

export type CameraLayout = "dock" | "full";

export interface Shot {
  blobs: Blob[];
  frame: string; // the frame id (the first frame's, for a clip)
}

export type CaptureFn = (opts?: { frames?: number; seconds?: number }) => Promise<Shot | null>;

interface HelixStore {
  page: Page;
  authed: boolean;
  bubbles: Bubble[];
  status: string;
  idleLine: string;
  busy: boolean;
  orb: OrbState;
  hue: Hue;
  level: number;
  bands: number[];
  legend: LegendItem[];
  voice: VoiceState | null;
  attachments: Attachment[];
  suggestion: { id: string; text: string; slug: string | null } | null;
  connectModal: ConnectModal | null;
  camera: CameraSession | null;
  cameraLayout: CameraLayout;
  captureOrder: CaptureOrder | null;
  overlays: OverlayGroup[];
  hologram: HologramSpec | null;
  cameraCapture: CaptureFn | null; // registered by the live panel: "give me what you see"
  attachView: boolean; // a typed message while the panel is live carries the current view
  lightbox: string; // a transcript picture opened large
  buildsVersion: number; // bumps to refresh the menu
  keepInput: string;
  toast: string;
  cart: CartSnapshot | null; // the staged Amazon cart (null = nothing known yet)
  dream: DreamState | null; // the dream session (null = nothing known yet)

  navigate: (page: Page) => void;
  addBubble: (b: Bubble) => void;
  useAction: (bubbleId: string, label: string) => void;
  setAttachments: (a: Attachment[]) => void;
  set: (partial: Partial<HelixStore>) => void;
}

function readAttachView(): boolean {
  try {
    const v = localStorage.getItem("helix_camera_attach");
    return v === null ? true : v === "1";
  } catch {
    return true;
  }
}

export const useHelix = create<HelixStore>((set) => ({
  page: { name: "console" },
  authed: true,
  bubbles: [],
  status: "",
  idleLine: "Ready when you are.",
  busy: false,
  orb: "idle",
  hue: "none",
  level: 0,
  bands: new Array(16).fill(0),
  legend: [],
  voice: null,
  attachments: [],
  suggestion: null,
  connectModal: null,
  camera: null,
  cameraLayout: "dock",
  captureOrder: null,
  overlays: [],
  hologram: null,
  cart: null,
  dream: null,
  cameraCapture: null,
  attachView: readAttachView(),
  lightbox: "",
  buildsVersion: 0,
  keepInput: "",
  toast: "",

  navigate: (page) => set({ page }),
  addBubble: (b) =>
    set((s) => ({ bubbles: [...s.bubbles, b].slice(-250) })),
  useAction: (bubbleId, label) =>
    set((s) => ({
      bubbles: s.bubbles.map((b) =>
        b.id === bubbleId ? { ...b, used: label, actions: [] } : b,
      ),
    })),
  setAttachments: (attachments) => set({ attachments }),
  set: (partial) => set(partial),
}));

export function cameraFromEvent(ev: Record<string, unknown>): CameraSession {
  const ask = ev.ask as Record<string, unknown> | null | undefined;
  return {
    id: ev.id as string,
    prompt: (ev.prompt as string) || "",
    ears: Boolean(ev.ears),
    manual: Boolean(ev.manual),
    wake: (ev.wake as string) || "HELIX",
    ask: ask
      ? {
          rid: (ask.rid as string) || "",
          prompt: (ask.prompt as string) || "",
          hold: Boolean(ask.hold),
          frames: Number(ask.frames) || 1,
          seconds: Number(ask.seconds) || 0,
          ears: Boolean(ev.ears),
        }
      : null,
  };
}

// ----- the event-stream reducer -----
export function applyEvent(ev: Record<string, unknown>): void {
  const s = useHelix.getState();
  const t = ev.t as string;
  switch (t) {
    case "msg":
      s.addBubble({
        id: ev.id as string,
        role: ev.role as Bubble["role"],
        text: (ev.text as string) || "",
        visuals: (ev.visuals as Visual[]) || [],
        sources: (ev.sources as { line: string }[]) || [],
        actions: (ev.actions as BubbleAction[]) || [],
        images: (ev.images as string[]) || [],
      });
      break;
    case "status":
      s.set({ status: (ev.text as string) || "" });
      break;
    case "cart":
      s.set({ cart: (ev.cart as CartSnapshot) || null });
      break;
    case "dream":
      s.set({ dream: { running: Boolean(ev.running), line: (ev.line as string) || "" } });
      break;
    case "busy":
      s.set({ busy: Boolean(ev.on) });
      break;
    case "orb":
      s.set({ orb: ev.state as OrbState });
      break;
    case "hue":
      s.set({ hue: ev.value as Hue });
      break;
    case "level":
      s.set({ level: ev.v as number });
      break;
    case "bands":
      s.set({ bands: ev.v as number[] });
      break;
    case "legend":
      s.set({ legend: (ev.items as LegendItem[]) || [] });
      break;
    case "voice": {
      const voice = ev as unknown as VoiceState & { t: string };
      s.set({ voice, idleLine: voice.idle_line, status: s.busy ? s.status : voice.idle_line });
      break;
    }
    case "builds":
      s.set({ buildsVersion: s.buildsVersion + 1 });
      break;
    case "suggest":
      s.set({
        suggestion: {
          id: ev.id as string,
          text: ev.text as string,
          slug: (ev.slug as string) || null,
        },
      });
      break;
    case "suggest.clear":
      s.set({ suggestion: null });
      break;
    case "connect":
      s.set({
        connectModal: {
          service: ev.service as string,
          label: ev.label as string,
          reason: (ev.reason as string) || "",
          fields: (ev.fields as ConnectModal["fields"]) || [],
        },
      });
      break;
    case "camera": {
      // A fresh session: drawings and holograms anchored to the OLD panel's frames go with it.
      const same = s.camera?.id === ev.id;
      s.set({
        camera: cameraFromEvent(ev),
        captureOrder: same ? s.captureOrder : null,
        overlays: same ? s.overlays : [],
        hologram: same ? s.hologram : null,
      });
      break;
    }
    case "camera.ask":
      if (s.camera && s.camera.id === ev.id) {
        s.set({
          camera: {
            ...s.camera,
            ask: {
              rid: (ev.rid as string) || "",
              prompt: (ev.prompt as string) || "",
              hold: Boolean(ev.hold),
              frames: Number(ev.frames) || 1,
              seconds: Number(ev.seconds) || 0,
              ears: Boolean(ev.ears),
            },
          },
        });
      }
      break;
    case "camera.ask.clear":
      if (s.camera && s.camera.id === ev.id) s.set({ camera: { ...s.camera, ask: null } });
      break;
    case "camera.capture":
      s.set({
        captureOrder: {
          rid: (ev.rid as string) || "",
          frames: Number(ev.frames) || 1,
          seconds: Number(ev.seconds) || 0,
          n: (s.captureOrder?.n ?? 0) + 1,
        },
      });
      break;
    case "camera.close":
      if (!s.camera || s.camera.id === ev.id) {
        s.set({ camera: null, captureOrder: null, overlays: [], hologram: null, cameraCapture: null });
      }
      break;
    case "camera.overlay": {
      const items = (ev.items as OverlayItem[]) || [];
      const group: OverlayGroup = {
        frame: (ev.frame as string) || "",
        title: (ev.title as string) || "",
        items,
        n: (s.overlays[s.overlays.length - 1]?.n ?? 0) + 1,
      };
      const clear = ev.clear !== false;
      s.set({ overlays: items.length ? (clear ? [group] : [...s.overlays, group]) : clear ? [] : s.overlays });
      break;
    }
    case "camera.hologram":
      s.set({
        hologram: ev.remove
          ? null
          : { slug: ev.slug as string, name: (ev.name as string) || (ev.slug as string), stl: ev.stl as string },
      });
      break;
    case "camera.layout":
      s.set({ cameraLayout: ev.layout === "full" ? "full" : "dock" });
      break;
    case "identity":
      s.addBubble({
        id: `id-${Date.now()}`,
        role: "system",
        text: `“${ev.heard as string}” — ${ev.reply as string}`,
        visuals: [], sources: [], actions: [], images: [],
      });
      break;
    case "open":
      // The model asked to open a build — the Menu logic owns resolution; nudge via the console page.
      window.dispatchEvent(new CustomEvent("helix-open-build", { detail: ev }));
      break;
    case "keep_input":
      s.set({ keepInput: (ev.text as string) || "" });
      break;
    case "toast":
      s.set({ toast: (ev.text as string) || "" });
      break;
    default:
      break;
  }
}
