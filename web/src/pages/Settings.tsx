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

  const load = useCallback(() => {
    void api.get<SettingsData>("/api/settings").then((d) => {
      setData(d);
      setEdits({});
      setSecretEdits({});
      setGmailAddr(d.gmail.address || "");
    });
  }, []);
  useEffect(load, [load]);

  const val = (key: string): unknown => (key in edits ? edits[key] : data?.values[key]);
  const setVal = (key: string, v: unknown) => setEdits((e) => ({ ...e, [key]: v }));

  const save = () => {
    const values: Record<string, unknown> = { ...edits };
    for (const [k, v] of Object.entries(secretEdits)) if (v.trim()) values[k] = v.trim();
    const body: Record<string, unknown> = { values };
    if (gmailAddr.trim() && gmailPw.trim()) body.gmail = { address: gmailAddr.trim(), password: gmailPw.trim() };
    if (calUrl.trim()) body.calendar_url = calUrl.trim();
    void api.put("/api/settings", body).then(() => {
      setNote("Saved.");
      setGmailPw("");
      setCalUrl("");
      load();
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
            <label className="flex items-center gap-2 text-[13px]">
              <input type="checkbox" checked={Boolean(val("evolve_enabled") ?? true)}
                onChange={(e) => setVal("evolve_enabled", e.target.checked)} />
              Evolve — draft one self-improvement overnight (never applies itself)
            </label>
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
