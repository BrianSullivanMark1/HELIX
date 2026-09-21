// THE VAULT (Brian, 2026-09-22) - the company's secrets, the way THE FORGE kept its Databricks
// scopes, on Google Cloud Secret Manager: one window, two panes. Left, every secret grouped by the
// app it is labelled for, with its expiry pill. Right, the one you picked: its versions, who reads
// it (a grep of the linked folders), Rotate, its rotation rule, and - behind its own fold - Delete,
// which takes the name typed back, a tick that you know what breaks, Are you sure, and a second
// tick when something still reads it. Values go in through a password field and never come back.
// Opened from the Console's Vault tab, from Settings, or by the `helix-vault` event.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import "./vault.css";

interface Version { number: number; state: string; created: string }
interface Row {
  name: string; created: string; labels: Record<string, string>; app: string; env: string;
  latest: Version | null; versions_n: number; age_days: number | null; policy_days: number | null; explicit: boolean;
  due_days: number | null; state: "fine" | "soon" | "overdue" | "never" | "unknown";
}
interface Board { identity: string | null; may_delete: boolean; default_days: number; warn_days: number; secrets: Row[]; problem: string | null; project: string }
interface Reader { app: string; file: string; line: number }

const APPS = ["MES", "WMS", "MRP", "ECHO"];
const ENVS = ["dev", "qa", "prod", "all"];
const APP_HUE: Record<string, number> = { MES: 188, WMS: 268, MRP: 140, ECHO: 36 };
const ENV_HUE: Record<string, number> = { dev: 188, qa: 42, prod: 8, all: 200 };

function ago(iso: string): string {
  const d = Math.round((Date.now() - Date.parse(iso)) / 86400000);
  if (!isFinite(d)) return "";
  return d <= 0 ? "today" : d === 1 ? "yesterday" : d < 60 ? `${d} d ago` : `${Math.round(d / 30)} mo ago`;
}

export function pillText(r: Pick<Row, "state" | "due_days" | "age_days">): string {
  if (r.state === "never") return "never expires";
  if (r.state === "unknown") return "age unknown";
  if (r.state === "overdue") return `${-(r.due_days ?? 0)} d overdue`;
  return `~${r.due_days} d left`;
}

function Pill({ r }: { r: Pick<Row, "state" | "due_days" | "age_days"> }) {
  return <span className={`vault-pill ${r.state}`}>{pillText(r)}</span>;
}
function Chip({ text, hue }: { text: string; hue: number }) {
  return <span className="vault-chip" style={{ "--h": hue } as React.CSSProperties}>{text}</span>;
}

/** A password field with its own eye: what YOU type you may look at; a stored value never shows. */
function ValueField({ value, onChange, placeholder, autoFocus }: { value: string; onChange: (v: string) => void; placeholder: string; autoFocus?: boolean }) {
  const [show, setShow] = useState(false);
  return (
    <div className="vault-value">
      <input type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} autoFocus={autoFocus} autoComplete="off" spellCheck={false} />
      <button type="button" className="vault-eye" onClick={() => setShow((s) => !s)} data-tip={show ? "Hide what you typed" : "Show what you typed"}>{show ? "◉" : "◎"}</button>
    </div>
  );
}

export default function VaultWindow({ onClose }: { onClose: () => void }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    setFailed(null);
    void api.get<Board>("/api/secrets").then((b) => { setBoard(b); }).catch((e: Error) => setFailed(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const rows = useMemo(() => (board?.secrets ?? []).filter((r) => !q || `${r.name} ${r.app} ${r.env}`.toLowerCase().includes(q.toLowerCase())), [board, q]);
  const groups = useMemo(() => {
    const by = new Map<string, Row[]>();
    for (const r of rows) { const k = r.app || "SHARED"; if (!by.has(k)) by.set(k, []); by.get(k)!.push(r); }
    const order = [...APPS, "SHARED", ...[...by.keys()].filter((k) => !APPS.includes(k) && k !== "SHARED").sort()];
    return order.filter((k) => by.has(k)).map((k) => [k, by.get(k)!] as const);
  }, [rows]);
  const chosen = board?.secrets.find((r) => r.name === picked) ?? null;
  const overdue = (board?.secrets ?? []).filter((r) => r.state === "overdue").length;
  const soon = (board?.secrets ?? []).filter((r) => r.state === "soon").length;
  return (
    <div className="vault-wrap" onClick={onClose}>
      <div className="vault-win" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="The vault">
        <i className="corners" aria-hidden="true" />
        <div className="vault-head">
          <span className="vault-lock" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="11" width="14" height="10" rx="2" /><path className="shackle" d="M8 11V7.5a4 4 0 0 1 8 0V11" /><circle cx="12" cy="16" r="1.5" fill="currentColor" stroke="none" /></svg>
          </span>
          <div className="min-w-0">
            <div className="vault-kicker">THE COMPANY'S SECRETS · {board?.project || "Google Cloud"}{board?.identity ? ` · ${board.identity}` : ""}</div>
            <div className="vault-title">THE VAULT</div>
          </div>
          <div className="vault-stats">
            {board && <><b>{board.secrets.length}</b> secret{board.secrets.length === 1 ? "" : "s"}{overdue ? <> · <span className="overdue">{overdue} overdue</span></> : null}{soon ? <> · <span className="soon">{soon} due soon</span></> : null}</>}
          </div>
          <div className="flex-1" />
          <button className="btn btn-primary text-xs" onClick={() => { setCreating(true); setPicked(null); }} data-tip="A new secret - name, value, which app and environment read it">＋ New secret</button>
          <button className="btn text-xs" onClick={load} data-tip="Read the vault again">⟳</button>
          <button className="btn text-xs" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {(failed || board?.problem) && <div className="vault-problem">{failed || board?.problem}</div>}
        <div className="vault-body">
          <div className="vault-list">
            <input className="vault-search" placeholder="Find a secret…" value={q} onChange={(e) => setQ(e.target.value)} />
            {board === null && !failed && <div className="vault-empty">Opening the vault…</div>}
            {board && board.secrets.length === 0 && !board.problem && <div className="vault-empty">The vault is empty. New secret puts the first one in.</div>}
            {groups.map(([app, list]) => (
              <div key={app} className="vault-group">
                <div className="vault-group-head"><Chip text={app} hue={APP_HUE[app] ?? 200} /><span className="vault-group-n">{list.length}</span></div>
                {list.map((r) => (
                  <button key={r.name} className={`vault-row${picked === r.name ? " on" : ""}`} onClick={() => { setPicked(r.name); setCreating(false); }}>
                    <span className="vault-row-name elide">{r.name}</span>
                    {r.env && <Chip text={r.env.toUpperCase()} hue={ENV_HUE[r.env] ?? 200} />}
                    <Pill r={r} />
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="vault-pane">
            {creating && <NewSecret busy={busy} setBusy={setBusy} onDone={(name) => { setCreating(false); load(); setPicked(name); }} onCancel={() => setCreating(false)} />}
            {!creating && chosen && board && <SecretPane key={chosen.name} row={chosen} board={board} busy={busy} setBusy={setBusy} onChanged={load} onGone={() => { setPicked(null); load(); }} />}
            {!creating && !chosen && (
              <div className="vault-hint">
                <div className="vault-hint-title">Pick a secret on the left</div>
                <div>Every app reads its secrets from here when its container starts. A rotation is a new version - the old ones stay, disabled or not, so nothing is ever lost by rotating. Values never show, not even to you: what you can do is put a new one in.</div>
                <div className="vault-legend"><Pill r={{ state: "fine", due_days: 60, age_days: 30 }} /> fine · <Pill r={{ state: "soon", due_days: 12, age_days: 78 }} /> rotate soon · <Pill r={{ state: "overdue", due_days: -9, age_days: 99 }} /> overdue · <Pill r={{ state: "never", due_days: null, age_days: 0 }} /> no expiry</div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- a new secret
function NewSecret({ busy, setBusy, onDone, onCancel }: { busy: boolean; setBusy: (b: boolean) => void; onDone: (name: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [app, setApp] = useState("");
  const [env, setEnv] = useState("");
  const [note, setNote] = useState("");
  const [why, setWhy] = useState<string | null>(null);
  const [made, setMade] = useState<{ name: string; bounce: string } | null>(null);
  const okName = /^[A-Za-z0-9_-]{1,255}$/.test(name);
  const submit = () => {
    if (!okName || !value || busy) return;
    setBusy(true); setWhy(null);
    void api.post<{ ok: boolean; name: string; bounce: string }>("/api/secrets", { name, value, app, env, note })
      .then((r) => { setMade({ name: r.name, bounce: r.bounce }); setValue(""); })
      .catch((e: Error) => setWhy(e.message)).finally(() => setBusy(false));
  };
  if (made) return (
    <div className="vault-detail">
      <div className="vault-kicker">IN THE VAULT</div>
      <div className="vault-name">{made.name}</div>
      <div className="vault-ok">Version 1 is in. {made.bounce}</div>
      <div className="vault-acts"><button className="btn btn-primary text-xs" onClick={() => onDone(made.name)}>Open it</button></div>
    </div>
  );
  return (
    <div className="vault-detail">
      <div className="vault-kicker">A NEW SECRET</div>
      <label className="vault-field"><span>Name</span><input value={name} onChange={(e) => setName(e.target.value.trim())} placeholder="MES_DB_PASSWORD" autoFocus spellCheck={false} />{name && !okName && <small className="err">letters, digits, dashes and underscores</small>}</label>
      <label className="vault-field"><span>Value</span><ValueField value={value} onChange={setValue} placeholder="paste the value - it goes straight to Google Cloud" /></label>
      <div className="vault-field-row">
        <label className="vault-field"><span>Which app reads it</span>
          <div className="vault-picks">{["", ...APPS].map((a) => <button key={a} type="button" className={`vault-pick${app === a ? " on" : ""}`} style={{ "--h": APP_HUE[a] ?? 200 } as React.CSSProperties} onClick={() => setApp(a)}>{a || "shared"}</button>)}</div>
        </label>
        <label className="vault-field"><span>Environment</span>
          <div className="vault-picks">{["", ...ENVS].map((e) => <button key={e} type="button" className={`vault-pick${env === e ? " on" : ""}`} style={{ "--h": ENV_HUE[e] ?? 200 } as React.CSSProperties} onClick={() => setEnv(e)}>{e || "any"}</button>)}</div>
        </label>
      </div>
      <label className="vault-field"><span>What it is, in a few words (for the audit row)</span><input value={note} onChange={(e) => setNote(e.target.value)} placeholder="the warehouse API key" /></label>
      {why && <div className="vault-err">{why}</div>}
      <div className="vault-acts">
        <button className="btn btn-primary text-xs" disabled={!okName || !value || busy} onClick={submit}>{busy ? "Putting it in…" : "Put it in the vault"}</button>
        <button className="btn text-xs" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------- one secret
function SecretPane({ row, board, busy, setBusy, onChanged, onGone }: { row: Row; board: Board; busy: boolean; setBusy: (b: boolean) => void; onChanged: () => void; onGone: () => void }) {
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [readers, setReaders] = useState<Reader[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [rotated, setRotated] = useState<string | null>(null);
  const [why, setWhy] = useState<string | null>(null);
  const [danger, setDanger] = useState(false);
  const [typed, setTyped] = useState("");
  const [know, setKnow] = useState(false);
  const [anyway, setAnyway] = useState(false);
  const [sure, setSure] = useState(false);
  const [delWhy, setDelWhy] = useState<string | null>(null);
  const policyRef = useRef<HTMLSelectElement | null>(null);
  const detail = useCallback((fresh = false) => {
    void api.get<{ versions: Version[]; readers: Reader[]; problem: string | null }>(`/api/secrets/${encodeURIComponent(row.name)}${fresh ? "?fresh=1" : ""}`)
      .then((d) => { setVersions(d.versions); setReaders(d.readers); setProblem(d.problem); }).catch((e: Error) => setProblem(e.message));
  }, [row.name]);
  useEffect(() => { detail(); }, [detail]);
  const rotate = () => {
    if (!value || busy) return;
    setBusy(true); setWhy(null); setRotated(null);
    void api.post<{ version: number; bounce: string; readers: Reader[] }>(`/api/secrets/${encodeURIComponent(row.name)}/rotate`, { value })
      .then((r) => { setRotated(`Version ${r.version} is the one apps read now. ${r.bounce}`); setValue(""); setReaders(r.readers); detail(); onChanged(); })
      .catch((e: Error) => setWhy(e.message)).finally(() => setBusy(false));
  };
  const flip = (v: Version) => {
    if (busy) return;
    setBusy(true);
    void api.post(`/api/secrets/${encodeURIComponent(row.name)}/versions/${v.number}/${v.state === "enabled" ? "disable" : "enable"}`)
      .then(() => detail()).catch((e: Error) => setProblem(e.message)).finally(() => setBusy(false));
  };
  const policy = (val: string) => {
    const days = val === "never" ? null : Number(val);
    void api.put(`/api/secrets/${encodeURIComponent(row.name)}/policy`, { days }).then(onChanged).catch((e: Error) => setProblem(e.message));
  };
  const readN = readers?.length ?? 0;
  const canDelete = board.may_delete && typed === row.name && know && sure && (readN === 0 || anyway) && !busy;
  const del = () => {
    if (!canDelete) return;
    setBusy(true); setDelWhy(null);
    void api.post(`/api/secrets/${encodeURIComponent(row.name)}/delete`, { typed, sure, anyway })
      .then(() => onGone()).catch((e: Error) => setDelWhy(e.message)).finally(() => setBusy(false));
  };
  const policyValue = row.policy_days === null ? "never" : row.explicit ? String(row.policy_days) : "0";
  return (
    <div className="vault-detail">
      <div className="vault-kicker">{row.app ? `${row.app} · ` : "SHARED · "}{row.env ? `${row.env.toUpperCase()} · ` : ""}created {ago(row.created)}</div>
      <div className="vault-name-row"><div className="vault-name elide">{row.name}</div><Pill r={row} /></div>
      {problem && <div className="vault-err">{problem}</div>}

      <div className="vault-block">
        <div className="vault-block-head">ROTATION</div>
        <div className="vault-rot">
          <span>{row.latest ? `last rotated ${ago(row.latest.created)} (version ${row.latest.number})` : "no live version"}</span>
          <label className="vault-inline">rotate every
            <select ref={policyRef} defaultValue={policyValue} onChange={(e) => policy(e.target.value)}>
              <option value="0">{board.default_days} days (the default)</option>
              <option value="30">30 days</option>
              <option value="60">60 days</option>
              <option value="180">180 days</option>
              <option value="365">a year</option>
              <option value="never">never</option>
            </select>
          </label>
        </div>
        <div className="vault-rotate">
          <ValueField value={value} onChange={setValue} placeholder="the new value - a new version; the old ones stay" />
          <button className="btn btn-primary text-xs" disabled={!value || busy} onClick={rotate}>{busy ? "Rotating…" : "Rotate"}</button>
        </div>
        {rotated && <div className="vault-ok">{rotated}</div>}
        {why && <div className="vault-err">{why}</div>}
      </div>

      <div className="vault-block">
        <div className="vault-block-head">VERSIONS <span className="vault-group-n">{versions?.length ?? row.versions_n}</span></div>
        {versions === null && <div className="vault-empty">Reading…</div>}
        <div className="vault-versions">
          {versions?.map((v, i) => (
            <div key={v.number} className={`vault-ver ${v.state}`}>
              <b>v{v.number}</b>
              <span className={`vault-ver-state ${v.state}`}>{v.state}{i === 0 && v.state === "enabled" ? " · what apps read" : ""}</span>
              <span className="vault-ver-when">{ago(v.created)}</span>
              {v.state !== "destroyed" && <button className="btn text-xs" disabled={busy} onClick={() => flip(v)} data-tip={v.state === "enabled" ? "Disable this version - an app asking for it gets nothing" : "Enable it again"}>{v.state === "enabled" ? "Disable" : "Enable"}</button>}
            </div>
          ))}
        </div>
      </div>

      <div className="vault-block">
        <div className="vault-block-head">WHO READS IT <button className="vault-again" onClick={() => detail(true)} data-tip="Grep the linked folders and the console checkout again">look again</button></div>
        {readers === null && <div className="vault-empty">Looking through the linked folders…</div>}
        {readers && readers.length === 0 && <div className="vault-empty">Nothing HELIX can see names it - the linked project folders and the console checkout. It may still be read somewhere HELIX is not linked to.</div>}
        {readers && readers.length > 0 && (
          <div className="vault-readers">{readers.map((r, i) => <div key={i} className="vault-reader"><Chip text={r.app} hue={APP_HUE[r.app] ?? 200} /><code>{r.file}:{r.line}</code></div>)}</div>
        )}
      </div>

      <div className={`vault-block danger${danger ? " open" : ""}`}>
        <button className="vault-block-head danger" onClick={() => setDanger((d) => !d)}>{danger ? "▾" : "▸"} DELETE THIS SECRET</button>
        {danger && (
          <div className="vault-danger">
            <div className="vault-danger-line">Deleting takes every version with it and cannot be undone. Anything that reads <b>{row.name}</b> fails at its next start.</div>
            {readN > 0 && <div className="vault-danger-line red">{readN} place{readN === 1 ? "" : "s"} still read{readN === 1 ? "s" : ""} it (above).</div>}
            {!board.may_delete && <div className="vault-danger-line red">Deleting is Brian, Brendan and Kate - the production list. {board.identity ? `Signed in as ${board.identity}.` : "No gcloud account is signed in on this PC."}</div>}
            <label className="vault-field"><span>Type the name to confirm</span><input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={row.name} spellCheck={false} disabled={!board.may_delete} /></label>
            <label className="vault-tick"><input type="checkbox" checked={know} onChange={(e) => setKnow(e.target.checked)} disabled={!board.may_delete} /> I know every service reading it fails at its next start</label>
            {readN > 0 && <label className="vault-tick red"><input type="checkbox" checked={anyway} onChange={(e) => setAnyway(e.target.checked)} disabled={!board.may_delete} /> Delete it anyway, although {readN === 1 ? "something" : `${readN} places`} still read{readN === 1 ? "s" : ""} it</label>}
            <label className="vault-tick"><input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} disabled={!board.may_delete} /> Are you sure</label>
            {delWhy && <div className="vault-err">{delWhy}</div>}
            <div className="vault-acts">
              <button className="btn btn-danger text-xs" disabled={!canDelete} onClick={del}>{busy ? "Deleting…" : "Delete it for good"}</button>
              <button className="btn text-xs" onClick={() => { setDanger(false); setTyped(""); setKnow(false); setAnyway(false); setSure(false); }}>Keep it</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** A small card for the Console's Vault tab and for Settings: the count, the overdue count, Enter the vault. */
export function VaultCard({ compact }: { compact?: boolean }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => { void api.get<Board>("/api/secrets").then(setBoard).catch((e: Error) => setFailed(e.message)); }, []);
  const overdue = (board?.secrets ?? []).filter((r) => r.state === "overdue").length;
  const soon = (board?.secrets ?? []).filter((r) => r.state === "soon").length;
  return (
    <div className={`vault-card${compact ? " compact" : ""}`}>
      <i className="corners" aria-hidden="true" />
      <span className="vault-lock big" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="11" width="14" height="10" rx="2" /><path className="shackle" d="M8 11V7.5a4 4 0 0 1 8 0V11" /><circle cx="12" cy="16" r="1.5" fill="currentColor" stroke="none" /></svg>
      </span>
      <div className="min-w-0 flex-1">
        <div className="vault-card-title">THE VAULT</div>
        <div className="vault-card-sub">
          {failed ? failed : board?.problem ? board.problem : board ? <>{board.secrets.length} secret{board.secrets.length === 1 ? "" : "s"} in {board.project}{overdue ? <> · <span className="overdue">{overdue} overdue</span></> : null}{soon ? <> · <span className="soon">{soon} due soon</span></> : null}{!overdue && !soon && board.secrets.length ? " · all rotated in time" : ""}</> : "reading…"}
        </div>
      </div>
      <button className="btn btn-primary text-xs" onClick={() => window.dispatchEvent(new CustomEvent("helix-vault"))} data-tip="Create, rotate and delete the company's secrets - values never show">Enter the vault</button>
    </div>
  );
}
