// The Dream journal — what the nights found, verified, tried and applied (READ_ME/DREAM_MIND.md
// §11). One card per night, newest first; discoveries lead, each verified fact carries its host
// and date, applied changes read as one-line summaries. Everything here is the journal's own
// record: nothing is invented for an empty night, and an unverified line is marked as such.
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

/** One discovery as the mind journals it (choose_discoveries): a sentence with its source. */
interface Discovery {
  text?: string;
  source?: string;
  url?: string;
  verified?: boolean | null;
  kind?: string;
}

/** One verified fact (VerifiedStore.Fact through the journal's view). */
interface Fact {
  id?: string;
  claim?: string;
  value?: string;
  host?: string;
  url?: string;
  date?: string;
  project?: string;
  topics?: string[];
}

/** One experiment: an idea tried in a throwaway worktree; only its findings text returned. */
interface Experiment {
  idea?: string;
  ok?: boolean;
  findings?: string;
  recommendation?: string;
  summary?: string;
}

interface Finding { text?: string; url?: string; host?: string; verified?: boolean }

interface Research {
  question?: string;
  why?: string;
  status?: string;
  findings?: Finding[];
  facts_noted?: number;
  queries?: string[];
}

interface Verify {
  claim?: string;
  verdict?: string;
  status?: string;
  url?: string;
  was?: string;
}

interface Draft {
  outcome: string;
  held_for: string;
  summary: string;
  request: string;
  branch: string;
  reason: string;
  origin: string;
}

interface Rebuild { ok: boolean | null; restored: boolean; message: string; at: string }

/** One night, as GET /api/dream/journal hands it over (DreamService.journal_entries). */
interface Night {
  id: string;
  day: string;
  kind: string;
  started: string;
  ended: string;
  window: string;
  stopped_reason: string;
  theme: string;
  model: string;
  discoveries: Discovery[];
  facts: Fact[];
  facts_noted: number;
  experiments: Experiment[];
  research?: Research[];
  verify?: Verify[];
  agenda?: Record<string, unknown>;
  agenda_remaining?: string[];
  self_model_delta?: Record<string, unknown>;
  drafts: Draft[];
  applied: { branch: string; summary: string }[];
  counts: Record<string, number>;
  rebuild: Rebuild | null;
  restart_needed: number;
  report: string;
  report_delivered: boolean;
  limit: string;
  weekly_digest?: string;
  in_progress: boolean;
}

interface Journal {
  available: boolean;
  enabled: boolean;
  start: string;
  running: boolean;
  nights: Night[];
}

const MUTED = { color: "var(--muted)" } as const;

function hostOf(url: string | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** "2026-09-04" → "Thu 4 Sep 2026"; anything unreadable is shown as it came. */
function dayLabel(day: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(day || "");
  if (!m) return day || "an undated night";
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (Number.isNaN(d.getTime())) return day;
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

function timeOf(iso: string): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  return m ? `${m[1]}:${m[2]}` : "";
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The night in numbers — only the buckets that are non-zero, in the report's order. */
function countsLine(night: Night): string {
  const c = night.counts || {};
  const bits: string[] = [];
  if (c.applied) bits.push(`${c.applied} applied`);
  if (c.waiting) bits.push(`${c.waiting} waiting for review`);
  if (c.held) bits.push(`${c.held} held`);
  if (c.failed) bits.push(`${c.failed} failed`);
  if (c.limited) bits.push(`${c.limited} held for the plan's limit`);
  if (c.stopped) bits.push(`${c.stopped} stopped`);
  const facts = night.facts_noted || c.facts || 0;
  if (facts) bits.push(plural(facts, "fact") + " verified");
  const experiments = (night.experiments || []).length || c.experiments || 0;
  if (experiments) bits.push(plural(experiments, "experiment"));
  return bits.join(" · ");
}

function SourceLink({ url, label }: { url?: string; label?: string }) {
  const text = label || hostOf(url);
  if (!text) return null;
  if (url && /^https:\/\//i.test(url)) {
    return (
      <a href={url} target="_blank" rel="noreferrer noopener" style={{ color: "var(--cyan)" }} title={url}>
        {text}
      </a>
    );
  }
  return <span style={{ color: "var(--cyan)" }}>{text}</span>;
}

function DiscoveryRow({ d }: { d: Discovery }) {
  const verified = d.verified === true;
  const unverified = d.verified === false;
  const source = d.source || hostOf(d.url);
  return (
    <li className="text-[13px] leading-snug">
      {d.text}
      {source && (
        <span style={MUTED}>
          {" — "}
          <SourceLink url={d.url} label={source} />
        </span>
      )}
      {verified && <span className="text-xs ml-2" style={{ color: "var(--done)" }}>verified</span>}
      {unverified && <span className="text-xs ml-2" style={{ color: "var(--working)" }}>unverified</span>}
    </li>
  );
}

function FactRow({ f }: { f: Fact }) {
  const host = f.host || hostOf(f.url);
  return (
    <li className="text-[13px] leading-snug">
      <span>{f.claim}</span>
      {f.value && <span>{": "}{f.value}</span>}
      <span style={MUTED}>
        {" — "}
        {host ? <SourceLink url={f.url} label={host} /> : "source not recorded"}
        {f.date ? `, ${f.date}` : ""}
        {f.project ? ` · for ${f.project}` : ""}
      </span>
    </li>
  );
}

/**
 * A draft's one-line summary is the coder's own closing words; when the coder signed off with
 * chatter ("All done — here's a summary of the change:") instead of a summary, the line says
 * nothing and the request is the honest label. Such a summary is dropped, never shown.
 */
function cleanSummary(summary: string | undefined): string {
  const s = (summary || "").trim();
  if (!s) return "";
  if (/[:：]$/.test(s)) return "";
  const lower = s.toLowerCase();
  if (/here(’|')?s (a |the )?summary/.test(lower) || /^all done\b/.test(lower)) return "";
  return s;
}

function ExperimentRow({ e }: { e: Experiment }) {
  const verdict = e.ok === false
    ? (e.summary || "could not run")
    : e.recommendation
      ? `recommends: ${e.recommendation}`
      : "no change recommended";
  return (
    <li className="text-[13px] leading-snug">
      {e.idea || e.summary || "an experiment"}
      <span style={MUTED}> — {verdict}</span>
    </li>
  );
}

function outcomeLabel(d: Draft): { text: string; color: string } {
  switch (d.outcome) {
    case "applied": return { text: "applied", color: "var(--done)" };
    case "drafted": return { text: "waiting for your review", color: "var(--cyan)" };
    case "held":
      return d.held_for === "limit"
        ? { text: "held — the plan's limit", color: "var(--working)" }
        : { text: "held", color: "var(--working)" };
    case "failed": return { text: "failed", color: "var(--error)" };
    case "skipped": return { text: "skipped", color: "var(--muted)" };
    case "stopped": return { text: "stopped", color: "var(--muted)" };
    default: return { text: d.outcome || "in progress", color: "var(--muted)" };
  }
}

function NightCard({ night }: { night: Night }) {
  const [open, setOpen] = useState(false);
  const applied = night.applied || [];
  const appliedBranches = new Set(applied.map((a) => a.branch));
  const otherDrafts = (night.drafts || []).filter((d) => !(d.outcome === "applied" && appliedBranches.has(d.branch)));
  const research = night.research || [];
  const verify = night.verify || [];
  const remaining = night.agenda_remaining || [];
  const nothing = !night.discoveries?.length && !night.facts?.length && !night.experiments?.length
    && !applied.length && !otherDrafts.length && !night.report;
  const when = [timeOf(night.started), timeOf(night.ended)].filter(Boolean).join("–") || night.window;

  return (
    <section className="glass rounded-2xl p-5">
      <div className="flex items-baseline gap-3 flex-wrap">
        <div className="section-title">{dayLabel(night.day)}</div>
        <span className="text-xs" style={MUTED}>
          {when}
          {night.kind === "now" ? " · started by hand" : ""}
          {night.model ? ` · drafted on ${night.model}` : ""}
          {night.in_progress ? " · in progress" : ""}
        </span>
        {night.theme && <span className="text-xs" style={MUTED}>· {night.theme}</span>}
      </div>

      {countsLine(night) && (
        <div className="text-xs mt-1" style={MUTED}>{countsLine(night)}</div>
      )}

      {night.discoveries?.length > 0 && (
        <div className="mt-3">
          <div className="text-xs mb-1 tracking-wide" style={{ color: "var(--cyan)" }}>DISCOVERED</div>
          <ul className="space-y-1 pl-4 list-disc">
            {night.discoveries.map((d, i) => <DiscoveryRow key={i} d={d} />)}
          </ul>
        </div>
      )}

      {applied.length > 0 && (
        <div className="mt-3">
          <div className="text-xs mb-1 tracking-wide" style={{ color: "var(--done)" }}>APPLIED</div>
          <ul className="space-y-1 pl-4 list-disc">
            {applied.map((a, i) => {
              const summary = cleanSummary(a.summary);
              return (
                <li key={a.branch || i} className="text-[13px] leading-snug">
                  {summary || a.branch}
                  {a.branch && summary && <span className="text-xs ml-2" style={MUTED}>{a.branch}</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {night.facts?.length > 0 && (
        <div className="mt-3">
          <div className="text-xs mb-1 tracking-wide" style={{ color: "var(--cyan)" }}>VERIFIED</div>
          <ul className="space-y-1 pl-4 list-disc">
            {night.facts.map((f, i) => <FactRow key={f.id || i} f={f} />)}
          </ul>
        </div>
      )}

      {night.experiments?.length > 0 && (
        <div className="mt-3">
          <div className="text-xs mb-1 tracking-wide" style={{ color: "var(--cyan)" }}>TRIED</div>
          <ul className="space-y-1 pl-4 list-disc">
            {night.experiments.map((e, i) => <ExperimentRow key={i} e={e} />)}
          </ul>
          <div className="text-xs mt-1" style={MUTED}>
            Experiments run in a throwaway copy and ship nothing; only their findings come back.
          </div>
        </div>
      )}

      {otherDrafts.length > 0 && (
        <div className="mt-3">
          <div className="text-xs mb-1 tracking-wide" style={MUTED}>DRAFTS</div>
          <ul className="space-y-1 pl-4 list-disc">
            {otherDrafts.map((d, i) => {
              const label = outcomeLabel(d);
              const summary = cleanSummary(d.summary);
              return (
                <li key={d.branch || i} className="text-[13px] leading-snug">
                  {d.request || summary || d.branch}
                  <span className="text-xs ml-2" style={{ color: label.color }}>{label.text}</span>
                  {d.reason && <span className="text-xs ml-2" style={MUTED}>{d.reason}</span>}
                  {d.request && summary && (
                    <div className="text-xs mt-0.5" style={MUTED}>{summary}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {night.rebuild && (
        <div className="text-[13px] mt-3" style={{ color: night.rebuild.ok === false ? "var(--error)" : MUTED.color }}>
          Rebuild{night.rebuild.at ? ` at ${timeOf(night.rebuild.at)}` : ""}:{" "}
          {night.rebuild.ok === true
            ? (night.rebuild.restored ? "the new build failed and the previous one was restored" : "rebuilt and relaunched")
            : night.rebuild.ok === false
              ? (night.rebuild.message || "failed")
              : night.rebuild.message}
        </div>
      )}
      {night.restart_needed > 0 && !night.rebuild && (
        <div className="text-[13px] mt-3" style={MUTED}>
          Restart HELIX to load {plural(night.restart_needed, "applied change")}.
        </div>
      )}

      {night.limit && <div className="text-[13px] mt-3" style={{ color: "var(--working)" }}>{night.limit}</div>}

      {night.report && (
        <div className="text-[13px] mt-3 leading-snug" style={{ whiteSpace: "pre-line" }}>
          <span className="text-xs mr-2 tracking-wide" style={MUTED}>REPORT</span>
          {night.report}
          {!night.report_delivered && !night.in_progress && (
            <span className="text-xs ml-2" style={{ color: "var(--cyan)" }}>not told yet</span>
          )}
        </div>
      )}

      {night.weekly_digest && (
        <div className="text-[13px] mt-3 leading-snug" style={{ whiteSpace: "pre-line" }}>
          <span className="text-xs mr-2 tracking-wide" style={MUTED}>THE WEEK</span>
          {night.weekly_digest}
        </div>
      )}

      {nothing && (
        <div className="text-[13px] mt-3" style={MUTED}>
          {night.in_progress
            ? "This session is still running — nothing journaled yet."
            : night.stopped_reason
              ? `Nothing came of this night — ${night.stopped_reason}.`
              : "Nothing came of this night."}
        </div>
      )}

      {(research.length > 0 || verify.length > 0 || remaining.length > 0) && (
        <div className="mt-3">
          <button className="btn text-xs" onClick={() => setOpen((o) => !o)}>
            {open ? "Hide the night's notes" : "The night's notes"}
            {!open && ` · ${[research.length && plural(research.length, "question"),
              verify.length && plural(verify.length, "check"),
              remaining.length && `${remaining.length} carried over`].filter(Boolean).join(", ")}`}
          </button>
          {open && (
            <div className="mt-3 space-y-3">
              {research.length > 0 && (
                <div>
                  <div className="text-xs mb-1 tracking-wide" style={MUTED}>RESEARCHED</div>
                  <ul className="space-y-2 pl-4 list-disc">
                    {research.map((r, i) => (
                      <li key={i} className="text-[13px] leading-snug">
                        {r.question}
                        {r.why && <span style={MUTED}> — {r.why}</span>}
                        {r.status && r.status !== "ok" && <span className="text-xs ml-2" style={{ color: "var(--working)" }}>{r.status}</span>}
                        {(r.findings || []).length > 0 && (
                          <ul className="pl-4 mt-1 space-y-0.5" style={{ listStyle: "circle" }}>
                            {(r.findings || []).map((f, j) => (
                              <li key={j} className="text-xs">
                                {f.text}
                                {f.verified
                                  ? <span style={MUTED}> — <SourceLink url={f.url} label={f.host || hostOf(f.url)} /></span>
                                  : <span className="ml-2" style={{ color: "var(--working)" }}>unverified</span>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {verify.length > 0 && (
                <div>
                  <div className="text-xs mb-1 tracking-wide" style={MUTED}>RE-CHECKED</div>
                  <ul className="space-y-1 pl-4 list-disc">
                    {verify.map((v, i) => (
                      <li key={i} className="text-[13px] leading-snug">
                        {v.claim}
                        <span className="text-xs ml-2" style={{
                          color: v.verdict === "contradicted" ? "var(--error)"
                            : v.verdict === "confirmed" ? "var(--done)" : "var(--muted)",
                        }}>
                          {v.verdict || v.status || "not reached"}
                        </span>
                        {v.url && <span style={MUTED}> — <SourceLink url={v.url} /></span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {remaining.length > 0 && (
                <div>
                  <div className="text-xs mb-1 tracking-wide" style={MUTED}>CARRIED TO THE NEXT NIGHT</div>
                  <ul className="space-y-1 pl-4 list-disc">
                    {remaining.map((r, i) => <li key={i} className="text-[13px] leading-snug">{r}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default function Dream() {
  const navigate = useHelix((s) => s.navigate);
  const liveDream = useHelix((s) => s.dream);
  const [journal, setJournal] = useState<Journal | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    void api.get<Journal>("/api/dream/journal")
      .then((j) => { setJournal(j); setFailed(false); })
      .catch(() => setFailed(true));
  }, []);
  // One read on open, and one more each time a session starts or ends while the page is open
  // (the event stream flips dream.running), so a night appears the moment it ends.
  useEffect(() => { load(); }, [liveDream?.running, load]);

  const running = liveDream ? liveDream.running : Boolean(journal?.running);
  const nights = journal?.nights ?? [];

  let empty = "";
  if (journal && nights.length === 0) {
    if (!journal.available) empty = "Dreaming isn't available in this build.";
    else if (running) empty = "The first session is running now — its journal appears when it ends.";
    else if (journal.enabled) empty = `No dreams yet — the first session runs tonight at ${journal.start}.`;
    else empty = "No dreams yet — dreaming is switched off. Turn it on in Settings, or ask for a session now.";
  }

  return (
    <div className="h-full overflow-y-auto pt-16 px-8 pb-12" style={{ pointerEvents: "auto" }}>
      <div className="max-w-[760px] mx-auto space-y-5">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="font-display text-[17px] font-bold tracking-[2px]" style={{ color: "var(--cyan)" }}>
            ◐ DREAM JOURNAL
          </div>
          {running && <span className="text-xs" style={{ color: "var(--working)" }}>◐ dreaming now</span>}
          <div className="flex-1" />
          <button className="btn text-xs" onClick={load}>Refresh</button>
          <button className="btn text-xs" onClick={() => navigate({ name: "settings" })}>Dreaming settings</button>
        </div>
        <div className="text-[13px]" style={MUTED}>
          What HELIX found, verified, tried and applied while you slept — newest night first. A
          discovery marked verified was read from a manufacturer, distributor or documentation page
          that night; anything unverified is marked so.
        </div>

        {failed && (
          <div className="text-[13px]" style={{ color: "var(--error)" }}>
            Couldn't read the journal — HELIX may be restarting. Try Refresh in a moment.
          </div>
        )}
        {!failed && journal === null && <div className="text-[13px]" style={MUTED}>Reading the journal…</div>}
        {empty && <div className="glass rounded-2xl p-5 text-[13px]" style={MUTED}>{empty}</div>}

        {nights.map((night) => <NightCard key={night.id || night.day} night={night} />)}
      </div>
    </div>
  );
}
