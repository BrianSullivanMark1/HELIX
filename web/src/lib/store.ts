// The app's one store: everything the event stream drives, plus routing.
import { create } from "zustand";

export type OrbState = "idle" | "listening" | "transcribing" | "thinking" | "speaking";
export type Hue = "none" | "working" | "done" | "error";

export interface Visual {
  type: "table" | "chart";
  [k: string]: unknown;
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
  images: string[];
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

interface CameraModal {
  id: string;
  prompt: string;
  ears: boolean;
  manual: boolean;
}

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
  cameraModal: CameraModal | null;
  cameraShutter: number; // bumps when the backend says "take the frame now"
  buildsVersion: number; // bumps to refresh the menu
  keepInput: string;
  toast: string;

  navigate: (page: Page) => void;
  addBubble: (b: Bubble) => void;
  useAction: (bubbleId: string, label: string) => void;
  setAttachments: (a: Attachment[]) => void;
  set: (partial: Partial<HelixStore>) => void;
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
  cameraModal: null,
  cameraShutter: 0,
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
    case "camera":
      s.set({
        cameraModal: {
          id: ev.id as string,
          prompt: (ev.prompt as string) || "",
          ears: Boolean(ev.ears),
          manual: Boolean(ev.manual),
        },
      });
      break;
    case "camera.capture":
      s.set({ cameraShutter: s.cameraShutter + 1 });
      break;
    case "camera.close":
      s.set({ cameraModal: null });
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
