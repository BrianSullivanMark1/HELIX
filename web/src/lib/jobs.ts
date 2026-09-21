// CURRENT TASKS on the page: the register mirrored from the event stream, with the moment a task
// finishes turned into a performance - a chime, a wave through the lattice, a toast - once.
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { api } from "./api";
import { chimeDone, chimeFail, chimeStart } from "./chime";

export type JobState = "running" | "done" | "failed" | "cancelled";
export interface Job {
  id: string; kind: string; title: string; app: string; env: string; by: string;
  state: JobState; progress: number | null; note: string; started: string; finished: string | null;
  rc: number | null; cancel_note: string; can_cancel: boolean; cancel_asked: boolean; quiet: boolean; lines_n: number;
}
export interface JobLog extends Job { lines: string[] }
export interface Toast { id: string; job: Job; at: number }

interface JobsStore {
  jobs: Record<string, Job>;
  order: string[];                 // newest first
  loaded: boolean;
  logOpen: string | null;          // the task whose log window is open
  lines: Record<string, string[]>; // live lines for open logs
  toasts: Toast[];
  wave: { kind: "done" | "failed"; at: number } | null;
  set: (p: Partial<JobsStore>) => void;
  load: () => Promise<void>;
  apply: (ev: Record<string, unknown>) => void;
  cancel: (id: string) => Promise<string | null>;
  dismiss: (id: string) => Promise<void>;
  clearFinished: () => Promise<void>;
  openLog: (id: string | null) => void;
}

const KIND_LABEL: Record<string, string> = {
  upload: "upload", fetch: "fetch", read: "fleet read", deploy: "deploy", rollback: "rollback",
  scan: "secrets scan", merge: "merge check", cache: "cache", create: "create", task: "task",
};
export const kindLabel = (k: string) => KIND_LABEL[k] || k;

export const useJobs = create<JobsStore>((set, get) => ({
  jobs: {}, order: [], loaded: false, logOpen: null, lines: {}, toasts: [], wave: null,
  set: (p) => set(p),
  load: async () => {
    try {
      const d = await api.get<{ jobs: Job[] }>("/api/jobs");
      const jobs: Record<string, Job> = {};
      for (const j of d.jobs) jobs[j.id] = j;
      set({ jobs, order: d.jobs.map((j) => j.id), loaded: true });
    } catch { set({ loaded: true }); }
  },
  apply: (ev) => {
    const s = get();
    if (ev.t === "job") {
      const j = ev.job as Job;
      const before = s.jobs[j.id];
      const jobs = { ...s.jobs, [j.id]: j };
      const order = s.order.includes(j.id) ? s.order : [j.id, ...s.order];
      const patch: Partial<JobsStore> = { jobs, order };
      if (!before && j.state === "running" && !j.quiet) chimeStart();
      if (before && before.state === "running" && j.state !== "running") {
        // THE MOMENT: gold or red - once per task
        if (j.state === "done") chimeDone(); else chimeFail();
        patch.wave = { kind: j.state === "done" ? "done" : "failed", at: performance.now() };
        window.dispatchEvent(new CustomEvent("helix-wave", { detail: { kind: j.state === "done" ? "done" : "failed", job: j } }));
        if (!j.quiet || j.state !== "done") patch.toasts = [...s.toasts, { id: j.id + ":" + j.state, job: j, at: Date.now() }].slice(-4);
        if (j.quiet && j.state === "done") window.setTimeout(() => { void get().dismiss(j.id); }, 8000);
      }
      set(patch);
    } else if (ev.t === "job_line") {
      const id = String(ev.id);
      if (s.logOpen === id || s.lines[id]) {
        const have = s.lines[id] || [];
        set({ lines: { ...s.lines, [id]: [...have, String(ev.line)].slice(-600) } });
      }
    } else if (ev.t === "job_gone") {
      const id = String(ev.id);
      const jobs = { ...s.jobs }; delete jobs[id];
      const lines = { ...s.lines }; delete lines[id];
      set({ jobs, order: s.order.filter((x) => x !== id), lines, logOpen: s.logOpen === id ? null : s.logOpen });
    }
  },
  cancel: async (id) => {
    try { await api.post(`/api/jobs/${id}/cancel`); return null; } catch (e) { return (e as Error).message || "Could not stop it."; }
  },
  dismiss: async (id) => {
    try { await api.del(`/api/jobs/${id}`); } catch { /* it may be running still; the list says so */ }
  },
  clearFinished: async () => {
    try { await api.post("/api/jobs/clear"); } catch { /* the events will say */ }
  },
  openLog: (id) => {
    set({ logOpen: id });
    if (id) {
      void api.get<JobLog>(`/api/jobs/${id}`).then((j) => {
        const s = get();
        set({ lines: { ...s.lines, [id]: j.lines }, jobs: { ...s.jobs, [id]: { ...j, lines: undefined } as unknown as Job } });
      }).catch(() => undefined);
    }
  },
}));

export function runningJobs(): Job[] {
  const s = useJobs.getState();
  return s.order.map((i) => s.jobs[i]).filter((j) => j && j.state === "running");
}

/** "3 s", "2 min", "1 h 12 min" between two ISO stamps (or now). */
export function took(a: string, b: string | null): string {
  const ms = (b ? Date.parse(b) : Date.now()) - Date.parse(a);
  if (!isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min${s % 60 ? ` ${s % 60} s` : ""}`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function ago(iso: string | null): string {
  if (!iso) return "";
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** The text a person pastes into a chat when a task went wrong: header + every line. */
export function debugText(j: Job, lines: string[]): string {
  const head = [
    `HELIX task: ${j.title}`,
    `kind: ${kindLabel(j.kind)}${j.app ? `  app: ${j.app}` : ""}${j.env ? `  env: ${j.env}` : ""}${j.by ? `  by: ${j.by}` : ""}`,
    `state: ${j.state}${j.rc !== null && j.rc !== undefined ? ` (exit ${j.rc})` : ""}  started: ${j.started}${j.finished ? `  finished: ${j.finished}` : ""}`,
    j.note ? `note: ${j.note}` : "",
    "----",
  ].filter(Boolean);
  return [...head, ...lines].join("\n");
}

/** running / done / failed counts, shallow-compared: a page that only needs the numbers does not
 *  re-render on every progress tick of an upload. */
export function useTaskCounts(): { running: number; done: number; failed: number } {
  return useJobs(useShallow((s) => {
    let running = 0, done = 0, failed = 0;
    for (const id of s.order) { const j = s.jobs[id]; if (!j || (j.quiet && j.state === "done")) continue; if (j.state === "running") running++; else if (j.state === "done") done++; else failed++; }
    return { running, done, failed };
  }));
}
