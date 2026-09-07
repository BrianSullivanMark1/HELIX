// Settings — credentials (write-only, presence dots), the brain status line, connections review,
// files, conversation & presence, voice. Save writes only what changed.
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

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

function Dot({ on }: { on: boolean }) {
  return (
    <span className="text-xs" style={{ color: on ? "var(--done)" : "var(--muted)" }}>
      {on ? "● Set" : "○ Not set"}
    </span>
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

  if (!data) return null;
  const brainColor = data.brain.tone === "ok" ? "var(--done)"
    : data.brain.tone === "warn" ? "var(--working)" : "var(--muted)";

  return (
    <div className="h-full overflow-y-auto pt-16 px-8 pb-28" style={{ pointerEvents: "auto" }}>
      <div className="max-w-[760px] mx-auto space-y-6">
        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">HELIX</div>
          <div className="space-y-3">
            <div>
              <div className="flex justify-between mb-1 text-[13px]">
                <span>Claude subscription token <span style={{ color: "var(--muted)" }}>(recommended — `claude setup-token`)</span></span>
                <Dot on={data.secrets.claude_code_oauth_token} />
              </div>
              <input type="password" placeholder="sk-ant-oat01-…" className="w-full"
                value={secretEdits.claude_code_oauth_token ?? ""}
                onChange={(e) => setSecretEdits((s) => ({ ...s, claude_code_oauth_token: e.target.value }))} />
            </div>
            <div>
              <div className="flex justify-between mb-1 text-[13px]">
                <span>Claude API key <span style={{ color: "var(--muted)" }}>(metered fallback)</span></span>
                <Dot on={data.secrets.claude_api_key} />
              </div>
              <input type="password" placeholder="sk-ant-…" className="w-full"
                value={secretEdits.claude_api_key ?? ""}
                onChange={(e) => setSecretEdits((s) => ({ ...s, claude_api_key: e.target.value }))} />
            </div>
            <div className="text-[13px]" style={{ color: brainColor }}>{data.brain.line}</div>
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Dreaming</div>
          <div className="space-y-3 text-[13px]">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("dream_enabled"))}
                onChange={(e) => setVal("dream_enabled", e.target.checked)} />
              Dream nightly — a session of non-stop self-improvement while you sleep
            </label>
            <div className="flex items-center gap-3">
              <span className="w-44">Window</span>
              <span>from</span>
              <input type="time" value={String(val("dream_start") ?? "23:00")}
                onChange={(e) => setVal("dream_start", e.target.value)} />
              <span>for</span>
              <input type="number" min={1} max={12} step={1} className="w-20"
                value={Number(val("dream_hours") ?? 8)}
                onChange={(e) => setVal("dream_hours", Number(e.target.value))} />
              <span>hours</span>
            </div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("dream_auto_apply"))}
                onChange={(e) => setVal("dream_auto_apply", e.target.checked)} />
              Apply green changes automatically
            </label>
            <div className="text-xs pl-6" style={{ color: "var(--working)" }}>
              A drafted change merges on its own only after HELIX's full test suite passes on that exact
              branch; anything red waits for your review. Off, every draft waits for you.
            </div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("dream_rebuild") ?? true)}
                onChange={(e) => setVal("dream_rebuild", e.target.checked)} />
              Rebuild and relaunch after applying (the previous build is kept and restored if the new one fails)
            </label>
            <div className="flex items-center gap-3">
              <span className="w-44">Drafts per round, at most</span>
              <input type="number" min={1} max={30} step={1} className="w-20"
                value={Number(val("dream_max_drafts") ?? 10)}
                onChange={(e) => setVal("dream_max_drafts", Number(e.target.value))} />
            </div>
            <div className="text-xs pl-6" style={{ color: "var(--muted)" }}>
              A night runs in rounds until its window closes: each round reflects again on what the night
              has found so far and goes deeper. While it dreams the orb sleeps and HELIX talks in its sleep —
              the murmurs drift past the orb, and are whispered aloud only when you're there to hear them.
            </div>
            <div style={{ color: "var(--muted)" }}>
              Plans and drafts on Fable — the growth model{dream?.model ? ` (${dream.model})` : ""}.
            </div>
            {dream?.frozen_without_source && (
              <div style={{ color: "var(--error)" }}>
                Dreaming is frozen on this machine: this installed copy can't find the source repository it
                was built from, so it has nothing to draft against. With HELIX closed, set source_root (the
                repository path) and dev_python (its Python) in helix_settings.json, saved as plain UTF-8,
                then start it again.
              </div>
            )}
            {(dream?.status || liveDream?.line) && (
              <div style={{ whiteSpace: "pre-line", color: dreamRunning ? "var(--working)" : "var(--muted)" }}>
                {dreamRunning ? "◐ " : ""}{dream?.status || liveDream?.line}
              </div>
            )}
            {dream?.report && (
              <div style={{ color: "var(--muted)" }}>Last report: {dream.report}</div>
            )}
            <div className="flex items-center gap-3 flex-wrap">
              <button className="btn" onClick={dreamNow} disabled={dreamRunning}>Dream for 30 minutes now</button>
              {dreamRunning && (
                <button className="btn btn-danger" onClick={dreamStop}>Stop dreaming</button>
              )}
              <button className="btn" title="What the nights found, verified, tried and applied"
                onClick={() => navigate({ name: "dream" })}>
                ◐ Dream journal
              </button>
              {dreamNote && <span className="text-xs" style={{ color: "var(--cyan)" }}>{dreamNote}</span>}
            </div>
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Connections</div>
          <div className="space-y-2">
            {Object.entries(data.connections).map(([sid, conn]) => (
              <div key={sid} className="flex items-center gap-3 text-[13px]">
                <span style={{ color: conn.set ? "var(--done)" : "var(--muted)" }}>●</span>
                <span className="flex-1">{conn.label}</span>
                {conn.set && (
                  <button className="btn text-xs" onClick={() => removeConnection(sid, conn.label)}>
                    Remove
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="text-xs mt-3" style={{ color: "var(--muted)" }}>
            Keys are never typed here — when something needs one, HELIX opens a secure panel just in time.
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Gmail & Calendar — read-only</div>
          <div className="grid grid-cols-2 gap-3">
            <input placeholder="Gmail address" value={gmailAddr}
              onChange={(e) => setGmailAddr(e.target.value)} />
            <input type="password" placeholder="16-character app password" value={gmailPw}
              onChange={(e) => setGmailPw(e.target.value)} />
          </div>
          <div className="mt-3">
            <div className="flex justify-between mb-1 text-[13px]">
              <span>Private iCal URL</span>
              <Dot on={data.calendar.configured} />
            </div>
            <input type="password" placeholder="https://…/basic.ics" className="w-full" value={calUrl}
              onChange={(e) => setCalUrl(e.target.value)} />
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Conversation & presence</div>
          <div className="space-y-3 text-[13px]">
            <div className="flex items-center gap-3">
              <span className="w-44">Wake word</span>
              <input placeholder="HELIX" value={String(val("wake_word") ?? "")}
                onChange={(e) => setVal("wake_word", e.target.value)} />
            </div>
            <div className="flex items-center gap-3">
              <span className="w-44">Talk while working</span>
              <select value={String(val("narration_mode") ?? "off")}
                onChange={(e) => setVal("narration_mode", e.target.value)}>
                <option value="off">Stay quiet while working (recommended)</option>
                <option value="milestones">Speak milestones out loud</option>
              </select>
            </div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("voice_input_on"))}
                onChange={(e) => setVal("voice_input_on", e.target.checked)} />
              Hands-free voice — always listening for the wake word
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("proactive_speech"))}
                onChange={(e) => setVal("proactive_speech", e.target.checked)} />
              Let background watchers speak up out loud
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("trust_household_voice"))}
                onChange={(e) => setVal("trust_household_voice", e.target.checked)} />
              Single-user home — trust any voice
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("file_write_access"))}
                onChange={(e) => setVal("file_write_access", e.target.checked)} />
              Allow HELIX to write files on this PC (reading is always on)
            </label>
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Appearance & voice</div>
          <div className="space-y-3 text-[13px]">
            <div className="flex items-center gap-3">
              <span className="w-44">Hologram detail</span>
              <select value={String(val("model_detail") ?? "balanced")}
                onChange={(e) => setVal("model_detail", e.target.value)}>
                <option value="balanced">Balanced</option>
                <option value="high">High</option>
              </select>
            </div>
            <div className="flex items-center gap-3">
              <span className="w-44">HELIX's voice</span>
              <select value={String(val("tts_voice") ?? "en-GB-RyanNeural")}
                onChange={(e) => setVal("tts_voice", e.target.value)}>
                {data.voices.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </div>
            <div className="flex items-center gap-3">
              <span className="w-44">Speed — {Number(val("tts_rate") ?? 1).toFixed(1)}×</span>
              <input type="range" min={0.8} max={2.0} step={0.1} className="accent-[#3fe0e0]"
                value={Number(val("tts_rate") ?? 1)}
                onChange={(e) => setVal("tts_rate", Number(e.target.value))} />
            </div>
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Camera</div>
          <div className="space-y-3 text-[13px]">
            <div className="flex items-center gap-3">
              <span className="w-44">Preferred camera</span>
              <select value={String(val("camera_device") ?? "")}
                onChange={(e) => setVal("camera_device", e.target.value)}>
                <option value="">Any camera (the panel remembers your last pick)</option>
                {cameras.map((c) => <option key={c} value={c}>{c}</option>)}
                {Boolean(val("camera_device")) && !cameras.includes(String(val("camera_device"))) && (
                  <option value={String(val("camera_device"))}>{String(val("camera_device"))}</option>
                )}
              </select>
              {cameras.length === 0 && (
                <span className="text-xs" style={{ color: "var(--muted)" }}>
                  Open the camera once on the Console to list them by name.
                </span>
              )}
            </div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("camera_mirror"))}
                onChange={(e) => setVal("camera_mirror", e.target.checked)} />
              Mirror the preview (selfie style — off keeps board markings readable)
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={Boolean(val("camera_attach_view") ?? true)}
                onChange={(e) => setVal("camera_attach_view", e.target.checked)} />
              While the camera is open, typed messages carry the current view
            </label>
            <div className="flex items-center gap-3">
              <span className="w-44">Clip length — {Number(val("camera_clip_seconds") ?? 6)}s</span>
              <input type="range" min={2} max={15} step={1} className="accent-[#3fe0e0]"
                value={Number(val("camera_clip_seconds") ?? 6)}
                onChange={(e) => setVal("camera_clip_seconds", Number(e.target.value))} />
            </div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>
              The 📷 button by the chat line opens the panel; ⇄ in the panel cycles cameras. HELIX
              looks through it on its own once it's open, draws callouts over what it sees, and
              can project your holograms onto the real board.
            </div>
          </div>
        </section>

        <section className="glass rounded-2xl p-5">
          <div className="section-title mb-4">Power</div>
          <div className="flex items-center gap-4">
            <button className="btn btn-danger" onClick={() => {
              if (!window.confirm("Quit HELIX? Watchers, reminders and voice stop until you launch it again.")) return;
              void api.post("/api/shell/quit").catch(() => undefined);
              setNote("HELIX is shutting down — this tab can be closed.");
            }}>⏻ Quit HELIX</button>
            <span className="text-xs" style={{ color: "var(--muted)" }}>
              Closing the browser tab does not stop HELIX — it keeps working in the background.
              This button shuts it down fully; the desktop icon starts it again.
            </span>
          </div>
        </section>

        {note && <div className="text-[13px]" style={{ color: "var(--cyan)" }}>{note}</div>}
      </div>

      <div className="fixed bottom-0 left-0 right-0 p-4 flex justify-center" style={{ zIndex: 20 }}>
        <div className="glass rounded-2xl px-4 py-3 flex gap-3">
          <button className="btn" onClick={() => navigate({ name: "console" })}>Cancel</button>
          <button className="btn btn-primary px-8" onClick={save}>Save</button>
        </div>
      </div>
    </div>
  );
}
