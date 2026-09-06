# Dreaming, Phase 2 — The Dream Mind (reflection, research, verification, experiment, discovery)

Status: SPEC + build contracts (2026-09-04, later the same evening). Phase 1 (READ_ME/DREAM.md
§1–§8) builds the scaffold — the window, the planner, drafting on Fable, the test-gated merge, the
rebuild at dawn. Phase 2 gives the session a mind. Brian's words are the bar:

> When HELIX is idle — especially overnight — it should not sit there waiting. It should reflect on
> what it knows, examine where it is weak, explore what it could become, and look for ways to improve
> its ability to solve real engineering problems. When I wake up I should notice something changed.
> It should occasionally wow me with what it figured out while I was away. If I go on vacation for a
> week and come back: "Holy shit. This thing got better." A major part is active exploration of the
> outside world — the web, current documentation, new technologies, tools, libraries. For hardware,
> verify reality: search suppliers, manufacturer documentation, distributors, marketplaces for what
> exists, specs, availability, pricing, compatibility, better alternatives. Distinguish MODEL
> KNOWLEDGE (what HELIX believes) from VERIFIED KNOWLEDGE (confirmed through current sources) and
> prefer verified knowledge for engineering decisions. Keep asking: what am I capable of, where am I
> weak, what is Brian building, what could help, what could I learn tonight that makes me materially
> more useful tomorrow. Reflection, research, experimentation, discovery, controlled
> self-improvement. HELIX goes to sleep as one version of itself and wakes up extremely more capable.

## 9. Workstreams (Phase 2 runs after Phase 1 lands, in the same worktree, disjoint files)

| Workstream | Owns | Must not touch |
|---|---|---|
| **F1 — Research faculty + verified knowledge** | NEW `helix/adapters/research_web.py`, NEW `helix/services/research.py`, NEW `helix/services/verified.py`, `helix/services/tools.py` (the research/verified tools only), `helix/services/conversation.py` (the DREAM tool tier + verified injection + the `tool_names` allowlist), `helix/services/prompts.py` (the VERIFIED KNOWLEDGE paragraph only), `helix/domain/vocabulary.py`, `helix/config.py` (volatile names only), NEW `tests/test_research.py`, NEW `tests/test_verified.py`, `tests/test_conversation.py` (extend) | dream.py, dream_mind.py, selfdev.py, shell.py, server.py, web/ |
| **F2 — The Dream Mind** | NEW `helix/services/dream_mind.py`, `helix/services/dream.py` (integrate the mind), `helix/services/selfdev.py` (`experiment()` only), `helix/app/container.py` (wiring), `helix/api/shell.py` (journal/report parts), `helix/api/server.py` (`/api/dream/journal`), NEW `web/src/pages/Dream.tsx` + `web/src/App.tsx` routing + `web/src/pages/Menu.tsx` (a link) + `web/src/pages/Settings.tsx` (a link from the Dreaming card), `web/src/lib/store.ts`, NEW `tests/test_dream_mind.py`, `tests/test_dream.py` (extend), `READ_ME/ARCHITECTURE.md` §8b, `READ_ME/README.md`, DREAM.md "What shipped" | research_web.py, research.py, verified.py, tools.py, conversation.py, prompts.py |

House rules exactly as DREAM.md §1. F2 codes against F1's API below (F1 builds concurrently);
anything missing at the end goes in open_issues, never a stub in F1's files.

## 10. The research faculty (F1) — verified reality, on a leash

`helix/adapters/research_web.py`:
- `Hit(title, url, host, snippet, readable: bool)`; `Page(url, host, title, text, fetched_at)`.
- `search(query, *, max_results=8) -> list[Hit]` — DuckDuckGo's HTML endpoint
  (`https://html.duckduckgo.com/html/?q=…`, no key; the `uddg` redirect parameter decoded to the real
  URL), parsed with lxml, paced (≥ 1.5 s between requests), cached 10 min. `readable` says whether
  `read()` would accept the host. Search failures raise `ResearchUnavailable`.
- `read(url, *, max_chars=12000) -> Page` — HTTPS only, GET only, a browser's headers, no cookies,
  redirects followed only within the allowlist, 4 MB cap, text extracted from `<main>`/`<article>`/
  `<body>` with script/style/nav/header/footer removed, whitespace collapsed, capped at `max_chars`
  with "… (truncated)". A PDF URL (`Content-Type: application/pdf` or `.pdf`) is read through the
  existing `helix/services/doc_extract.py` text path when it can, else refused plainly.
- THE ALLOWLIST (`READ_HOSTS`, suffix-matched on the registrable host with subdomains included) —
  official docs, repositories, package indexes, manufacturers, distributors, and a few community
  references: github.com, raw.githubusercontent.com, gist.github.com, pypi.org, npmjs.com,
  readthedocs.io, readthedocs.org, python.org, developer.mozilla.org, arxiv.org, espressif.com,
  arduino.cc, seeedstudio.com, adafruit.com, sparkfun.com, digikey.com, mouser.com, raspberrypi.com,
  raspberrypi.org, ti.com, st.com, microchip.com, nordicsemi.com, analog.com, bosch-sensortec.com,
  invensense.com, tdk.com, nxp.com, infineon.com, sensirion.com, ams-osram.com, omnivision.com,
  bambulab.com, prusa3d.com, thingiverse.com, printables.com, hackaday.com, hackaday.io,
  instructables.com, stackoverflow.com, stackexchange.com, reddit.com (read-only), wikipedia.org,
  plus `research_hosts_extra` from settings (a comma-separated list the user may add). amazon.com is
  delegated to the Amazon faculty's own readers (search_amazon / lookup_amazon), never a raw fetch.
  Everything else is refused with one plain line naming the host; the model is told the allowlist
  exists and why (HELIX reads only sources whose content it can trust as documentation, never
  arbitrary pages).
- No secret ever rides in a research request; no cookies; no POST; the query text is journaled.

`helix/services/verified.py` — `VerifiedStore(store: SettingsStore, clock)` on
`data/helix_verified.json` (add to `VOLATILE_STORE_NAMES`): `Fact(id, claim, value, source_url, host,
verified_at, first_verified_at, confidence, topics: tuple, project: str, note: str)`;
`note(claim, value, source_url, *, topics=(), project="", confidence=0.9, note="") -> Fact` (dedupe on
the normalized claim → the newer verification replaces the value/date and keeps `first_verified_at`);
`lookup(text, *, project="", limit=8) -> list[Fact]` (keyword + topic + project scoring);
`for_turn(text, project="") -> str` (a compact labelled block, max 8 facts: "claim: value — verified
2026-09-04 from wiki.seeedstudio.com"; "" when nothing is relevant); `stale(days=90)`; `forget(id)`;
`recent(n)`; `count()`. Facts are records, never instructions: the injected block is labelled data.

Tools (`tools.py`), readable tier unless marked:
- `research_search(query)` → the hits, one per line: title — host (readable / not readable) — snippet
  — the exact URL.
- `research_read(url, question="")` → the page text (or the passages answering `question`, found by
  keyword windows, when given), with the source line "Read <host> on <date>". Refuses non-allowlisted
  hosts plainly, naming the host.
- `note_verified_fact(claim, value, source_url, topics, project="", confidence)` — DREAM-tier write
  (below): records a fact HELIX just read from the source; the tool echoes the fact back.
- `verified_facts(query, project="")` — readable: what HELIX has verified about a thing, with dates.
- `forget_verified(id)` — fenced (BUILD_TOOLS).

The DREAM tier (`conversation.py`): `DREAM_TOOLS` = every readable (unfenced) tool plus
`note_verified_fact`, `note_improvement`, `remember`. `run_turn` gains `tool_names: set[str] | None`
(an explicit allowlist applied AFTER the fence filters, at offer time AND at dispatch). The Dream
Mind runs its research turns with `allow_builds=False, tool_names=DREAM_TOOLS`; the model's own web
tools stay OFF for those runs (research goes through research_search/research_read — the audited
channel). A watcher agent never gets the DREAM writes (they are not in its set). Turn injection:
`run_turn` appends `verified.for_turn(prompt, project)` beside lessons/memory when non-empty (a
`verified` constructor arg, None-safe).

Persona (`prompts.py`, one paragraph "VERIFIED KNOWLEDGE" placed after the maker-flow paragraph if
present, else after the Amazon paragraph): for engineering facts — parts, sizes, pins, protocols,
prices, availability — prefer VERIFIED facts (the injected block, or research_read on a datasheet /
wiki / distributor page) over memory; say which it is ("verified today on Seeed's wiki" vs "from
memory — let me verify") and the date when it matters; note what you verify with note_verified_fact
so it's known tomorrow; when a part must exist, check a supplier before promising it.

## 11. The Dream Mind (F2) — the night's structure

`helix/services/dream_mind.py` — `DreamMind(chat, conversation, selfdev, verified, research, parts,
builds, tools_registry, store, settings, clock, log_tail, activity)` with `data/helix_self.json`
(the self-model; add to volatile names) and the night journal (Phase 1's `helix_dream.json` entries
gain `discoveries`, `facts`, `experiments`, `agenda`, `self_model_delta`).

The session runs these cycles inside Phase 1's window/gate/wind-down (DreamService calls
`mind.run_night(deadline, budget)`; `budget` = the drafts ceiling; the mind checks `deadline` and the
activity pause between steps and returns a `NightSummary`):

1. **REFLECT** (growth chat, no tools; `DREAM_REFLECT_SYSTEM`). Material (fenced as data): the tool
   registry's names + one-liners (capabilities), the build list (apps / holograms / protocols /
   agents), the parts lists and their unresolved rows, the last 7 days of conversation (user turns
   only, capped — what Brian is building and asking for), lessons, the log tail (errors/warnings =
   weaknesses), test counts per module, held/failed drafts, the last 7 nights' journals, and the
   current self-model. Output (strict shape, parsed; QUIET allowed): `CAPABLE:` bullets, `WEAK:`
   bullets, `BUILDING:` bullets (Brian's active projects + what each needs next), `AGENDA:` with
   sections `RESEARCH:` (questions, each with a why — up to 8), `VERIFY:` (claims to check — up to 5),
   `EXPERIMENT:` (ideas — up to 2), `IMPROVE:` (self-contained change requests with EFFORT — up to the
   ceiling). The self-model is updated from CAPABLE/WEAK/BUILDING (merged, dated).
2. **RESEARCH** — each question is one research turn (`conversation.run_turn(prompt,
   allow_builds=False, tool_names=DREAM_TOOLS, persist=False, speaker="dream")` with
   `DREAM_RESEARCH_SYSTEM` prepended to the prompt): search, read allowlisted pages, and END with
   `FINDINGS:` bullets each tagged `[verified: <url>]` or `[unverified]`, `FACTS NOTED: n`, `IDEAS:`
   (capability ideas → the improvement backlog via note_improvement, each with its source). The mind
   journals the question, the queries made (from the tool trail), and the findings. A hardware claim
   is `[verified]` only after a manufacturer/distributor page was read in that turn.
3. **VERIFY** — for each stale fact and each `VERIFY:` claim: one research turn that re-reads the
   source (or finds it) and notes the fact again, or marks it contradicted in the journal.
4. **EXPERIMENT** — `SelfDevService.experiment(request, *, timeout_s=1500) -> str`: a coder run in a
   temp worktree (the existing propose() mechanics and guards) with `experiment_prompt(request)`:
   "investigate, try, measure; write FINDINGS.md; ship nothing" — the branch and worktree are
   DISCARDED; only FINDINGS.md's text (capped) returns; a finding that recommends a change becomes a
   backlog idea.
5. **IMPROVE** — Phase 1's draft loop over the agenda's IMPROVE requests (research-derived ideas
   first), with verify + auto-apply as configured.
6. **RECORD** — the journal entry for the night; `discoveries` = the 1–5 most interesting things (a
   fact that changed a plan, a tool/library found, a capability drafted, a weakness fixed), each one
   sentence with its source; the self-model delta; the morning report (Phase 1's `morning_report`)
   now LEADS with the best discovery, then applied changes, then facts/experiments counts, then what
   is waiting for review — still one brief paragraph. Every 7th session also writes a weekly digest.

Time budget (baked defaults, not settings): reflect ≤ 10 min; research ≤ 30 % of the window;
verify ≤ 10 %; experiments ≤ 15 %; the rest improves; the last 20 min are reserved for RECORD and
wind-down. Every step is skipped cleanly when its input is empty (no research questions → straight to
IMPROVE). Nothing speaks aloud at night; everything is journaled.

Face + routes (F2): `GET /api/dream/journal` → the last 30 nights (date, discoveries, facts noted,
experiments, applied/held/failed counts, rebuild result, the report text). A "Dream journal" page
(`web/src/pages/Dream.tsx`, reachable from the Menu and from the Dreaming settings card): nights as
cards, discoveries first, each fact with its host and date, applied changes with one-line summaries.
Persona hook (F2 may add ONE sentence to the DREAMING paragraph in prompts.py only if F1 has finished
with that file; otherwise note it in open_issues): "what did you dream?" / "what did you learn last
night?" → dream_status + the journal's latest entry.

## 12. Quality bars (Phase 2)

1. **Verified means verified.** A `[verified]` finding or a noted fact carries a URL on an allowlisted
   host that was actually read in that turn; the store keeps the date and host; the morning report
   never presents an unverified claim as a fact.
2. **The leash holds.** `research_read` refuses non-allowlisted hosts (tested with lookalikes:
   `github.com.evil.net`, `evil-adafruit.com`, userinfo tricks); no cookies, no POST, no secrets, no
   arbitrary fetch; DREAM writes are unavailable to watcher agents; the fenced tools stay fenced.
3. **Experiments never ship.** `experiment()` discards its branch and worktree; only text returns.
4. **The night degrades gracefully.** No brain, no web, an empty agenda, a research turn that fails —
   each is one journal line and the session moves on; the window and the activity pause always win.
5. **It reads like discovery.** The journal and the morning report are written for Brian: specific,
   sourced, brief, and honest about what is still unverified.

## 13. Limits and model discipline (F2, with one shared helper F1 may also use)

Brian: "If the dream state runs out of limit, it should have a neat and easy way to handle this. The
solution is NOT to drop down to a low-end model that could mess the program up." Two rules:

**Rule 1 — Fable or nothing.** The dream plans, researches, and drafts on the growth model only:
`growth_model.work_model(deep=True)` for every coder run (the planner's `EFFORT: standard` line is
read but IGNORED at night — journaled as "standard suggested; drafting on Fable anyway"), and the
growth chat for reflection/research. If the growth model resolver cannot name a Fable-class model
(the pinned floor is missing from the plan, the resolver fell back below `claude-fable`), the session
does not start; `dream_status` says "Dreaming is paused: Fable isn't available on this plan right now"
and the Settings card shows it. No leg of the night may fall through to a weaker model: the dream
constructs its chats so that `PreferredChat`'s API-key fallback is never taken for dream work
(pass a chat whose fallback is None, or check `subscription.active()` before each step and treat
inactivity as a limit) — a downgrade is treated exactly like a limit (below), never as success.

**Rule 2 — A limit pauses the night; it never degrades it.** `helix/services/limits.py` (F2 owns it;
F1 may import it): `looks_like_limit(text) -> bool` (case-insensitive: "rate limit", "rate_limit",
"429", "usage limit", "hit your limit", "limit reached", "quota", "overloaded", "resets at",
"try again in", "too many requests", "capacity") and `reset_hint(text) -> str` (the "resets at 3pm" /
"try again in 2 hours" phrase when present, else ""). The lane's failure text, the coder's error, and
any exception text from a research/reflect turn go through it. On a limit:
- the session enters PAUSED: the journal gets "paused at 02:14 — the plan's limit was reached
  (<reset hint>); waiting for it to reset"; `dream_status` and the console chip say "dreaming — paused
  for the plan's limit"; nothing else is attempted for a backoff of 20 minutes, then 30, then 45, then
  every 60 (capped), each retry a cheap probe (the planner call, or the pending step itself) — never a
  new draft on a probe that fails;
- the window still rules: when it ends, the session winds down as usual, and the morning report says
  plainly "The plan's limit was reached at 02:14; I paused and resumed at 05:10 / did not get to
  resume — 2 of 6 planned improvements ran." Nothing is lost: the remaining agenda is written to the
  journal and the improvement backlog so tomorrow night starts from it;
- a draft that failed mid-way for a limit is journaled as "held: limit" (not as a failure of the
  idea) and its branch, if any, is discarded so a half-drafted change never waits for review;
- three limit pauses in one night end the session early (journaled), so a broken plan never spins
  all night.
Tests (F2): `looks_like_limit`/`reset_hint` matrix; a lane error that looks like a limit → PAUSED,
backoff sequence with a fake clock, resume after a successful probe, wind-down at the window end while
paused, the morning report wording, the remaining agenda preserved, three pauses end the night; the
Fable-or-nothing gate with a resolver that reports a sub-Fable model; `EFFORT: standard` ignored.


## 14. Sleep-talk — HELIX talks in its sleep (2026-09-05)

A dreaming HELIX murmurs: short, half-formed, lowercase fragments about the exact moment it is in — the
page it is reading, the part it is measuring in its head, the draft it is writing, the doubt it can't put
down. `services/murmur.py`.

- **Two sources, one voice.** Every big model call a night makes (REFLECT, each research and verify
  turn, the plan fold) ends with one extra line, `MURMUR: …`, asked for by `MURMUR_INSTRUCTION`
  (appended to the prompt; the research system prompt allows it after the FINDINGS shape). Zero extra
  calls: the murmur is written by the same thinking it is about. `take_murmur` lifts it off the reply
  before `parse_reflection`, `parse_findings` or `parse_plan` run (`Reflection.murmur`,
  `Findings.murmur`). Moments with no model call — a step starting (`start_murmur`), a draft starting,
  landing, held, failed; a limit pause; the user walking in; a new round; the session's start and end —
  get deterministic templates from the session's notes (`murmur_for_note`; the variant is picked by a
  hash of the line, so a night is reproducible). Bookkeeping notes stay silent.
- **The session says it.** `DreamService._murmur` keeps each on the night's record (`murmurs`, capped at
  120, with a clock stamp and its kind — "mind" or "note"), paces template murmurs to one per twenty
  seconds of the session's clock (a burst of notes is one breath; the mind's own words always land),
  and publishes `DreamMurmur`. The mind reaches the session through `NightHooks.murmur`.
- **The face hears it.** The shell pushes `{"t": "murmur", text, kind, at}` and carries the last one on
  the snapshot; `store.murmur` (a climbing `seq`) drives the `SleepTalk` line above the status pill and
  the orb's REM flicker. The journal card lists them under its notes as SAID IN ITS SLEEP.
- **Whispered only to someone who is there.** `ShellSession._murmur_aloud`: a manual "dream now" (the
  user asked, so they are at the keyboard), or a user who spoke within the last ten minutes; then
  `WebVoice.murmur` — only while idle and unmuted, never over a reply, a note or a listening mic — and
  `EdgeSpeechOut.murmur`: the user's own voice a fifth slower, well under half the volume, a touch lower
  (edge-tts rate / volume / pitch), once, never the OS fallback. The echo shield knows the words.
- **Never a secret.** Every murmur is scrubbed (`limits.scrub_secrets`) before it leaves the engine.
- **The sleeping star.** `Orb.tsx`: `STATE_LOOK.dreaming` (indigo-violet, dim, slow) whenever a session
  runs and nothing has woken the orb; `uDream` stills the storm (no strikes, the tendrils asleep), slows
  and deepens the breath, folds the corona in, and threads a teal AURORA (`DREAM_AURORA`) through the
  plasma on its own clock; `uRem` (`DREAM_REM`, rose) flickers on each murmur and, rarely, on its own.

## 15. Rounds — a night is several passes, each deeper (2026-09-05)

A pass that finishes with time left is not the end of the night. When the six cycles return with at
least 45 minutes of window left — the agenda drained, or the round's draft ceiling reached — the session
asks the mind again (`NightHooks.round_no` = 2, 3, …). The reflect prompt names the round and points at
tonight's own journal entry (saved as it goes, so it is in the DREAM JOURNAL material): never repeat the
earlier rounds' questions, checks, experiments or requests; go deeper on what they found, or take the
next most valuable thing; QUIET if nothing is worth another pass. `dream_max_drafts` bounds a ROUND; the
window bounds the night (and a fuse of twelve rounds, whatever the clock says). Each round's lists land
on top of the earlier rounds' — `_record_round` appends research, verify, experiments, facts and
discoveries (deduped by text) and cycles, adds counts, keeps the first round's theme and the weekly
digest (written on the first round only; the nights counter is bumped once) — so the morning report and
the journal card read the whole night. A later round that reflects and finds nothing ends the night
with the last WORKING round's reason; a stop, the window, or drafts that kept failing end it as before.
The status line says "round N" while a later round runs; the journal card says "N rounds". The Evolve
pass — one proposal a night, always human-approved — was retired the same day; its backlog and material
live on in `services/backlog.py`.
