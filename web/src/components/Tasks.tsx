// CURRENT TASKS - everything HELIX is doing in the background, in one place, on every page.
//
//   TasksSection  the section above PROJECTS on the Console: one row per task with its Strandbar,
//                 the word it is on, Stop (with the plain sentence about what stopping leaves
//                 behind), Log, and Dismiss once it is over; Clear finished at the top.
//   TaskDock      the strip along the very bottom of the window (every page): folded, it is one
//                 line - how many tasks run, one combined strand; unfolded, the same rows, and the
//                 top edge drags taller or shorter (remembered on this PC).
//   TaskLog       the log window: the task's lines, live, styled and copyable - "Copy log" for the
//                 lines, "Copy for debugging" for the whole story to paste into a chat.
//   TaskToasts    one small card when a task ends (gold or ember), click to open its log.
//
// The register itself lives in lib/jobs.ts (the event stream keeps it live).
import { useEffect, useMemo, useRef, useState } from "react";
import Strandbar from "./Strandbar";
import { ago, debugText, kindLabel, took, useJobs, type Job } from "../lib/jobs";
import "./tasks.css";

const GLYPH: Record<string, string> = { upload: "⇧", fetch: "⇩", read: "◎", deploy: "▲", rollback: "↶", scan: "⌕", merge: "⑂", cache: "▤", create: "✦", vault: "⚿", task: "•" };
const APP_HUE: Record<string, number> = { MES: 188, WMS: 268, MRP: 140, ECHO: 36, CONSOLE: 200 };
const ENV_HUE: Record<string, number> = { dev: 188, qa: 42, prod: 8 };

function useTick(ms: number) {
  const [, setN] = useState(0);
  useEffect(() => { const id = window.setInterval(() => setN((n) => n + 1), ms); return () => window.clearInterval(id); }, [ms]);
}

function orderedJobs(): Job[] {
  const s = useJobs.getState();
  return s.order.map((i) => s.jobs[i]).filter(Boolean);
}

// ---------------------------------------------------------------------------- the picture of a task
/** A task's kind DRAWN, not spelled: an arrow rising through a helix for an upload, a chevron with
 *  exhaust for a deploy, a sweeping eye for a read, a lens for a scan, two strands joining for a
 *  merge, a padlock for the vault... Around it, the ring is the progress (a sweep when the length
 *  is unknown); done seals it gold with a check, failed cracks it ember, stopped greys it. */
function TaskIcon({ kind, state, progress, size = 40 }: { kind: string; state: Job["state"]; progress: number | null; size?: number }) {
  const r = size / 2 - 3, C = 2 * Math.PI * r;
  const known = progress !== null && progress !== undefined;
  const p = state === "running" ? (known ? Math.max(0.02, Math.min(1, progress)) : 0.28) : 1;
  const k = kind in GLYPH ? kind : "task";
  const pic = (() => {
    switch (k) {
      case "upload": return <><path className="ti-strand" d="M9 26c3-3 3-9 0-12M15 26c-3-3-3-9 0-12" /><path className="ti-move up" d="M12 22V10m0 0l-3.5 3.5M12 10l3.5 3.5" /></>;
      case "fetch": return <><path className="ti-strand" d="M9 26c3-3 3-9 0-12M15 26c-3-3-3-9 0-12" /><path className="ti-move down" d="M12 10v12m0 0l-3.5-3.5M12 22l3.5-3.5" /></>;
      case "deploy": return <><path className="ti-move up" d="M12 7l6 8h-3.5v4h-5v-4H6z" fill="currentColor" stroke="none" /><path className="ti-exhaust" d="M9.5 22h5M10.5 25h3" /></>;
      case "rollback": return <><path d="M6 12a6 6 0 1 1 2 4.5" /><path d="M6 8v4h4" className="ti-move back" /></>;
      case "read": return <><path d="M3 16s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z" /><circle className="ti-pupil" cx="12" cy="16" r="2.6" fill="currentColor" stroke="none" /><path className="ti-sweep" d="M4 16h16" /></>;
      case "scan": return <><circle cx="10.5" cy="14.5" r="5.5" /><path d="M14.5 18.5L20 24" /><path className="ti-sweep" d="M7 14.5h7" /></>;
      case "merge": return <><path className="ti-strand" d="M7 6v6c0 3 5 4 5 8v2M17 6v6c0 3-5 4-5 8" /><circle cx="7" cy="5" r="1.6" fill="currentColor" stroke="none" /><circle cx="17" cy="5" r="1.6" fill="currentColor" stroke="none" /><circle className="ti-pupil" cx="12" cy="23" r="1.8" fill="currentColor" stroke="none" /></>;
      case "cache": return <><rect x="5" y="7" width="14" height="4" rx="1" className="ti-stack s1" /><rect x="5" y="13" width="14" height="4" rx="1" className="ti-stack s2" /><rect x="5" y="19" width="14" height="4" rx="1" className="ti-stack s3" /></>;
      case "create": return <><path className="ti-spark" d="M12 5v6M12 19v6M5 12h6M19 12h6" transform="translate(0 2) scale(.85) translate(2 0)" /><path className="ti-spark b" d="M12 8l2 4 4 2-4 2-2 4-2-4-4-2 4-2z" fill="currentColor" stroke="none" transform="translate(0 2)" /></>;
      case "vault": return <><rect x="6" y="13" width="12" height="10" rx="2" /><path className="ti-shackle" d="M8.5 13V9.5a3.5 3.5 0 0 1 7 0V13" /><circle cx="12" cy="18" r="1.4" fill="currentColor" stroke="none" /></>;
      default: return <circle cx="12" cy="16" r="3" fill="currentColor" stroke="none" className="ti-pupil" />;
    }
  })();
  return (
    <span className={`task-icon ${state}${known ? "" : " unknown"}`} style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} className="task-ring">
        <circle cx={size / 2} cy={size / 2} r={r} className="task-ring-track" />
        <circle cx={size / 2} cy={size / 2} r={r} className="task-ring-arc" strokeDasharray={`${(C * p).toFixed(1)} ${C.toFixed(1)}`} />
      </svg>
      <svg viewBox="0 0 24 32" width={size * 0.55} height={size * 0.72} className={`task-pic ${k}`} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{pic}</svg>
      {state !== "running" && <span className="task-seal">{state === "done" ? "✓" : state === "failed" ? "✕" : "■"}</span>}
    </span>
  );
}

function Chip({ text, hue, tip }: { text: string; hue: number; tip?: string }) {
  return <span className="task-chip" style={{ "--h": hue } as React.CSSProperties} title={tip}>{text}</span>;
}

// ---------------------------------------------------------------------------- one row
function TaskRow({ job, compact }: { job: Job; compact?: boolean }) {
  const openLog = useJobs((s) => s.openLog);
  const cancel = useJobs((s) => s.cancel);
  const dismiss = useJobs((s) => s.dismiss);
  const [asking, setAsking] = useState(false);
  const [why, setWhy] = useState<string | null>(null);
  useTick(5000);
  const over = job.state !== "running";
  const who = (job.by || "").split("@")[0];
  const initials = who ? who.split(/[._-]/).map((w) => w[0]).join("").slice(0, 2).toUpperCase() : "";
  return (
    <div className={`task-row ${job.state}${compact ? " compact" : ""}${job.quiet ? " quiet" : ""}`} onClick={() => openLog(job.id)} role="button" data-tip={`${kindLabel(job.kind)} - open the log`}>
      <TaskIcon kind={job.kind} state={job.state} progress={job.progress} size={compact ? 40 : 48} />
      <div className="task-main">
        <div className="task-title-row">
          <span className="task-title elide">{job.title}</span>
          <span className="task-when">
            <b className={`task-time ${job.state}`}>{over ? took(job.started, job.finished) : took(job.started, null)}</b>
            {over && <small>{ago(job.finished)}</small>}
          </span>
        </div>
        <div className="task-chips">
          {job.app && <Chip text={job.app} hue={APP_HUE[job.app.toUpperCase()] ?? 200} tip="the app" />}
          {job.env && <Chip text={job.env.toUpperCase()} hue={ENV_HUE[job.env.toLowerCase()] ?? 200} tip="the environment" />}
          {initials && <span className="task-who" title={job.by}>{initials}</span>}
          {job.state === "running" && job.note && <span className="task-doing elide">{job.note}</span>}
          {job.state !== "running" && job.note && <span className="task-doing elide dim">{job.note}</span>}
        </div>
        <Strandbar progress={job.progress} state={job.state} height={compact ? 12 : 16} compact words={false} />
      </div>
      <div className="task-acts" onClick={(e) => e.stopPropagation()}>
        {!over && job.can_cancel && !asking && (
          <button className="task-ib stop" onClick={() => setAsking(true)} data-tip={`Stop it - ${job.cancel_note}`} aria-label="Stop">■</button>
        )}
        {!over && !job.can_cancel && !job.cancel_asked && job.kind !== "read" && (
          <span className="task-ib lock" data-tip={job.cancel_note || "This task cannot be stopped once it has started."}>⚿</span>
        )}
        {!over && job.cancel_asked && <span className="task-note">stopping…</span>}
        <button className="task-ib" onClick={() => openLog(job.id)} data-tip="Open the log" aria-label="Log">≡</button>
        {over && <button className="task-ib dim" onClick={() => void dismiss(job.id)} data-tip="Take it off the list" aria-label="Dismiss">✕</button>}
      </div>
      {asking && (
        <div className="task-ask" onClick={(e) => e.stopPropagation()}>
          <div className="task-ask-title">Stop {job.title}?</div>
          <div className="task-ask-body">{job.cancel_note}</div>
          {why && <div className="task-ask-body" style={{ color: "var(--error)" }}>{why}</div>}
          <div className="task-ask-acts">
            <button className="task-btn stop solid" onClick={() => { void cancel(job.id).then((w) => { setWhy(w); if (!w) setAsking(false); }); }}>Stop it</button>
            <button className="task-btn" onClick={() => setAsking(false)}>Keep going</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- the section (Console)
export function TasksSection({ folded, onFold }: { folded: boolean; onFold: () => void }) {
  const order = useJobs((s) => s.order);
  const jobs = useJobs((s) => s.jobs);
  const loaded = useJobs((s) => s.loaded);
  const clear = useJobs((s) => s.clearFinished);
  const wave = useJobs((s) => s.wave);
  useEffect(() => { if (!loaded) void useJobs.getState().load(); }, [loaded]);
  const list = useMemo(() => order.map((i) => jobs[i]).filter((j) => j && !(j.quiet && j.state === "done")), [order, jobs]);
  const running = list.filter((j) => j.state === "running").length;
  const done = list.filter((j) => j.state === "done").length;
  const failed = list.filter((j) => j.state === "failed" || j.state === "cancelled").length;
  const flash = wave && performance.now() - wave.at < 1500 ? ` flash-${wave.kind}` : "";
  return (
    <section className={`board-section tasks-section${folded ? " folded" : ""}${flash}`}>
      <button className="board-section-head" onClick={onFold}>
        <span className="board-section-chev">{folded ? "▸" : "▾"}</span>
        <span className="board-section-title">CURRENT TASKS</span>
        <span className="board-section-sub">
          {running ? <span className="tasks-count run">{running} running</span> : <span>nothing running</span>}
          {done ? <> · <span className="tasks-count gold">{done} done</span></> : null}
          {failed ? <> · <span className="tasks-count red">{failed} failed or stopped</span></> : null}
        </span>
        <span className="flex-1" />
        <span className="board-section-tools" onClick={(e) => e.stopPropagation()}>
          {done + failed > 0 && <button className="task-btn dim" onClick={() => void clear()} title="Take every finished task off the list">Clear finished</button>}
        </span>
      </button>
      {!folded && (
        <div className="tasks-grid mt-3">
          {list.length === 0 && <div className="tasks-empty">Nothing running. Uploads, reads, deploys, scans and merge checks land here the moment they start - and stay, gold or ember, until you clear them.</div>}
          {list.map((j) => <TaskRow key={j.id} job={j} compact />)}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------- the dock (every page)
export function TaskDock() {
  const order = useJobs((s) => s.order);
  const jobs = useJobs((s) => s.jobs);
  const loaded = useJobs((s) => s.loaded);
  const clear = useJobs((s) => s.clearFinished);
  useEffect(() => { if (!loaded) void useJobs.getState().load(); }, [loaded]);
  const [open, setOpen] = useState<boolean>(() => { try { return localStorage.getItem("helix_dock_open") === "1"; } catch { return false; } });
  const [height, setHeight] = useState<number>(() => { try { return Math.max(140, Math.min(window.innerHeight * 0.8, Number(localStorage.getItem("helix_dock_h")) || 260)); } catch { return 260; } });
  const drag = useRef<{ y: number; h: number } | null>(null);
  const list = useMemo(() => order.map((i) => jobs[i]).filter(Boolean), [order, jobs]);
  const running = list.filter((j) => j.state === "running");
  const loud = running.filter((j) => !j.quiet);
  const finished = list.filter((j) => j.state !== "running");
  const combined = running.length ? (running.every((j) => j.progress === null) ? null : running.reduce((a, j) => a + (j.progress ?? 0), 0) / running.length) : 1;
  const latest = list[0];
  const toggle = () => setOpen((o) => { try { localStorage.setItem("helix_dock_open", o ? "0" : "1"); } catch { /* fine */ } return !o; });
  useEffect(() => {
    const move = (e: PointerEvent) => { if (!drag.current) return; const h = Math.max(140, Math.min(window.innerHeight * 0.8, drag.current.h + (drag.current.y - e.clientY))); setHeight(h); };
    const up = () => { if (drag.current) { drag.current = null; try { localStorage.setItem("helix_dock_h", String(Math.round(height))); } catch { /* fine */ } } };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  }, [height]);
  // the window never closes on a running task without a word
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (loud.length) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [loud.length]);
  if (!loaded) return null;
  return (
    <div className={`task-dock${open ? " open" : ""}${loud.length ? " busy" : ""}`} style={open ? { height } : undefined}>
      <div className="task-dock-grip" onPointerDown={(e) => { if (!open) return; drag.current = { y: e.clientY, h: height }; (e.target as HTMLElement).setPointerCapture?.(e.pointerId); }} title={open ? "Drag to resize" : undefined}>
        <button className="task-dock-head" onClick={toggle} title={open ? "Fold the tasks away" : "Open the tasks"}>
          <span className="task-dock-chev">{open ? "▾" : "▴"}</span>
          <span className="task-dock-title">CURRENT TASKS</span>
          <span className="task-dock-sub">
            {running.length ? `${running.length} running` : finished.length ? `${finished.length} finished` : "quiet"}
            {latest && !open ? <> · <span className="elide" style={{ maxWidth: 380, display: "inline-block", verticalAlign: "bottom" }}>{latest.title}{latest.state === "running" && latest.note ? ` — ${latest.note}` : latest.state !== "running" ? ` — ${latest.state}` : ""}</span></> : null}
          </span>
          <span className="task-dock-strand"><Strandbar progress={combined} state={running.length ? "running" : latest ? (latest.state === "done" ? "done" : latest.state) : "done"} height={12} words={false} compact /></span>
        </button>
        {open && finished.length > 0 && <button className="task-btn dim task-dock-clear" onClick={() => void clear()}>Clear finished</button>}
      </div>
      {open && (
        <div className="task-dock-body">
          {list.length === 0 && <div className="tasks-empty">Nothing running. This strip stays on every page so an upload or a deploy is never out of sight.</div>}
          {list.map((j) => <TaskRow key={j.id} job={j} compact />)}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------- the log window
export function TaskLog() {
  const id = useJobs((s) => s.logOpen);
  const job = useJobs((s) => (s.logOpen ? s.jobs[s.logOpen] : undefined));
  const lines = useJobs((s) => (s.logOpen ? s.lines[s.logOpen] : undefined)) || [];
  const openLog = useJobs((s) => s.openLog);
  const cancel = useJobs((s) => s.cancel);
  const [copied, setCopied] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const pre = useRef<HTMLDivElement | null>(null);
  useTick(1000);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") openLog(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openLog]);
  useEffect(() => { if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight; }, [lines.length, follow]);
  if (!id || !job) return null;
  const copy = (text: string, what: string) => {
    void navigator.clipboard?.writeText(text).then(() => { setCopied(what); window.setTimeout(() => setCopied(null), 1600); }).catch(() => {
      const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); setCopied(what); window.setTimeout(() => setCopied(null), 1600);
    });
  };
  const cls = (ln: string) => ln.startsWith("[helix]") ? "helix" : /error|failed|exception|traceback|conflict/i.test(ln) ? "bad" : /done|success|deployed|cached|clean|in the catalog/i.test(ln) ? "good" : /warn/i.test(ln) ? "warn" : "";
  return (
    <div className="task-log-wrap" onClick={() => openLog(null)}>
      <div className={`task-log ${job.state}`} onClick={(e) => e.stopPropagation()}>
        <div className="task-log-head">
          <TaskIcon kind={job.kind} state={job.state} progress={job.progress} size={52} />
          <div className="min-w-0 flex-1">
            <div className="task-log-kicker">{kindLabel(job.kind).toUpperCase()}{job.app ? ` · ${job.app}` : ""}{job.env ? ` · ${job.env.toUpperCase()}` : ""}{job.by ? ` · ${job.by}` : ""}</div>
            <div className="task-log-title elide">{job.title}</div>
            <div className="task-log-sub">
              <span className={`task-state ${job.state}`}>{job.state === "running" ? "running" : job.state === "done" ? "done" : job.state === "failed" ? "failed" : "stopped"}</span>
              {" · "}started {ago(job.started)} · {job.finished ? `took ${took(job.started, job.finished)}` : `${took(job.started, null)} so far`}
              {job.rc !== null && job.rc !== undefined ? ` · exit ${job.rc}` : ""}{job.note ? ` · ${job.note}` : ""}
            </div>
          </div>
          <div className="task-log-acts">
            {job.state === "running" && job.can_cancel && <button className="task-btn stop" title={job.cancel_note} onClick={() => { if (window.confirm(`Stop ${job.title}?\n\n${job.cancel_note}`)) void cancel(job.id); }}>Stop</button>}
            <button className="task-btn" onClick={() => copy(lines.join("\n"), "log")}>{copied === "log" ? "Copied" : "Copy log"}</button>
            <button className="task-btn gold" onClick={() => copy(debugText(job, lines), "debug")} title="Title, kind, state, times and every line - ready to paste for help">{copied === "debug" ? "Copied" : "Copy for debugging"}</button>
            <button className="task-btn" onClick={() => openLog(null)} aria-label="Close">✕</button>
          </div>
        </div>
        <div className="task-log-strand"><Strandbar progress={job.progress} state={job.state} height={16} note={job.note} /></div>
        <div className="task-log-lines" ref={pre} onScroll={(e) => { const el = e.currentTarget; setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24); }}>
          {lines.length === 0 && <div className="task-log-empty">{job.state === "running" ? "No lines yet - they appear here as they come." : "This task printed nothing."}</div>}
          {lines.map((ln, i) => (
            <div key={i} className={`task-line ${cls(ln)}`}><span className="task-ln">{i + 1}</span><span className="task-txt">{ln}</span></div>
          ))}
        </div>
        {!follow && job.state === "running" && <button className="task-btn task-log-follow" onClick={() => { setFollow(true); if (pre.current) pre.current.scrollTop = pre.current.scrollHeight; }}>▾ follow</button>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- toasts
export function TaskToasts() {
  const toasts = useJobs((s) => s.toasts);
  const openLog = useJobs((s) => s.openLog);
  const set = useJobs((s) => s.set);
  useTick(1000);
  const live = toasts.filter((t) => Date.now() - t.at < 7000);
  useEffect(() => { if (live.length !== toasts.length) set({ toasts: live }); });
  if (!live.length) return null;
  return (
    <div className="task-toasts">
      {live.map((t) => (
        <button key={t.id} className={`task-toast ${t.job.state}`} onClick={() => { openLog(t.job.id); set({ toasts: toasts.filter((x) => x.id !== t.id) }); }}>
          <span className="task-toast-mark">{t.job.state === "done" ? "✓" : t.job.state === "failed" ? "✕" : "■"}</span>
          <span className="min-w-0">
            <span className="task-toast-title elide">{t.job.title}</span>
            <span className="task-toast-sub">{t.job.state === "done" ? "done" : t.job.state === "failed" ? "failed" : "stopped"}{t.job.note ? ` · ${t.job.note}` : ""} · open the log</span>
          </span>
        </button>
      ))}
    </div>
  );
}

export { orderedJobs };
