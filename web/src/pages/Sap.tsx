// The SAP page — HELIX's SAP data-model faculty as a browsable panel: search the catalog, read a
// table's definition (its key, the joins with their FULL compound keys, the standard filters, the
// fields, what the EDW has), plan a join across tables, read the WIP report's columns and query,
// and record what the EDW actually holds. Every fact here is a LOOKUP into the same dicts the
// tools read (helix/services/sap.py) — nothing is recited from memory — and every string renders
// as text, never markup: the dictionary pages and the pasted column lists are untrusted content.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import VisualBlock from "../components/Chart";
import { api } from "../lib/api";
import { useHelix, type Visual } from "../lib/store";

// ----- the routes' shapes (helix/services/sap.py *_dict) -----
interface SearchHit { table?: string; field?: string; description?: string; type?: string; edw?: unknown; note?: string }
interface SearchTable { name?: string; description?: string; module?: string; edw?: unknown }
interface SearchResult { hits?: SearchHit[]; tables?: SearchTable[] }

interface FilterRow { table?: string; field?: string; op?: string; value?: string; why?: string; optional?: boolean }
interface FieldRow {
  name?: string; description?: string; key?: boolean; type?: string; check_table?: string;
  edw?: unknown; note?: string;
}
interface JoinRow {
  other?: string; direction?: string; on?: unknown[]; kind?: string; cardinality?: string;
  note?: string; verified?: boolean; filters?: FilterRow[];
}
interface EdwStatus { status?: string; columns_recorded?: unknown; custom?: string[]; missing?: string[] }
interface TableDef {
  name?: string; description?: string; module?: string; component?: string; source?: string;
  source_url?: string; fetched_at?: string; keys?: string[]; fields?: FieldRow[]; joins?: JoinRow[];
  filters?: FilterRow[]; edw?: EdwStatus; notes?: string[];
}

interface PlanStep {
  left?: string; right?: string; on?: unknown[]; kind?: string; cardinality?: string; note?: string;
  verified?: boolean;
}
interface JoinResult {
  plan?: PlanStep[]; filters?: FilterRow[]; unreachable?: string[]; sql?: string;
  edw?: Record<string, unknown>;
}

interface ReportColumn { label?: string; source?: string; expression?: string; status?: string; note?: string }
interface ReportResult { columns?: ReportColumn[]; sql?: string; warnings?: string[] }

interface EdwTable {
  name?: string; status?: string; columns?: unknown; recorded_at?: string; source?: string;
  custom?: string[];
}
interface EdwResult { prefix?: string; view_prefix?: string; plant?: string; tables?: EdwTable[] }

interface SapStatus {
  shipped?: number; extensions?: number; wide_index?: boolean; fetch_allowed?: boolean;
  curated_joins?: number; verified_joins?: number; edw_tables_recorded?: number;
}

type Tab = "table" | "join" | "sql" | "edw";
const TABS: [Tab, string][] = [["table", "Table"], ["join", "Join"], ["sql", "SQL"], ["edw", "EDW"]];

// ----- small helpers -----
const text = (v: unknown): string => (v === null || v === undefined ? "" : String(v));

/** Mirrors domain.sap.model.normalize_table: upper-case, and 'TV_AFRU' means AFRU. */
function normTable(raw: string): string {
  let n = raw.trim().toUpperCase().replace(/\s+/g, "");
  if (n.startsWith("TV_") && n.length > 3) n = n.slice(3);
  return n;
}

/** A join's `on` list as [left, right] string pairs, whatever shape the JSON took. */
function pairs(on: unknown[] | undefined): [string, string][] {
  return (Array.isArray(on) ? on : []).map((p) => {
    const a = Array.isArray(p) ? p : [];
    return [text(a[0]), text(a[1])];
  });
}

/** The EDW flag as the service sends it (a bool on a field, a status word on a table). */
function edwWord(v: unknown): string {
  if (v === true) return "present";
  if (v === false) return "missing";
  return text(v);
}

function edwTone(v: unknown): "cyan" | "amber" | "muted" {
  const w = edwWord(v);
  if (w === "present") return "cyan";
  if (w === "missing") return "amber";
  return "muted";
}

function errorLine(e: unknown): string {
  return e instanceof Error && e.message ? e.message : "Something went wrong.";
}

const TONE = { cyan: "var(--cyan)", amber: "var(--amber)", muted: "var(--muted)" } as const;

function Chip({ tone = "muted", children, title }: { tone?: keyof typeof TONE; children: ReactNode; title?: string }) {
  return (
    <span className="inline-block text-[11px] rounded-full px-2 py-0.5 mr-1 mb-1" title={title}
      style={{ color: TONE[tone], border: `1px solid ${TONE[tone]}`, opacity: 0.9 }}>
      {children}
    </span>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <span style={{ fontFamily: "ui-monospace, Consolas, monospace" }}>{children}</span>;
}

/** A SQL skeleton or query in a monospace block with a ⧉ Copy button. */
function SqlBlock({ sql, label }: { sql: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard.writeText(sql).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }).catch(() => undefined);
  };
  if (!sql) return null;
  return (
    <div className="mt-3">
      <div className="flex items-center gap-2 mb-1">
        <span className="section-title flex-1" style={{ borderBottom: "none", paddingBottom: 0 }}>{label}</span>
        <button className="btn-nav text-xs" onClick={copy}>{copied ? "✓ Copied" : "⧉ Copy"}</button>
      </div>
      <pre className="text-xs rounded-xl p-3 overflow-x-auto"
        style={{ background: "var(--panel)", border: "1px solid var(--line)", color: "var(--text)",
          fontFamily: "ui-monospace, Consolas, monospace", whiteSpace: "pre", maxHeight: 460 }}>
        {sql}
      </pre>
    </div>
  );
}

function FilterLines({ filters, showTable }: { filters: FilterRow[] | undefined; showTable: boolean }) {
  const rows = Array.isArray(filters) ? filters : [];
  if (!rows.length) return null;
  return (
    <div className="space-y-1 mt-1">
      {rows.map((f, i) => (
        <div key={i} className="text-xs">
          <Mono>
            {showTable && f.table ? `${text(f.table)}.` : ""}{text(f.field)} {text(f.op) || "="}{" "}
            {text(f.value) ? `'${text(f.value)}'` : ""}
          </Mono>
          {f.optional && <Chip tone="muted">optional</Chip>}
          {f.why && <span style={{ color: "var(--muted)" }}> — {text(f.why)}</span>}
        </div>
      ))}
    </div>
  );
}

// ----- the Table tab -----
function TableView({ def, onJoin }: { def: TableDef; onJoin: (name: string) => void }) {
  const [filter, setFilter] = useState("");
  const name = text(def.name);
  const provenance = [text(def.source), text(def.source_url), def.fetched_at ? `read ${text(def.fetched_at)}` : ""]
    .filter(Boolean).join(" · ");
  const keys = Array.isArray(def.keys) ? def.keys.map(text) : [];
  const fields = Array.isArray(def.fields) ? def.fields : [];
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? fields.filter((f) => [f.name, f.description, f.check_table, f.type].some((v) => text(v).toLowerCase().includes(needle)))
    : fields;
  const grid: Visual = {
    type: "table",
    title: `Fields · ${shown.length}${needle ? ` of ${fields.length}` : ""}`,
    columns: ["Field", "Type", "Description", "Check table", "EDW"],
    rows: shown.map((f) => [
      f.key ? `${text(f.name)} (key)` : text(f.name), text(f.type), text(f.description) + (f.note ? ` — ${text(f.note)}` : ""),
      text(f.check_table), f.edw === null || f.edw === undefined ? "" : edwWord(f.edw),
    ]),
  };
  const edw = def.edw || {};
  const joins = Array.isArray(def.joins) ? def.joins : [];
  const notes = Array.isArray(def.notes) ? def.notes : [];
  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-baseline gap-3 flex-wrap">
          <span className="font-semibold text-[18px]" style={{ color: "var(--cyan)" }}><Mono>{name}</Mono></span>
          <span className="text-[14px]">{text(def.description)}</span>
          <span className="flex-1" />
          <button className="btn-nav text-xs" onClick={() => onJoin(name)}>＋ Join</button>
        </div>
        <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
          {[text(def.module), text(def.component)].filter(Boolean).join(" · ")}
          {provenance ? ` · from ${provenance}` : ""}
        </div>
        <div className="mt-2">
          {keys.length ? (
            <>
              <span className="text-xs mr-1" style={{ color: "var(--muted)" }}>Key</span>
              {keys.map((k) => <Chip key={k} tone="cyan"><Mono>{k}</Mono></Chip>)}
            </>
          ) : (
            <span className="text-xs" style={{ color: "var(--muted)" }}>No key fields recorded.</span>
          )}
        </div>
        <div className="mt-1">
          <Chip tone={edwTone(edw.status)} title="What your EDW has, from the column lists you recorded">
            EDW {edwWord(edw.status) || "unknown"}
            {edw.columns_recorded ? ` · ${typeof edw.columns_recorded === "number" ? `${edw.columns_recorded} columns` : "columns recorded"}` : ""}
          </Chip>
          {(edw.custom || []).map((z) => <Chip key={`z-${text(z)}`} tone="amber"><Mono>{text(z)}</Mono></Chip>)}
          {(edw.missing || []).length > 0 && (
            <span className="text-xs" style={{ color: "var(--muted)" }}>
              not in the EDW: <Mono>{(edw.missing || []).map(text).join(", ")}</Mono>
            </span>
          )}
        </div>
        {notes.length > 0 && (
          <ul className="text-xs mt-2 space-y-0.5 list-disc pl-4" style={{ color: "var(--text)" }}>
            {notes.map((n, i) => <li key={i}>{text(n)}</li>)}
          </ul>
        )}
      </div>

      {(def.filters || []).length > 0 && (
        <div>
          <div className="section-title">Standard filters</div>
          <FilterLines filters={def.filters} showTable={false} />
        </div>
      )}

      <div>
        <div className="section-title">Joins · {joins.length}</div>
        {joins.length === 0 && (
          <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>No joins known for this table yet.</div>
        )}
        <div className="space-y-2 mt-2">
          {joins.map((j, i) => {
            // `on` keeps the edge's own orientation (left → right); direction "in" means the
            // other table is the edge's left side, so the qualified pairs read left-to-right.
            const inward = text(j.direction) === "in";
            const other = text(j.other);
            const l = inward ? other : name;
            const r = inward ? name : other;
            return (
              <div key={i} className="card px-3 py-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[13px] font-medium" style={{ color: "var(--cyan)" }}>
                    <Mono>{inward ? `${other} → ${name}` : `${name} → ${other}`}</Mono>
                  </span>
                  <Chip tone="muted">{text(j.kind) || "join"}</Chip>
                  {j.cardinality && <Chip tone="muted">{text(j.cardinality)}</Chip>}
                  <Chip tone={j.verified ? "cyan" : "amber"}>{j.verified ? "verified" : "unverified"}</Chip>
                </div>
                <div className="text-xs mt-1">
                  <Mono>{pairs(j.on).map(([a, b]) => `${l}.${a} = ${r}.${b}`).join(" AND ")}</Mono>
                </div>
                {j.note && <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>{text(j.note)}</div>}
                <FilterLines filters={j.filters} showTable />
              </div>
            );
          })}
        </div>
      </div>

      <div>
        <div className="flex items-center gap-2 mb-2">
          <div className="section-title flex-1">Fields · {fields.length}</div>
          <input className="text-xs py-1 px-2" style={{ width: 220 }} placeholder="Filter fields…"
            value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        {fields.length ? <VisualBlock spec={grid} /> : (
          <div className="text-xs" style={{ color: "var(--muted)" }}>No field list recorded for this table.</div>
        )}
      </div>
    </div>
  );
}

// ----- the Join tab -----
function JoinView({ chips, setChips }: { chips: string[]; setChips: (c: string[]) => void }) {
  const [entry, setEntry] = useState("");
  const [root, setRoot] = useState("");
  const [result, setResult] = useState<JoinResult | null>(null);
  const [status, setStatus] = useState("");

  const add = (raw: string) => {
    const names = raw.split(/[,\s]+/).map(normTable).filter(Boolean);
    if (names.length) setChips([...chips, ...names.filter((n) => !chips.includes(n))]);
    setEntry("");
  };
  const remove = (n: string) => setChips(chips.filter((c) => c !== n));
  const rootName = chips.includes(root) ? root : chips[0] || "";

  const plan = () => {
    if (chips.length < 2) {
      setStatus("Name at least two tables.");
      return;
    }
    setStatus("Planning…");
    const q = `tables=${encodeURIComponent(chips.join(","))}&root=${encodeURIComponent(rootName)}`;
    void api.get<JoinResult>(`/api/sap/join?${q}`)
      .then((res) => {
        setResult(res);
        setStatus("");
      })
      .catch((e) => setStatus(errorLine(e)));
  };

  const edw = result?.edw && typeof result.edw === "object" ? result.edw : {};
  return (
    <div className="space-y-4">
      <div>
        <div className="section-title">Tables</div>
        <div className="mt-2">
          {chips.map((n) => (
            <span key={n} className="inline-flex items-center text-[12px] rounded-full pl-2 pr-1 py-0.5 mr-1 mb-1"
              style={{ border: `1px solid ${n === rootName ? "var(--cyan)" : "var(--line)"}`,
                color: n === rootName ? "var(--cyan)" : "var(--text)" }}
              title={n === rootName ? "The FROM table" : "Click to make this the FROM table"}
              onClick={() => setRoot(n)}>
              <Mono>{n}</Mono>
              <button className="btn-nav text-xs px-1 py-0" onClick={(e) => { e.stopPropagation(); remove(n); }}>✕</button>
            </span>
          ))}
        </div>
        <div className="flex gap-2 mt-2 items-center">
          <input className="flex-1 text-[13px] py-2" placeholder="Add tables — AFRU, AFVC, CRHD (Enter or comma)"
            value={entry}
            onChange={(e) => {
              if (e.target.value.includes(",")) add(e.target.value);
              else setEntry(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (entry.trim()) add(entry);
                else plan();
              }
            }} />
          <button className="btn btn-primary text-[13px]" onClick={() => { if (entry.trim()) add(entry); plan(); }}>
            Plan join
          </button>
        </div>
        <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
          {rootName ? `FROM ${rootName} — click a chip to change the root.` : "The first table is the FROM table."}
          {status ? ` ${status}` : ""}
        </div>
      </div>

      {result && (
        <>
          <div>
            <div className="section-title">Plan · {(result.plan || []).length} hops</div>
            <div className="space-y-2 mt-2">
              {(result.plan || []).map((s, i) => (
                <div key={i} className="card px-3 py-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-medium" style={{ color: "var(--cyan)" }}>
                      <Mono>{text(s.left)} → {text(s.right)}</Mono>
                    </span>
                    <Chip tone="muted">{text(s.kind) || "join"}</Chip>
                    {s.cardinality && <Chip tone="muted">{text(s.cardinality)}</Chip>}
                    <Chip tone={s.verified ? "cyan" : "amber"}>{s.verified ? "verified" : "unverified"}</Chip>
                  </div>
                  <div className="text-xs mt-1">
                    <Mono>{pairs(s.on).map(([a, b]) => `${text(s.left)}.${a} = ${text(s.right)}.${b}`).join(" AND ")}</Mono>
                  </div>
                  {s.note && <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>{text(s.note)}</div>}
                </div>
              ))}
              {(result.plan || []).length === 0 && (
                <div className="text-xs" style={{ color: "var(--muted)" }}>No hops — one table, or nothing reachable.</div>
              )}
            </div>
          </div>

          {(result.unreachable || []).length > 0 && (
            <div className="text-xs" style={{ color: "var(--amber)" }}>
              No join path reached: <Mono>{(result.unreachable || []).map(text).join(", ")}</Mono>
            </div>
          )}

          {(result.filters || []).length > 0 && (
            <div>
              <div className="section-title">Filters</div>
              <FilterLines filters={result.filters} showTable />
            </div>
          )}

          {Object.keys(edw).length > 0 && (
            <div>
              <span className="text-xs mr-1" style={{ color: "var(--muted)" }}>EDW</span>
              {Object.entries(edw).map(([t, st]) => (
                <Chip key={t} tone={edwTone(st)}><Mono>{t}</Mono> {edwWord(st) || "unknown"}</Chip>
              ))}
            </div>
          )}

          <SqlBlock sql={text(result.sql)} label="FROM / JOIN skeleton" />
        </>
      )}
    </div>
  );
}

// ----- the SQL tab: the WIP report -----
function ReportView() {
  const [report, setReport] = useState<ReportResult | null>(null);
  const [status, setStatus] = useState("Loading the WIP report…");
  useEffect(() => {
    void api.get<ReportResult>("/api/sap/report/wip")
      .then((res) => {
        setReport(res);
        setStatus("");
      })
      .catch((e) => setStatus(errorLine(e)));
  }, []);
  if (!report) return <div className="text-xs" style={{ color: "var(--muted)" }}>{status}</div>;
  const cols = Array.isArray(report.columns) ? report.columns : [];
  const grid: Visual = {
    type: "table",
    title: `WIP report · ${cols.length} columns`,
    columns: ["Label", "Source", "Status", "Note"],
    rows: cols.map((col) => [text(col.label), text(col.source), text(col.status), text(col.note)]),
  };
  return (
    <div className="space-y-3">
      <div className="text-xs" style={{ color: "var(--muted)" }}>
        The WIP report as the curated layer defines it: each column's source field, whether it is
        standard SAP, derived, probable, or a site custom field — and the query, written for your
        EDW views.
      </div>
      <VisualBlock spec={grid} />
      {(report.warnings || []).length > 0 && (
        <ul className="text-xs list-disc pl-4 space-y-0.5" style={{ color: "var(--amber)" }}>
          {(report.warnings || []).map((w, i) => <li key={i}>{text(w)}</li>)}
        </ul>
      )}
      <SqlBlock sql={text(report.sql)} label="Query" />
    </div>
  );
}

// ----- the EDW tab -----
function EdwView() {
  const [edw, setEdw] = useState<EdwResult | null>(null);
  const [table, setTable] = useState("");
  const [paste, setPaste] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    void api.get<EdwResult>("/api/sap/edw").then(setEdw).catch((e) => setStatus(errorLine(e)));
  }, []);
  useEffect(load, [load]);

  const record = (action: "record_columns" | "information_schema" | "missing") => {
    if (busy) return;
    const name = normTable(table);
    if (!name && action !== "information_schema") {
      setStatus("Name the table first.");
      return;
    }
    if (!paste.trim() && action !== "missing") {
      setStatus(action === "record_columns" ? "Paste the column list first." : "Paste the export first.");
      return;
    }
    setBusy(true);
    setStatus("Recording…");
    void api.post<{ ok?: boolean; message?: string }>("/api/sap/edw", { action, table: name, text: paste })
      .then((res) => {
        setStatus(text(res.message) || (res.ok ? "Recorded." : "Nothing recorded."));
        if (res.ok) setPaste("");
        load();
      })
      .catch((e) => setStatus(errorLine(e)))
      .finally(() => setBusy(false));
  };

  const tables = edw && Array.isArray(edw.tables) ? edw.tables : [];
  return (
    <div className="space-y-4">
      <div className="text-xs" style={{ color: "var(--muted)" }}>
        {edw ? (
          <>
            Views read as <Mono>{text(edw.prefix)}{text(edw.view_prefix)}&lt;TABLE&gt;</Mono>
            {edw.plant ? <> · plant <Mono>{text(edw.plant)}</Mono></> : null}
            {" · "}{tables.length} table{tables.length === 1 ? "" : "s"} recorded
          </>
        ) : "Reading the EDW overlay…"}
      </div>

      <div className="card p-3">
        <div className="section-title mb-2">Record what the EDW has</div>
        <div className="flex gap-2 items-center mb-2">
          <input className="text-[13px] py-2" style={{ width: 180 }} placeholder="Table (AFRU)"
            value={table} onChange={(e) => setTable(e.target.value)} />
          <span className="text-xs" style={{ color: "var(--muted)" }}>
            A column list is one table's; an INFORMATION_SCHEMA export names its own tables.
          </span>
        </div>
        <textarea rows={5} className="w-full text-xs"
          style={{ fontFamily: "ui-monospace, Consolas, monospace" }}
          placeholder="Paste a column list or an INFORMATION_SCHEMA export"
          value={paste} onChange={(e) => setPaste(e.target.value)} />
        <div className="flex gap-2 mt-2 items-center flex-wrap">
          <button className="btn btn-primary text-[13px]" disabled={busy} onClick={() => record("record_columns")}>
            Record columns
          </button>
          <button className="btn text-[13px]" disabled={busy} onClick={() => record("information_schema")}>
            Import export
          </button>
          <button className="btn text-[13px]" disabled={busy} onClick={() => record("missing")}>
            Mark missing
          </button>
          {status && <span className="text-xs" style={{ color: "var(--muted)" }}>{status}</span>}
        </div>
      </div>

      <div className="space-y-2">
        {tables.map((t, i) => (
          <div key={`${text(t.name)}-${i}`} className="card px-3 py-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[13px] font-medium" style={{ color: "var(--cyan)" }}><Mono>{text(t.name)}</Mono></span>
              <Chip tone={edwTone(t.status)}>{edwWord(t.status) || "unknown"}</Chip>
              <span className="text-xs" style={{ color: "var(--muted)" }}>
                {Array.isArray(t.columns) ? `${t.columns.length} columns` : t.columns ? `${text(t.columns)} columns` : ""}
                {t.recorded_at ? ` · ${text(t.recorded_at)}` : ""}
                {t.source ? ` · ${text(t.source)}` : ""}
              </span>
            </div>
            {(t.custom || []).length > 0 && (
              <div className="mt-1">
                <span className="text-xs mr-1" style={{ color: "var(--muted)" }}>custom</span>
                {(t.custom || []).map((z) => <Chip key={text(z)} tone="amber"><Mono>{text(z)}</Mono></Chip>)}
              </div>
            )}
          </div>
        ))}
        {edw && tables.length === 0 && (
          <div className="text-xs py-4" style={{ color: "var(--muted)" }}>
            Nothing recorded yet — paste a column list above and HELIX will stop guessing which
            views exist.
          </div>
        )}
      </div>
    </div>
  );
}

// ----- the page -----
export default function Sap({ table }: { table?: string }) {
  const navigate = useHelix((s) => s.navigate);
  const [tab, setTab] = useState<Tab>("table");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState("");
  const [def, setDef] = useState<TableDef | null>(null);
  const [tableStatus, setTableStatus] = useState("");
  const [chips, setChips] = useState<string[]>([]);
  const [faculty, setFaculty] = useState<SapStatus | null | "down">(null);
  const debounce = useRef(0);
  const seq = useRef(0);
  const loadSeq = useRef(0);

  useEffect(() => {
    void api.get<SapStatus>("/api/sap/status").then(setFaculty).catch(() => setFaculty("down"));
  }, []);

  const openTable = useCallback((name: string) => {
    const n = normTable(name);
    if (!n) return;
    const mySeq = ++loadSeq.current;
    setTab("table");
    setTableStatus(`Reading ${n}…`);
    void api.get<TableDef>(`/api/sap/table/${encodeURIComponent(n)}`)
      .then((res) => {
        if (mySeq !== loadSeq.current) return;
        setDef(res);
        setTableStatus("");
      })
      .catch((e) => {
        if (mySeq !== loadSeq.current) return;
        setTableStatus(errorLine(e));
      });
  }, []);

  useEffect(() => {
    if (table) openTable(table);
  }, [table, openTable]);

  const search = (q: string) => {
    setQuery(q);
    window.clearTimeout(debounce.current);
    const mySeq = ++seq.current;
    if (!q.trim()) {
      setResults(null);
      setSearching("");
      return;
    }
    debounce.current = window.setTimeout(() => {
      setSearching("Searching…");
      void api.get<SearchResult>(`/api/sap/search?q=${encodeURIComponent(q)}`)
        .then((res) => {
          if (mySeq !== seq.current) return;
          setResults(res);
          setSearching("");
        })
        .catch((e) => {
          if (mySeq !== seq.current) return;
          setSearching(errorLine(e));
        });
    }, 300);
  };

  const addToJoin = (name: string) => {
    const n = normTable(name);
    if (n && !chips.includes(n)) setChips([...chips, n]);
    setTab("join");
  };

  const hits = results && Array.isArray(results.hits) ? results.hits : [];
  const tables = results && Array.isArray(results.tables) ? results.tables : [];
  const looksLikeTable = /^[A-Z0-9_/]{2,30}$/.test(normTable(query));
  const statusLine = faculty === "down"
    ? "SAP faculty unavailable"
    : faculty
      ? `${text(faculty.shipped ?? 0)} tables shipped · ${text(faculty.curated_joins ?? 0)} curated joins` +
        ` (${text(faculty.verified_joins ?? 0)} verified) · ${text(faculty.edw_tables_recorded ?? 0)} EDW tables recorded` +
        (faculty.fetch_allowed ? "" : " · fetching off")
      : "";

  return (
    <div className="h-full overflow-y-auto pt-16 px-8 pb-8" style={{ pointerEvents: "auto" }}>
      <div className="max-w-[1180px] mx-auto">
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <button className="btn-nav" onClick={() => navigate({ name: "menu" })}>← Back</button>
          <span className="font-semibold text-[15px]" style={{ color: "var(--cyan)" }}>SAP data model</span>
          <span className="text-xs" style={{ color: faculty === "down" ? "var(--amber)" : "var(--muted)" }}>
            {statusLine}
          </span>
        </div>

        <div className="flex gap-5 items-start">
          {/* the left column: search */}
          <div className="shrink-0" style={{ width: 300 }}>
            <input className="w-full text-[13px]" placeholder="🔍 Table, field, or a business term…"
              value={query} onChange={(e) => search(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && looksLikeTable) openTable(query);
              }} />
            {searching && <div className="text-xs mt-2" style={{ color: "var(--muted)" }}>{searching}</div>}
            <div className="space-y-2 mt-3">
              {looksLikeTable && (
                <div className="card px-3 py-2 cursor-pointer" onClick={() => openTable(query)}>
                  <span className="text-[13px]" style={{ color: "var(--cyan)" }}>Open <Mono>{normTable(query)}</Mono></span>
                  <span className="text-xs ml-2" style={{ color: "var(--muted)" }}>fetches it if it isn't in the catalog</span>
                </div>
              )}
              {tables.map((t, i) => (
                <div key={`t-${text(t.name)}-${i}`} className="card px-3 py-2 cursor-pointer"
                  onClick={() => openTable(text(t.name))}>
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium" style={{ color: "var(--cyan)" }}><Mono>{text(t.name)}</Mono></span>
                    {t.module && <Chip tone="muted">{text(t.module)}</Chip>}
                    {edwWord(t.edw) && <Chip tone={edwTone(t.edw)}>EDW {edwWord(t.edw)}</Chip>}
                  </div>
                  <div className="text-xs elide" style={{ color: "var(--muted)" }}>{text(t.description)}</div>
                </div>
              ))}
              {hits.map((h, i) => (
                <div key={`h-${text(h.table)}-${text(h.field)}-${i}`} className="card px-3 py-2 cursor-pointer"
                  onClick={() => openTable(text(h.table))}>
                  <div className="flex items-center gap-2">
                    <span className="text-[13px]" style={{ color: "var(--cyan)" }}>
                      <Mono>{text(h.table)}{h.field ? `.${text(h.field)}` : ""}</Mono>
                    </span>
                    {h.type && <span className="text-xs" style={{ color: "var(--muted)" }}><Mono>{text(h.type)}</Mono></span>}
                    {edwWord(h.edw) && <Chip tone={edwTone(h.edw)}>EDW {edwWord(h.edw)}</Chip>}
                  </div>
                  <div className="text-xs" style={{ color: "var(--muted)" }}>
                    {text(h.description)}{h.note ? ` — ${text(h.note)}` : ""}
                  </div>
                </div>
              ))}
              {results && !searching && tables.length === 0 && hits.length === 0 && (
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  Nothing in the catalog matches “{query}”.
                </div>
              )}
              {!results && !looksLikeTable && (
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  Try a table (AFRU), a field (ARBPL), or a term (posting date, work center).
                </div>
              )}
            </div>
          </div>

          {/* the main column: tabs */}
          <div className="flex-1 min-w-0">
            <div className="glass rounded-xl px-1 py-0.5 inline-flex gap-0.5 mb-4">
              {TABS.map(([key, label]) => (
                <button key={key} className="btn-nav text-[13px]"
                  style={tab === key ? { color: "var(--cyan)" } : undefined}
                  onClick={() => setTab(key)}>
                  {label}
                </button>
              ))}
            </div>

            {tab === "table" && (
              def ? (
                <>
                  {tableStatus && <div className="text-xs mb-2" style={{ color: "var(--muted)" }}>{tableStatus}</div>}
                  <TableView def={def} onJoin={addToJoin} />
                </>
              ) : (
                <div className="text-sm py-6" style={{ color: "var(--muted)" }}>
                  {tableStatus || "Search on the left, or type a table name and press Enter."}
                </div>
              )
            )}
            {tab === "join" && <JoinView chips={chips} setChips={setChips} />}
            {tab === "sql" && <ReportView />}
            {tab === "edw" && <EdwView />}
          </div>
        </div>
      </div>
    </div>
  );
}
