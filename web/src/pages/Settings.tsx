// Settings — THE FORGE's layout in HELIX's skin (HELIX_MARK1_PLAN.md §17): a rail of groups on
// the left, one group at a time on the right, an Overview of cards that jump to a group, and a
// search that finds a setting wherever it lives. Every value and every call is what the page did
// before (credentials write-only with presence dots, the brain status line, connections review,
// dreaming, files, conversation & presence, voice, camera, power); only the shape changed.
// Save writes only what changed.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, tokenUrl } from "../lib/api";
import { useHelix } from "../lib/store";
import "./settings.css";
import { perf, setMode, type Mode } from "../lib/perf";
import { VaultCard } from "../components/Vault";

interface SettingsData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
  connections: Record<string, { label: string; set: boolean }>;
  brain: { tone: string; line: string };
  voices: string[];
  gmail: { configured: boolean; address: string };
  calendar: { configured: boolean };
}

/** GET /api/dream — the nightly dream session as the backend sees it right now. */
interface DreamInfo {
  available: boolean;
  running: boolean;
  line: string;
  status: string;
  report: string;
  frozen_without_source: boolean;
  model: string;
}

/** GET /api/face/status — is the built face behind its source; has the Python changed since launch. */
interface FaceStatus {
  face: { stale: boolean; never_built: boolean; npm: boolean; src_changed_at: number | null; built_at: number | null };
  backend: { stale: boolean; started_at: number; py_changed_at: number | null };
  building: boolean;
  last: { ok: boolean; seconds: number; at: number; output: string; error: string | null } | null;
}

type GroupId = "overview" | "updates" | "brain" | "board" | "dreaming" | "connections" | "presence" | "voice" | "camera" | "power";

const GROUPS: { id: GroupId; icon: string; title: string; sub: string }[] = [
  { id: "overview", icon: "▦", title: "Overview", sub: "what's set, what needs you" },
  { id: "updates", icon: "⟳", title: "Updates", sub: "build the face, restart the backend" },
  { id: "brain", icon: "◉", title: "The brain", sub: "tokens and the model" },
  { id: "board", icon: "⬡", title: "The Board", sub: "repos and Cloud Run" },
  { id: "dreaming", icon: "◐", title: "Dreaming", sub: "the night session" },
  { id: "connections", icon: "⇄", title: "Connections", sub: "services, mail, calendar" },
  { id: "presence", icon: "◈", title: "Conversation", sub: "wake word, listening, files" },
  { id: "voice", icon: "♪", title: "Voice & look", sub: "how it sounds and renders" },
  { id: "camera", icon: "◎", title: "Camera", sub: "which one, how it behaves" },
  { id: "power", icon: "⏻", title: "Power", sub: "stop HELIX" },
];

interface Section {
  id: string;
  group: Exclude<GroupId, "overview">;
  icon: string;
  title: string;
  what: string;          // the one-line "what this is", after the dash
  keys: string;          // what search matches against, beyond the title
  body: ReactNode;
}

function Dot({ on, yes = "Set", no = "Not set" }: { on: boolean; yes?: string; no?: string }) {
  return (
    <span className="st-dot" style={{ color: on ? "var(--done)" : "var(--muted)" }}>
      {on ? `● ${yes}` : `○ ${no}`}
    </span>
  );
}

function Switch({ checked, onChange, children }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode }) {
  return (
    <label className="st-switch">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="tr" aria-hidden="true" />
      <span>{children}</span>
    </label>
  );
}

function Choice({ on, title, detail, pill, onPick }: { on: boolean; title: string; detail: string; pill?: string; onPick: () => void }) {
  return (
    <button type="button" className={`st-choice${on ? " on" : ""}`} onClick={onPick}>
      <span className="dot" aria-hidden="true" />
      <span><div className="t">{title}</div><div className="d">{detail}</div></span>
      {pill && <span className="pill">{pill}</span>}
    </button>
  );
}

/** THE VOICE (2026-09-21): Google's Gemini voices, styled in plain words - a British butler, a
 *  Dubliner, mission control - with a Hear-it on every card. edge-tts stays as the free engine,
 *  the Windows voice as the last resort. Each person's PC keeps its own choice. */
interface VoiceCatalog { voices: { name: string; gender: string; feel: string; pick: boolean }[]; styles: { key: string; label: string; flag: string; prompt: string | null }[]; default_voice: string; default_style: string; model: string; google_available: boolean; last_error: string | null }
const SAMPLE = "Good evening. HELIX online - the fleet is quiet, and I have been reading your commits.";
function VoiceCard({ val, setVal, edgeVoices }: { val: (k: string) => unknown; setVal: (k: string, v: unknown) => void; edgeVoices: string[] }) {
  const [cat, setCat] = useState<VoiceCatalog | null>(null);
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);    // the voice (or "style") being auditioned
  const [note, setNote] = useState<string | null>(null);
  const [audio] = useState(() => new Audio());
  useEffect(() => { void api.get<VoiceCatalog>("/api/say/voices").then(setCat).catch(() => setCat(null)); }, []);
  const engine = String(val("tts_engine") ?? "google");
  const voice = String(val("tts_google_voice") ?? cat?.default_voice ?? "Charon");
  const style = String(val("tts_style") ?? cat?.default_style ?? "butler");
  const custom = String(val("tts_style_custom") ?? "");
  const hear = async (v: string, key: string) => {
    setBusy(key); setNote(null);
    try {
      audio.pause();
      const r = await api.post<{ url?: string; fell_back?: string; error?: string }>("/api/say", { text: SAMPLE, preview: true, engine: "google", voice: v, style, style_custom: custom });
      if (r.fell_back) setNote(`Google did not answer (${r.fell_back.replace(/^google:\s*/, "")}) - that was edge-tts you heard.`);
      if (r.url) { audio.src = tokenUrl(r.url); audio.onended = () => setBusy(null); await audio.play(); } else setBusy(null);
    } catch (e) { setNote((e as Error).message); setBusy(null); }
  };
  const voices = (cat?.voices || []).filter((v) => all || v.pick || v.name === voice);
  return (
    <div className="space-y-4">
      <div className="st-choices">
        <Choice on={engine === "google"} title="Google's voice - Gemini" detail="paid quality, styled in plain words, spoken as you (your gcloud login); the one we chose" pill={cat && !cat.google_available ? "gcloud missing" : undefined} onPick={() => setVal("tts_engine", "google")} />
        <Choice on={engine === "edge"} title="edge-tts" detail="free neural voices; the fallback when Google cannot answer" onPick={() => setVal("tts_engine", "edge")} />
        <Choice on={engine === "os"} title="Windows voice" detail="offline, no network at all; the last resort" onPick={() => setVal("tts_engine", "os")} />
      </div>
      {engine === "google" && (
        <>
          <div className="vc-kicker">THE STYLE <small>how the voice carries itself - this is where the accent lives</small></div>
          <div className="vc-styles">
            {(cat?.styles || []).map((st) => (
              <button key={st.key} type="button" className={`vc-style${style === st.key ? " on" : ""}`} onClick={() => setVal("tts_style", st.key)} title={st.prompt || (st.key === "custom" ? "Your own sentence" : "No styling")}>
                {st.flag && <span className="vc-flag">{st.flag}</span>}{st.label}
              </button>
            ))}
          </div>
          {style === "custom" ? (
            <textarea className="vc-custom" rows={2} value={custom} placeholder="Speak like a calm ship's captain from Cornwall - slow, warm, sure of every word." onChange={(e) => setVal("tts_style_custom", e.target.value)} />
          ) : (
            <div className="st-note vc-prompt">{cat?.styles.find((x) => x.key === style)?.prompt || "No styling - the voice as Google ships it."}</div>
          )}
          <div className="vc-kicker">THE VOICE <small>{all ? "all thirty" : "our eight picks"}</small><button type="button" className="vc-link" onClick={() => setAll((v) => !v)}>{all ? "Just the picks" : "Show all thirty"}</button></div>
          <div className="vc-grid">
            {voices.map((v) => (
              <div key={v.name} className={`vc-voice${voice === v.name ? " on" : ""}`} onClick={() => setVal("tts_google_voice", v.name)}>
                <div className="vc-name">{v.name}<span className={`vc-g ${v.gender}`}>{v.gender === "male" ? "M" : "F"}</span></div>
                <div className="vc-feel">{v.feel}</div>
                <button type="button" className={`vc-hear${busy === v.name ? " on" : ""}`} onClick={(e) => { e.stopPropagation(); void hear(v.name, v.name); }} disabled={busy !== null && busy !== v.name}>{busy === v.name ? "◼ playing" : "▶ Hear it"}</button>
              </div>
            ))}
          </div>
          {note && <div className="st-note warn">{note}</div>}
          {cat?.last_error && !note && <div className="st-note warn">Last time Google refused: {cat.last_error}</div>}
          <div className="st-note">Every line HELIX speaks goes through this voice and style - replies, task endings, the Say button. Repeated lines are cached, so they cost nothing twice. Model: {cat?.model || "gemini-2.5-flash-tts"}.</div>
        </>
      )}
      {engine !== "google" && (
        <>
          <Row label="Voice">
            <select value={String(val("tts_voice") ?? "en-GB-RyanNeural")} onChange={(e) => setVal("tts_voice", e.target.value)}>
              {edgeVoices.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </Row>
          <Row label={`Speed - ${Number(val("tts_rate") ?? 1).toFixed(1)}x`}>
            <input type="range" min={0.8} max={2.0} step={0.1} className="accent-[#3fe0e0]"
              value={Number(val("tts_rate") ?? 1)} onChange={(e) => setVal("tts_rate", Number(e.target.value))} />
          </Row>
        </>
      )}
    </div>
  );
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="st-row">
      <span className="lbl">{label}{hint && <small>{hint}</small>}</span>
      {children}
    </div>
  );
}

function SectionView({ s, open, toggle, hit, groupLabel }: { s: Section; open: boolean; toggle: () => void; hit?: boolean; groupLabel?: string }) {
  return (
    <div className={`st-section${open ? " open" : ""}${hit ? " hit" : ""}`}>
      <button type="button" className="hd" onClick={toggle}>
        <span className="ic">{s.icon}</span>
        <span className="tt">
          <span className="t">{s.title}{groupLabel && <span className="g">in {groupLabel}</span>}</span>
          <span className="w">{s.what}</span>
        </span>
        <span className="chev">▼</span>
      </button>
      {open && <div className="body">{s.body}</div>}
    </div>
  );
}

export default function Settings() {
  const navigate = useHelix((s) => s.navigate);
  const [data, setData] = useState<SettingsData | null>(null);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [secretEdits, setSecretEdits] = useState<Record<string, string>>({});
  const [gmailAddr, setGmailAddr] = useState("");
  const [gmailPw, setGmailPw] = useState("");
  const [calUrl, setCalUrl] = useState("");
  const [note, setNote] = useState("");
  const [cameras, setCameras] = useState<string[]>([]);
  const [dream, setDream] = useState<DreamInfo | null>(null);
  const [dreamNote, setDreamNote] = useState("");
  const [group, setGroup] = useState<GroupId>("overview");
  const [q, setQ] = useState("");
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const [face, setFace] = useState<FaceStatus | null>(null);
  // THE TOOLS (2026-09-22): what the deploy lane needs on this PC - each row says the fix.
  interface ToolRow { key: string; label: string; ok: boolean; value: string | null; fix: string }
  const [tools, setTools] = useState<{ tools: ToolRow[]; needs: string[]; ok: boolean } | null>(null);
  const [toolEdits, setToolEdits] = useState<Record<string, string>>({});
  const [toolNote, setToolNote] = useState("");
  const [perfMode, setPerfMode] = useState<Mode>(perf.mode);
  const [fpsOn, setFpsOn] = useState<boolean>(() => { try { return localStorage.getItem("helix_fps") === "1"; } catch { return false; } });
  const toggleFps = (v: boolean) => { setFpsOn(v); try { localStorage.setItem("helix_fps", v ? "1" : "0"); } catch { /* fine */ } window.dispatchEvent(new Event("helix-fps-toggle")); };
  const [perfLine, setPerfLine] = useState("");
  useEffect(() => { const id = window.setInterval(() => setPerfLine(`${perf.fps} fps, ${perf.frameMs} ms a frame, drawing at ${perf.level}`), 1000); return () => window.clearInterval(id); }, []);
  const readTools = useCallback(() => { void api.get<{ tools: ToolRow[]; needs: string[]; ok: boolean }>("/api/deploy/tools").then(setTools).catch(() => setTools(null)); }, []);
  useEffect(() => { readTools(); }, [readTools]);
  const saveTool = async (key: string) => {
    const v = (toolEdits[key] ?? "").trim();
    try {
      if (key === "console_root") await api.put("/api/deploy/console_root", { path: v });
      else if (key === "firebase_project") await api.put("/api/settings", { values: { firebase_project: v } });
      setToolNote("Saved."); readTools(); window.dispatchEvent(new Event("helix-settings-saved"));
    } catch (e) { setToolNote((e as Error).message); }
  };
  const [building, setBuilding] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [buildNote, setBuildNote] = useState("");
  const liveDream = useHelix((s) => s.dream); // the event stream's view: flips the moment a session starts/ends

  const loadDream = useCallback(() => {
    void api.get<DreamInfo>("/api/dream").then(setDream).catch(() => undefined);
  }, []);

  const load = useCallback(() => {
    void api.get<SettingsData>("/api/settings").then((d) => {
      setData(d);
      setEdits({});
      setSecretEdits({});
      setGmailAddr(d.gmail.address || "");
    });
    loadDream();
    // Camera names are only readable once the camera has been allowed; until then the list is
    // empty and the hint says so.
    void navigator.mediaDevices?.enumerateDevices?.()
      .then((all) => setCameras(all.filter((x) => x.kind === "videoinput" && x.label).map((x) => x.label)))
      .catch(() => undefined);
  }, [loadDream]);
  useEffect(load, [load]);
  // Updates: the face/backend staleness, re-read every 15 s while the page is open.
  const loadFace = useCallback(() => {
    void api.get<FaceStatus>("/api/face/status").then(setFace).catch(() => setFace(null));
  }, []);
  useEffect(() => {
    loadFace();
    const id = window.setInterval(loadFace, 15000);
    return () => window.clearInterval(id);
  }, [loadFace]);

  const buildFace = () => {
    setBuilding(true);
    setBuildNote("Building the face — Vite takes a few seconds…");
    void api.post<FaceStatus["last"] & { error?: string }>("/api/face/build")
      .then((r) => {
        if (r && r.ok) {
          setBuildNote(`Built in ${r.seconds}s — reloading the face…`);
          window.setTimeout(() => window.location.reload(), 900);
        } else {
          setBuildNote(r?.error || "The build failed.");
        }
        loadFace();
      })
      .catch((e: Error) => setBuildNote(e.message))
      .finally(() => setBuilding(false));
  };

  const restartBackend = () => {
    if (!window.confirm("Restart HELIX? It comes straight back with the new Python; the page reconnects on its own.")) return;
    setRestarting(true);
    setBuildNote("Restarting HELIX…");
    void api.post("/api/face/restart").catch(() => undefined).finally(() => {
      // Poll until the new instance answers, then reload onto it.
      const t0 = Date.now();
      const tick = () => {
        fetch("/api/snapshot", { headers: { "X-Helix-Token": sessionStorage.getItem("helix_token") || "" } })
          .then((r) => { if (r.ok) window.location.reload(); else throw new Error(); })
          .catch(() => { if (Date.now() - t0 < 90000) window.setTimeout(tick, 1500); else setBuildNote("HELIX did not come back in 90 s — start it from the desktop icon."); });
      };
      window.setTimeout(tick, 4000);
    });
  };
  // A session starting or ending while this page is open re-reads the status line.
  useEffect(() => { loadDream(); }, [liveDream?.running, loadDream]);

  const val = (key: string): unknown => (key in edits ? edits[key] : data?.values[key]);
  const setVal = (key: string, v: unknown) => setEdits((e) => ({ ...e, [key]: v }));

  const dreamRunning = liveDream ? liveDream.running : Boolean(dream?.running);
  const dreamNow = () => {
    setDreamNote("Starting…");
    void api.post<{ ok: boolean; text: string }>("/api/dream/now", { minutes: 30 })
      .then((res) => { setDreamNote(res.text || (res.ok ? "Dreaming." : "Couldn't start.")); loadDream(); })
      .catch(() => setDreamNote("Couldn't start a dream session — try again."));
  };
  const dreamStop = () => {
    void api.post<{ ok: boolean; text: string }>("/api/dream/stop")
      .then((res) => { setDreamNote(res.text || "Stopped."); loadDream(); })
      .catch(() => setDreamNote("Couldn't stop it — try again."));
  };

  const dirty = Object.keys(edits).length
    + Object.values(secretEdits).filter((v) => v.trim()).length
    + (gmailAddr.trim() && gmailPw.trim() ? 1 : 0)
    + (calUrl.trim() ? 1 : 0);

  const save = () => {
    const values: Record<string, unknown> = { ...edits };
    for (const [k, v] of Object.entries(secretEdits)) if (v.trim()) values[k] = v.trim();
    const body: Record<string, unknown> = { values };
    if (gmailAddr.trim() && gmailPw.trim()) body.gmail = { address: gmailAddr.trim(), password: gmailPw.trim() };
    if (calUrl.trim()) body.calendar_url = calUrl.trim();
    void api.put<{ ok: boolean; changed: string[]; rejected?: Record<string, string> }>("/api/settings", body).then((res) => {
      // A dream value the backend could not read (a cleared start time, say) is refused and kept as
      // it was, not saved as the default — say so and stay on the page instead of leaving.
      const refused = Object.values(res?.rejected ?? {});
      setGmailPw("");
      setCalUrl("");
      load();
      if (refused.length) {
        setNote(`Saved the rest. ${refused.join(" ")}`);
        return;
      }
      setNote("Saved.");
      window.dispatchEvent(new CustomEvent("helix-settings-saved"));
      window.setTimeout(() => navigate({ name: "console" }), 400);
    }).catch(() => setNote("Save failed — try again."));
  };

  const removeConnection = (sid: string, label: string) => {
    if (!window.confirm(`Remove the ${label} connection?`)) return;
    void api.post<{ still_connected: string[] }>("/api/settings/remove_connection", { service: sid })
      .then((res) => {
        if (res.still_connected.length)
          setNote(`${label} is still connected — an environment variable on this PC is handing it ` +
            `${res.still_connected.join(", ")}. Clear it there (and sign out and back in).`);
        load();
      });
  };

  // ------------------------------------------------------------------ the sections
  // Built every render (they close over live state); cheap, and it keeps each setting's UI next to
  // its keywords so search cannot drift from what is on screen.
  const sections: Section[] = useMemo(() => {
    if (!data) return [];
    const brainColor = data.brain.tone === "ok" ? "var(--done)"
      : data.brain.tone === "warn" ? "var(--working)" : "var(--muted)";
    const connEntries = Object.entries(data.connections);
    const fmt = (t: number | null | undefined) => t ? new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "never";
    return [
      // ---------------------------------------------------------------- updates
      {
        id: "face-build", group: "updates", icon: "⟳", title: "The face",
        what: "the pages you are looking at - rebuilt here instead of in PyCharm",
        keys: "update build npm vite face web dist rebuild",
        body: (
          <div className="space-y-3">
            {face === null ? (
              <div className="st-note">Reading the build status…</div>
            ) : (
              <>
                <div className="st-update">
                  <span className={`state ${face.face.stale ? "stale" : "fresh"}`}>
                    {face.face.never_built ? "Never built" : face.face.stale ? "Source is newer than the build" : "Up to date"}
                  </span>
                  <button className={`btn ${face.face.stale ? "btn-primary" : ""}`} disabled={building || !face.face.npm} onClick={buildFace}>
                    {building ? <><span className="st-spin">⟳</span> Building…</> : "⟳ Build the face"}
                  </button>
                </div>
                <div className="st-note">
                  this window is running build <b>{__HELIX_BUILD__}</b> · source changed {fmt(face.face.src_changed_at)} · last built {fmt(face.face.built_at)}
                  {!face.face.npm && " · npm is not installed on this machine, so the build cannot run here"}
                </div>
                {face.last && (
                  <div className="st-note" style={{ color: face.last.ok ? "var(--done)" : "var(--error)" }}>
                    Last build {face.last.ok ? "succeeded" : "failed"} in {face.last.seconds}s, {fmt(face.last.at)}{face.last.error ? ` — ${face.last.error}` : ""}
                  </div>
                )}
                {face.last?.output && <pre className="st-output">{face.last.output}</pre>}
              </>
            )}
            {buildNote && <div className="text-[13px]" style={{ color: "var(--cyan)" }}>{buildNote}</div>}
          </div>
        ),
      },
      {
        id: "backend-restart", group: "updates", icon: "⏻", title: "The backend",
        what: "the Python - a restart picks up changed code",
        keys: "update restart backend python relaunch code changed",
        body: (
          <div className="space-y-3">
            {face === null ? (
              <div className="st-note">Reading the status…</div>
            ) : (
              <>
                <div className="st-update">
                  <span className={`state ${face.backend.stale ? "stale" : "fresh"}`}>
                    {face.backend.stale ? "Python changed since launch" : "Running the current code"}
                  </span>
                  <button className={`btn ${face.backend.stale ? "btn-primary" : ""}`} disabled={restarting} onClick={restartBackend}>
                    {restarting ? <><span className="st-spin">⟳</span> Restarting…</> : "⏻ Restart HELIX"}
                  </button>
                </div>
                <div className="st-note">
                  running since {fmt(face.backend.started_at)} · code last changed {fmt(face.backend.py_changed_at)}
                </div>
                <div className="st-note">
                  A restart spawns the next HELIX, waits for this one to let go, and the page reconnects
                  on its own. Watchers, reminders and voice pause for a few seconds.
                </div>
              </>
            )}
            {buildNote && <div className="text-[13px]" style={{ color: "var(--cyan)" }}>{buildNote}</div>}
          </div>
        ),
      },
      // ---------------------------------------------------------------- the brain
      {
        id: "subscription", group: "brain", icon: "◉", title: "Claude subscription",
        what: "the recommended way HELIX thinks", keys: "token oauth claude setup-token subscription",
        body: (
          <div className="space-y-2">
            <Row label="Subscription token" hint="from `claude setup-token`">
              <input type="password" placeholder="sk-ant-oat01-…" className="flex-1"
                value={secretEdits.claude_code_oauth_token ?? ""}
                onChange={(e) => setSecretEdits((s) => ({ ...s, claude_code_oauth_token: e.target.value }))} />
              <Dot on={data.secrets.claude_code_oauth_token} />
            </Row>
            <div className="st-note">Write-only: the value is never shown back, only whether one is set.</div>
          </div>
        ),
      },
      {
        id: "apikey", group: "brain", icon: "◌", title: "API key",
        what: "the metered fallback when the subscription rail is out", keys: "api key anthropic metered fallback",
        body: (
          <div className="space-y-2">
            <Row label="Claude API key">
              <input type="password" placeholder="sk-ant-…" className="flex-1"
                value={secretEdits.claude_api_key ?? ""}
                onChange={(e) => setSecretEdits((s) => ({ ...s, claude_api_key: e.target.value }))} />
              <Dot on={data.secrets.claude_api_key} />
            </Row>
            <div className="st-note" style={{ color: brainColor }}>{data.brain.line}</div>
          </div>
        ),
      },
      // ---------------------------------------------------------------- the board
      {
        id: "github", group: "board", icon: "⬡", title: "GitHub",
        what: "lets the Board see each repo's HEAD, so it can say whether an environment is behind",
        keys: "github token repo drift behind fleet board",
        body: (
          <div className="space-y-2">
            <Row label="GitHub token" hint="read-only scope is enough">
              <input type="password" placeholder="github_pat_… or ghp_…" className="flex-1"
                value={secretEdits.github_token ?? ""}
                onChange={(e) => setSecretEdits((s) => ({ ...s, github_token: e.target.value }))} />
              <Dot on={data.secrets.github_token} />
            </Row>
            <div className="st-note">Cloud Run is read through your own gcloud login — nothing to paste here for that.</div>
          </div>
        ),
      },
      {
        id: "tools", group: "board", icon: "⚒", title: "The tools on this PC",
        what: "what shipping needs here: gcloud signed in, the Firebase CLI, the console checkout, the token - and how to fix each",
        keys: "tools gcloud firebase cli console dev.ps1 checkout project deploy missing fix",
        body: (
          <div className="space-y-2">
            {tools === null && <div className="st-note">Checking the tools…</div>}
            {tools?.tools.map((r) => (
              <div key={r.key} className="st-tool">
                <span className={`st-tool-dot${r.ok ? " ok" : ""}`} aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="st-tool-label">{r.label}</span>
                    <span className="st-tool-value elide">{r.value || (r.ok ? "ready" : "missing")}</span>
                  </div>
                  {!r.ok && r.fix && <div className="st-tool-fix">{r.fix}</div>}
                  {(r.key === "console_root" || r.key === "firebase_project") && (
                    <div className="flex items-center gap-2 mt-1">
                      <input className="flex-1" placeholder={r.key === "console_root" ? "C:\\Users\\you\\Desktop\\BRMS_MES_WEB_APP\\BRMS_MES_WEB_VERSION" : "the Firebase project id"}
                        value={toolEdits[r.key] ?? r.value ?? ""} onChange={(e) => setToolEdits((t) => ({ ...t, [r.key]: e.target.value }))}
                        onKeyDown={(e) => { if (e.key === "Enter") void saveTool(r.key); }} />
                      <button className="btn text-xs" onClick={() => void saveTool(r.key)}>Save</button>
                    </div>
                  )}
                </div>
              </div>
            ))}
            <div className="flex items-center gap-3">
              <button className="btn text-xs" onClick={() => { setTools(null); readTools(); }}>⟳ Check again</button>
              <span className="st-note">{toolNote || (tools?.ok ? "Everything shipping needs is here." : "The menu button glows until every row is green.")}</span>
            </div>
          </div>
        ),
      },
      {
        id: "vault", group: "board", icon: "⚿", title: "The vault",
        what: "the company's secrets in Google Cloud Secret Manager - create, rotate, delete; values never show",
        keys: "vault secrets secret manager rotate delete password key token",
        body: (
          <div className="space-y-2">
            <VaultCard compact />
            <div className="st-note">Apps read a secret when their container starts, so a rotation takes after a redeploy or a bounce. Deleting is Brian, Brendan and Kate, with the name typed back.</div>
          </div>
        ),
      },
      // ---------------------------------------------------------------- dreaming
      {
        id: "dream-window", group: "dreaming", icon: "◐", title: "The window",
        what: "when HELIX may spend the night improving itself", keys: "dream nightly window start hours enabled",
        body: (
          <div className="space-y-3">
            <Switch checked={Boolean(val("dream_enabled"))} onChange={(v) => setVal("dream_enabled", v)}>
              Dream nightly — a session of non-stop self-improvement while you sleep
            </Switch>
            <Row label="Window">
              <span className="text-[13px]">from</span>
              <input type="time" value={String(val("dream_start") ?? "23:00")}
                onChange={(e) => setVal("dream_start", e.target.value)} />
              <span className="text-[13px]">for</span>
              <input type="number" min={1} max={12} step={1} className="w-20"
                value={Number(val("dream_hours") ?? 8)}
                onChange={(e) => setVal("dream_hours", Number(e.target.value))} />
              <span className="text-[13px]">hours</span>
            </Row>
            <div className="st-note">
              A night runs in rounds until its window closes: each round reflects again on what the night
              has found so far and goes deeper. While it dreams the orb sleeps and HELIX talks in its sleep —
              the murmurs drift past the orb, and are whispered aloud only when you're there to hear them.
            </div>
            <div className="st-note">Plans and drafts on Fable — the growth model{dream?.model ? ` (${dream.model})` : ""}.</div>
          </div>
        ),
      },
      {
        id: "dream-power", group: "dreaming", icon: "◑", title: "What it may do",
        what: "how far a night is allowed to go on its own", keys: "dream auto apply merge rebuild relaunch drafts round",
        body: (
          <div className="space-y-3">
            <Switch checked={Boolean(val("dream_auto_apply"))} onChange={(v) => setVal("dream_auto_apply", v)}>
              Apply green changes automatically
            </Switch>
            <div className="st-note warn">
              A drafted change merges on its own only after HELIX's full test suite passes on that exact
              branch; anything red waits for your review. Off, every draft waits for you.
            </div>
            <Switch checked={Boolean(val("dream_rebuild") ?? true)} onChange={(v) => setVal("dream_rebuild", v)}>
              Rebuild and relaunch after applying (the previous build is kept and restored if the new one fails)
            </Switch>
            <Row label="Drafts per round, at most">
              <input type="number" min={1} max={30} step={1} className="w-20"
                value={Number(val("dream_max_drafts") ?? 10)}
                onChange={(e) => setVal("dream_max_drafts", Number(e.target.value))} />
            </Row>
          </div>
        ),
      },
      {
        id: "dream-now", group: "dreaming", icon: "◒", title: "Tonight, and right now",
        what: "the session's status, and the buttons", keys: "dream now stop journal status report",
        body: (
          <div className="space-y-3">
            {dream?.frozen_without_source && (
              <div className="st-note err">
                Dreaming is frozen on this machine: this installed copy can't find the source repository it
                was built from, so it has nothing to draft against. With HELIX closed, set source_root (the
                repository path) and dev_python (its Python) in helix_settings.json, saved as plain UTF-8,
                then start it again.
              </div>
            )}
            {(dream?.status || liveDream?.line) && (
              <div className="text-[13px]" style={{ whiteSpace: "pre-line", color: dreamRunning ? "var(--working)" : "var(--muted)" }}>
                {dreamRunning ? "◐ " : ""}{dream?.status || liveDream?.line}
              </div>
            )}
            {dream?.report && <div className="st-note">Last report: {dream.report}</div>}
            <div className="flex items-center gap-3 flex-wrap">
              <button className="btn" onClick={dreamNow} disabled={dreamRunning}>Dream for 30 minutes now</button>
              {dreamRunning && <button className="btn btn-danger" onClick={dreamStop}>Stop dreaming</button>}
              <button className="btn" title="What the nights found, verified, tried and applied"
                onClick={() => navigate({ name: "dream" })}>◐ Dream journal</button>
              {dreamNote && <span className="text-xs" style={{ color: "var(--cyan)" }}>{dreamNote}</span>}
            </div>
          </div>
        ),
      },
      // ---------------------------------------------------------------- connections
      {
        id: "services", group: "connections", icon: "⇄", title: "Services",
        what: "what HELIX is connected to, and how to cut one loose",
        keys: "connections services remove " + connEntries.map(([, c]) => c.label).join(" "),
        body: (
          <div className="space-y-2">
            {connEntries.map(([sid, conn]) => (
              <div key={sid} className="flex items-center gap-3 text-[13px]">
                <span style={{ color: conn.set ? "var(--done)" : "var(--muted)" }}>●</span>
                <span className="flex-1">{conn.label}</span>
                {conn.set && <button className="btn text-xs" onClick={() => removeConnection(sid, conn.label)}>Remove</button>}
              </div>
            ))}
            <div className="st-note">Keys are never typed here — when something needs one, HELIX opens a secure panel just in time.</div>
          </div>
        ),
      },
      {
        id: "mail", group: "connections", icon: "✉", title: "Gmail",
        what: "read-only: HELIX can check your mail, never send", keys: "gmail mail email app password",
        body: (
          <div className="space-y-2">
            <Row label="Gmail address">
              <input placeholder="you@example.com" className="flex-1" value={gmailAddr} onChange={(e) => setGmailAddr(e.target.value)} />
              <Dot on={data.gmail.configured} />
            </Row>
            <Row label="App password" hint="16 characters, from Google">
              <input type="password" placeholder="xxxx xxxx xxxx xxxx" className="flex-1" value={gmailPw} onChange={(e) => setGmailPw(e.target.value)} />
            </Row>
          </div>
        ),
      },
      {
        id: "calendar", group: "connections", icon: "▤", title: "Calendar",
        what: "read-only: a private iCal feed", keys: "calendar ical ics url",
        body: (
          <Row label="Private iCal URL">
            <input type="password" placeholder="https://…/basic.ics" className="flex-1" value={calUrl} onChange={(e) => setCalUrl(e.target.value)} />
            <Dot on={data.calendar.configured} />
          </Row>
        ),
      },
      // ---------------------------------------------------------------- conversation & presence
      {
        id: "wake", group: "presence", icon: "◈", title: "Wake & listening",
        what: "how you get HELIX's attention", keys: "wake word voice input hands-free listening trust household",
        body: (
          <div className="space-y-3">
            <Row label="Wake word">
              <input placeholder="HELIX" value={String(val("wake_word") ?? "")} onChange={(e) => setVal("wake_word", e.target.value)} />
            </Row>
            <Switch checked={Boolean(val("voice_input_on"))} onChange={(v) => setVal("voice_input_on", v)}>
              Hands-free voice — always listening for the wake word
            </Switch>
            <Switch checked={Boolean(val("trust_household_voice"))} onChange={(v) => setVal("trust_household_voice", v)}>
              Single-user home — trust any voice
            </Switch>
          </div>
        ),
      },
      {
        id: "speak", group: "presence", icon: "◇", title: "Speaking up",
        what: "when HELIX talks without being asked", keys: "narration milestones talk while working proactive watchers speak",
        body: (
          <div className="space-y-3">
            <div className="st-choices">
              <Choice on={String(val("narration_mode") ?? "off") === "off"} title="Stay quiet while working"
                detail="Builds and long tasks finish in silence; the result speaks." pill="RECOMMENDED"
                onPick={() => setVal("narration_mode", "off")} />
              <Choice on={String(val("narration_mode") ?? "off") === "milestones"} title="Speak milestones out loud"
                detail="A sentence at each step of a build, as it happens." onPick={() => setVal("narration_mode", "milestones")} />
            </div>
            <Switch checked={Boolean(val("proactive_speech"))} onChange={(v) => setVal("proactive_speech", v)}>
              Let background watchers speak up out loud
            </Switch>
          </div>
        ),
      },
      {
        id: "files", group: "presence", icon: "▣", title: "Files on this PC",
        what: "reading is always on; writing is yours to allow", keys: "files write access disk folder",
        body: (
          <Switch checked={Boolean(val("file_write_access"))} onChange={(v) => setVal("file_write_access", v)}>
            Allow HELIX to write files on this PC (reading is always on)
          </Switch>
        ),
      },
      // ---------------------------------------------------------------- voice & look
      {
        id: "tts", group: "voice", icon: "♪", title: "HELIX's voice",
        what: "Google's Gemini voices, styled in plain words - a British butler, a Dubliner, mission control", keys: "voice tts accent speed rate neural google gemini british irish style",
        body: <VoiceCard val={val} setVal={setVal} edgeVoices={data.voices} />,
      },
      {
        id: "orb-body", group: "voice", icon: "◉", title: "The orb",
        what: "which body HELIX wears on Talk, and how it looks", keys: "orb body organism cell face storm star hue energy look creature",
        body: (
          <div className="space-y-3">
            <div className="st-choices">
              <Choice on={String(val("orb_style") || "star") === "star"} title="The star" detail="Brendan's plasma sphere with its rings - the original." onPick={() => setVal("orb_style", "star")} />
              <Choice on={String(val("orb_style") || "star") === "cell"} title="The avatar" detail="A holographic face that condenses out of falling code - it looks at you, its lips move when it speaks, and it pulses at the moments that matter." pill="NEW" onPick={() => setVal("orb_style", "cell")} />
            </div>
            {String(val("orb_style") || "star") === "cell" && (
              <>
                <Row label="Phase" hint="what the organism is doing">
                  <select value={String(val("orb_phase") || "face")} onChange={(e) => setVal("orb_phase", e.target.value)}>
                    <option value="face">Face - the avatar: eyes that follow you, lips that speak</option>
                    <option value="core">Core - the code sphere without the face</option>
                    <option value="storm">Storm - arcs race the code</option>
                  </select>
                </Row>
                <Row label={`Hue - ${Number(val("orb_hue") ?? 185)}°`} hint="shifts the whole palette">
                  <input type="range" min={0} max={360} step={1} className="accent-[#3fe0e0]" style={{ width: 260 }}
                    value={Number(val("orb_hue") ?? 185)} onChange={(e) => setVal("orb_hue", Number(e.target.value))} />
                  <span style={{ width: 22, height: 22, borderRadius: 99, background: `hsl(${Number(val("orb_hue") ?? 185)} 85% 55%)`, boxShadow: `0 0 12px hsl(${Number(val("orb_hue") ?? 185)} 85% 55%)` }} />
                </Row>
                <Row label={`Energy - ${Number(val("orb_energy") ?? 1).toFixed(1)}×`} hint="its tempo">
                  <input type="range" min={0.3} max={2.5} step={0.1} className="accent-[#3fe0e0]" style={{ width: 260 }}
                    value={Number(val("orb_energy") ?? 1)} onChange={(e) => setVal("orb_energy", Number(e.target.value))} />
                </Row>
                <div className="st-note">Next rung: a shape it can be asked to take ("be a cat") - needs a shape generator; the face phase is the first step toward it.</div>
              </>
            )}
          </div>
        ),
      },
      {
        id: "performance", group: "voice", icon: "⚡", title: "Performance",
        what: "how much of the art this PC draws - Auto steps down on its own when frames run long",
        keys: "performance speed fps slow lag frame rate quality lean minimal auto",
        body: (
          <div className="space-y-3">
            <div className="flex gap-2 flex-wrap">
              {([["auto", "Auto", "measures the frame time; steps down when it is slow, back up when it is not"], ["full", "Full", "1.5x pixels on the faces, the whole lattice, every glow, the backdrop"], ["lean", "Lean", "1.25x pixels, a thinner lattice, no glow halos, 2D at 30 fps"], ["minimal", "Minimal", "1x pixels, half the lattice, no backdrop scene, 2D at 20 fps"]] as [Mode, string, string][]).map(([m, label, d]) => (
                <Choice key={m} on={perfMode === m} title={label} detail={d} onPick={() => { setMode(m); setPerfMode(m); }} />
              ))}
            </div>
            <div className="st-note">Right now: {perfLine}. Settings &gt; Overview &gt; Show the frame rate puts the live numbers under the menu.</div>
            <div className="st-note">{perf.software ? `No usable graphics card here (${perf.renderer || "software rendering"}) - every 3D frame is CPU work, so Auto starts at Minimal on this PC.` : `Graphics: ${perf.renderer || "hardware"}. Below Full, the 3D faces and helices draw at 30 / 20 frames a second.`}</div>
          </div>
        ),
      },
      {
        id: "helix-art", group: "voice", icon: "⬡", title: "The helix",
        what: "the two strand colors behind the Console", keys: "helix colors art strand console background theme",
        body: (
          <div className="space-y-3">
            <Row label="Strand A" hint="the first strand and the signals">
              <input type="color" value={String(val("helix_color_a") || "#3fe0e0")} onChange={(e) => setVal("helix_color_a", e.target.value)} style={{ width: 54, height: 34, padding: 2 }} />
              <span className="board-chip dim" style={{ fontFamily: "monospace", fontSize: 12 }}>{String(val("helix_color_a") || "#3fe0e0")}</span>
            </Row>
            <Row label="Strand B" hint="the second strand">
              <input type="color" value={String(val("helix_color_b") || "#2a8cff")} onChange={(e) => setVal("helix_color_b", e.target.value)} style={{ width: 54, height: 34, padding: 2 }} />
              <span className="board-chip dim" style={{ fontFamily: "monospace", fontSize: 12 }}>{String(val("helix_color_b") || "#2a8cff")}</span>
            </Row>
            <div className="flex gap-2 flex-wrap">
              {([["HELIX", "#3fe0e0", "#2a8cff"], ["Ember", "#ff7a3d", "#ffc857"], ["Aurora", "#3fe07a", "#7c5cff"], ["Rose", "#ff5d8f", "#ffb3c7"], ["Ice", "#bffcfc", "#5aa7ff"]] as const).map(([n, a, b]) => (
                <button key={n} className="btn text-xs" onClick={() => { setVal("helix_color_a", a); setVal("helix_color_b", b); }}>
                  <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 99, background: a, marginRight: 4 }} />
                  <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 99, background: b, marginRight: 8 }} />{n}
                </button>
              ))}
            </div>
            <div className="st-note">Saved with the rest; the Console picks the new colors up the next time it opens.</div>
          </div>
        ),
      },
      {
        id: "holo", group: "voice", icon: "◬", title: "Holograms",
        what: "how much detail a model is rendered with", keys: "hologram detail model render quality balanced high",
        body: (
          <div className="st-choices">
            <Choice on={String(val("model_detail") ?? "balanced") === "balanced"} title="Balanced"
              detail="Smooth on any machine; the detail most models need." pill="DEFAULT"
              onPick={() => setVal("model_detail", "balanced")} />
            <Choice on={String(val("model_detail") ?? "balanced") === "high"} title="High"
              detail="Every edge; costs more to bake and to draw." onPick={() => setVal("model_detail", "high")} />
          </div>
        ),
      },
      // ---------------------------------------------------------------- camera
      {
        id: "cam", group: "camera", icon: "◎", title: "Which camera",
        what: "the one the panel opens with", keys: "camera device webcam preferred",
        body: (
          <div className="space-y-2">
            <Row label="Preferred camera">
              <select value={String(val("camera_device") ?? "")} onChange={(e) => setVal("camera_device", e.target.value)}>
                <option value="">Any camera (the panel remembers your last pick)</option>
                {cameras.map((c) => <option key={c} value={c}>{c}</option>)}
                {Boolean(val("camera_device")) && !cameras.includes(String(val("camera_device"))) && (
                  <option value={String(val("camera_device"))}>{String(val("camera_device"))}</option>
                )}
              </select>
            </Row>
            {cameras.length === 0 && <div className="st-note">Open the camera once on the Console to list them by name.</div>}
          </div>
        ),
      },
      {
        id: "cam-behave", group: "camera", icon: "◉", title: "How it behaves",
        what: "mirroring, clips, and what a typed message carries", keys: "camera mirror clip length seconds attach view selfie",
        body: (
          <div className="space-y-3">
            <Switch checked={Boolean(val("camera_mirror"))} onChange={(v) => setVal("camera_mirror", v)}>
              Mirror the preview (selfie style — off keeps board markings readable)
            </Switch>
            <Switch checked={Boolean(val("camera_attach_view") ?? true)} onChange={(v) => setVal("camera_attach_view", v)}>
              While the camera is open, typed messages carry the current view
            </Switch>
            <Row label={`Clip length — ${Number(val("camera_clip_seconds") ?? 6)}s`}>
              <input type="range" min={2} max={15} step={1} className="accent-[#3fe0e0]"
                value={Number(val("camera_clip_seconds") ?? 6)} onChange={(e) => setVal("camera_clip_seconds", Number(e.target.value))} />
            </Row>
            <div className="st-note">
              The 📷 button by the chat line opens the panel; ⇄ in the panel cycles cameras. HELIX
              looks through it on its own once it's open, draws callouts over what it sees, and
              can project your holograms onto the real board.
            </div>
          </div>
        ),
      },
      // ---------------------------------------------------------------- power
      {
        id: "quit", group: "power", icon: "⏻", title: "Quit HELIX",
        what: "stop it, and what closing the tab does", keys: "quit stop shutdown power exit close tab window last",
        body: (
          <div className="space-y-3">
          <div className="flex items-center gap-4 flex-wrap">
            <button className="btn btn-danger" onClick={() => {
              if (!window.confirm("Quit HELIX? Watchers, reminders and voice stop until you launch it again.")) return;
              void api.post("/api/shell/quit").catch(() => undefined);
              window.setTimeout(() => window.dispatchEvent(new CustomEvent("helix-off", { detail: { reason: "You quit HELIX from Settings." } })), 600);
            }}>⏻ Quit HELIX</button>
            <span className="st-note">Shuts down the backend and everything it started; the desktop icon starts it again.</span>
          </div>
          <Switch checked={Boolean(val("quit_when_closed") ?? true)} onChange={(v) => setVal("quit_when_closed", v)}>
            Stop HELIX when the last tab or window closes
          </Switch>
          <div className="st-note">Off, HELIX keeps running in the background for watchers, reminders and voice after you close the tab.</div>
          </div>
        ),
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, edits, secretEdits, gmailAddr, gmailPw, calUrl, cameras, dream, dreamNote, liveDream, dreamRunning, face, building, restarting, buildNote, tools, toolEdits, toolNote, perfMode, perfLine]);

  if (!data) return null;

  // ------------------------------------------------------------------ search + overview
  const query = q.trim().toLowerCase();
  const hits = query
    ? sections.filter((s) => `${s.title} ${s.what} ${s.keys}`.toLowerCase().includes(query))
    : [];
  const hitGroups = new Set(hits.map((s) => s.group));
  const groupLabel = (g: GroupId) => GROUPS.find((x) => x.id === g)?.title ?? g;

  const connSet = Object.values(data.connections).filter((c) => c.set).length;
  const connAll = Object.keys(data.connections).length;
  const brainOn = data.secrets.claude_code_oauth_token || data.secrets.claude_api_key;
  const updatesWaiting = Boolean(face?.face.stale || face?.backend.stale);
  const needs: Partial<Record<GroupId, boolean>> = {
    updates: updatesWaiting,
    brain: !brainOn,
    board: !data.secrets.github_token || Boolean(tools && !tools.ok),
  };
  const toolsMissing = tools ? tools.needs.length : 0;
  const cards: { g: GroupId; k: string; v: string; d: string; color?: string; needs?: boolean }[] = [
    { g: "updates", k: "Updates", v: face === null ? "Unknown" : updatesWaiting ? (face.face.stale && face.backend.stale ? "Face + backend" : face.face.stale ? "Face to build" : "Backend to restart") : "Up to date",
      d: updatesWaiting ? "changes on disk are not running yet" : "what you see is what is on disk",
      color: updatesWaiting ? "var(--working)" : "var(--done)", needs: updatesWaiting },
    { g: "brain", k: "The brain", v: data.secrets.claude_code_oauth_token ? "Subscription" : data.secrets.claude_api_key ? "API key" : "Not set",
      d: data.brain.line, color: brainOn ? "var(--done)" : "var(--working)", needs: !brainOn },
    { g: "board", k: "The Board", v: toolsMissing ? `${toolsMissing} tool${toolsMissing === 1 ? "" : "s"} to fix` : data.secrets.github_token ? "GitHub linked" : "GitHub token needed",
      d: toolsMissing ? tools!.tools.filter((r) => !r.ok).map((r) => r.label).join(", ") : data.secrets.github_token ? "drift is judged against each repo's HEAD" : "without it the Board reads Cloud Run only — no drift",
      color: data.secrets.github_token && !toolsMissing ? "var(--done)" : "var(--working)", needs: !data.secrets.github_token || toolsMissing > 0 },
    { g: "dreaming", k: "Dreaming", v: dreamRunning ? "Dreaming now" : Boolean(val("dream_enabled")) ? `Nightly at ${String(val("dream_start") ?? "23:00")}` : "Off",
      d: dream?.status || liveDream?.line || (Boolean(val("dream_enabled")) ? `${Number(val("dream_hours") ?? 8)} hours, ${Boolean(val("dream_auto_apply")) ? "applies green changes itself" : "every draft waits for you"}` : "no night sessions"),
      color: dreamRunning ? "var(--working)" : Boolean(val("dream_enabled")) ? "var(--cyan)" : undefined },
    { g: "connections", k: "Connections", v: `${connSet} of ${connAll}`, d: connSet ? "services HELIX can reach" : "nothing connected yet", color: connSet ? "var(--done)" : undefined },
    { g: "connections", k: "Mail & calendar", v: data.gmail.configured ? (data.calendar.configured ? "Both set" : "Gmail only") : (data.calendar.configured ? "Calendar only" : "Not set"),
      d: "read-only, never sends", color: data.gmail.configured || data.calendar.configured ? "var(--done)" : undefined },
    { g: "presence", k: "Conversation", v: Boolean(val("voice_input_on")) ? "Hands-free" : "Push to talk",
      d: `wake word "${String(val("wake_word") || "HELIX")}" · ${String(val("narration_mode") ?? "off") === "off" ? "quiet while working" : "speaks milestones"}` },
    { g: "voice", k: "Voice & look", v: String(val("tts_engine") ?? "google") === "google" ? `${String(val("tts_google_voice") ?? "Charon")} · ${String(val("tts_style") ?? "butler")}` : String(val("tts_voice") ?? "en-GB-RyanNeural").replace(/Neural$/, ""),
      d: `${Number(val("tts_rate") ?? 1).toFixed(1)}× · holograms ${String(val("model_detail") ?? "balanced")} · helix ${String(val("helix_color_a") || "#3fe0e0")}` },
    { g: "camera", k: "Camera", v: String(val("camera_device") || "Any camera"), d: `${Number(val("camera_clip_seconds") ?? 6)}s clips · ${Boolean(val("camera_mirror")) ? "mirrored" : "not mirrored"}` },
    { g: "power", k: "Power", v: "Running", d: "quit from here; the icon starts it again", color: "var(--done)" },
  ];

  const toggle = (id: string) => setClosed((c) => ({ ...c, [id]: !c[id] }));
  const shown = query ? hits : sections.filter((s) => s.group === group);
  const active = GROUPS.find((g) => g.id === group)!;

  return (
    <div className="st-stage" style={{ pointerEvents: "auto" }}>
      <div className="st-hud" />
      <div className="h-full flex flex-col pt-14 relative">
        {/* header */}
        <div className="flex items-center gap-4 px-6 py-3 flex-wrap" style={{ borderBottom: "1px solid color-mix(in srgb, var(--cyan) 12%, var(--line))" }}>
          <span className="text-glow-cyan text-[20px]" style={{ color: "var(--cyan)" }}>⚙</span>
          <div>
            <div className="st-title">SETTINGS</div>
            <div className="st-tag">everything about how HELIX looks and behaves</div>
          </div>
          <div className="flex-1" />
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--muted)" }}>⌕</span>
            <input className="st-search" placeholder="find a setting…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        </div>

        <div className="flex-1 flex min-h-0">
          {/* the rail */}
          <nav className="st-rail overflow-y-auto">
            {GROUPS.map((g) => (
              <button key={g.id} type="button" className={`st-item${group === g.id && !query ? " on" : ""}`}
                onClick={() => { setGroup(g.id); setQ(""); }}>
                <span className="ic">{g.icon}</span>
                <span className="min-w-0">
                  <div className="t">{g.title.toUpperCase()}</div>
                  <div className="s">{g.sub}</div>
                </span>
                {query && hitGroups.has(g.id as Exclude<GroupId, "overview">) && <span className="badge">match</span>}
                {!query && needs[g.id] && <span className="badge">needs you</span>}
              </button>
            ))}
          </nav>

          {/* the pane */}
          <div className="st-pane">
            {query ? (
              <>
                <div className="st-lead mb-3">
                  {hits.length ? <><b>{hits.length}</b> setting{hits.length === 1 ? "" : "s"} match "{q}"</> : <>Nothing matches "{q}".</>}
                </div>
                {hits.map((s) => (
                  <SectionView key={s.id} s={s} open={!closed[s.id]} toggle={() => toggle(s.id)} hit groupLabel={groupLabel(s.group)} />
                ))}
              </>
            ) : group === "overview" ? (
              <>
                <div className="st-lead mb-4">
                  This is <b>HELIX</b> on your machine. Everything here is yours alone — nothing you change
                  affects anyone else. Click any card to jump to it.
                </div>
                <div className="mb-4">
                  <Switch checked={fpsOn} onChange={toggleFps}>
                    Show the frame rate — a small badge under the menu with the live fps and the level the art is drawing at ({perfLine || "measuring…"})
                  </Switch>
                </div>
                <div className="st-cards">
                  {cards.map((c, i) => (
                    <button key={c.k} type="button" className={`st-card materialize${c.needs ? " needs" : ""}`}
                      style={{ "--i": i, ...(c.color ? { "--card-color": c.color } : {}) } as React.CSSProperties}
                      onClick={() => setGroup(c.g)}>
                      <div className="k">{c.k}</div>
                      <div className="v">{c.v}</div>
                      <div className="d">{c.d}</div>
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <>
                <div className="st-group">
                  <span className="ic">{active.icon}</span>
                  <div>
                    <div className="t">{active.title.toUpperCase()}</div>
                    <div className="s">{active.sub}</div>
                  </div>
                  <span className="n">{shown.length} SECTION{shown.length === 1 ? "" : "S"}</span>
                </div>
                {shown.map((s) => (
                  <SectionView key={s.id} s={s} open={!closed[s.id]} toggle={() => toggle(s.id)} />
                ))}
              </>
            )}
            {note && <div className="text-[13px] mt-4" style={{ color: "var(--cyan)" }}>{note}</div>}
          </div>
        </div>

        {/* the save bar */}
        <div className="st-savebar">
          {dirty > 0 && <span className="st-dirty">{dirty} unsaved change{dirty === 1 ? "" : "s"}</span>}
          <div className="glass rounded-2xl px-4 py-3 flex gap-3">
            <button className="btn" onClick={() => navigate({ name: "console" })}>Cancel</button>
            <button className="btn btn-primary px-8" onClick={save} disabled={dirty === 0}>Save</button>
          </div>
        </div>
      </div>
    </div>
  );
}
