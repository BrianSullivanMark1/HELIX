# Dreaming — the nightly self-improvement session

Status: SPEC + build contracts (2026-09-04). Two workstreams build to this document at once in the
`dream/session` branch (worktree `C:\Users\brian\HELIX_dream`); the contracts are binding so the
pieces meet. When it lands, this file gains a "What shipped" section.

## 0. Why, and what exists

Brian: "HELIX should feel like it is getting significantly better each day. Let me set a sleep time for
a daily 8-hour dream session where the program self-improves non-stop; I must be able to disable it;
the model should be Fable."

What exists (read it before touching anything): `helix/services/evolve.py` — ONE proposal per night
(hour ≥ 3, `evolve_last_run` stamp, `evolve_enabled` toggle, a catch-up band), drafted through the
same lane `improve_helix` uses (`helix/services/selfdev_lane.py` → `SelfDevService.propose` in an
isolated git worktree on a `selfdev/…` branch, constitution-scanned, never self-applied); the human
approves with `approve_self_change` → `SelfDevService.approve` (constitution re-check, a non-executing
compile smoke check, a revertible `--no-ff` merge into the base branch, "restart to load it"); the
startup self-heal in `helix/app/bootstrap.py` rolls the source back to the last commit that booted if
a merged change bricks startup. The growth model (`helix/adapters/model_select.py`
`GrowthModelResolver`) pins Fable 5 as the floor and upgrades automatically; Evolve plans on
`growth_chat` and sizes the coder with `work_model(deep)` (deep = Fable).

**The hidden break.** In the FROZEN desktop app (the one on Brian's icon), `AppPaths.root` is the
install folder `dist/HELIX` next to the exe — which is not a git repository. `SelfDevService` is
constructed with that root, so every overnight draft would fail at the first git call, and a merged
change could never reach the running exe anyway (its code is the bundled `_internal`). Evolve has
been logging "a night is owed; holding it for a quiet hour" for days. The dream session must draft
against the SOURCE repository that built the exe, and — when a change is applied — rebuild the app
and relaunch it before the user wakes.

## 1. Workstreams (both run in the `C:\Users\brian\HELIX_dream` worktree, disjoint files)

| Workstream | Owns | Must not touch |
|---|---|---|
| **E1 — Engine** | NEW `helix/services/dream.py`, NEW `helix/adapters/rebuild.py`, NEW `scripts/rebuild_and_relaunch.py`, `helix/services/evolve.py`, `helix/services/selfdev.py`, `helix/services/selfdev_lane.py`, `helix/config.py`, `build.py`, `helix/app/container.py` (wiring only), `helix/app/webboot.py` (the quit-for-rebuild hook only), `helix/adapters/model_select.py` (only if needed), NEW `tests/test_dream.py`, NEW `tests/test_rebuild.py`, `tests/test_evolve.py`, `tests/test_selfdev*.py` | tools.py, prompts.py, conversation.py, shell.py, server.py, web/ |
| **E2 — Face, tools, voice** | `helix/services/tools.py`, `helix/services/prompts.py`, `helix/services/conversation.py`, `helix/domain/vocabulary.py`, `helix/api/shell.py`, `helix/api/server.py`, `web/src/pages/Settings.tsx`, `web/src/lib/store.ts`, `web/src/App.tsx` (a small status chip only), NEW `tests/test_dream_tools.py`, `tests/test_prompts.py`, `tests/test_webshell.py` (extend), `READ_ME/ARCHITECTURE.md` (§8b), `READ_ME/README.md`, this file's "What shipped" | dream.py, rebuild.py, evolve.py, selfdev.py, config.py, build.py, container.py |

House rules: Python 3.11; tests with `python -m pytest tests/<file> -q -p no:cacheprovider -W ignore`
run from the worktree root; the web builds with `cd web && npm run build` (run `npm ci` once in `web/`
if `node_modules` is missing there). Never commit or push (the orchestrator merges), never touch
`data/` or `%LOCALAPPDATA%\HELIX`, never run `build.py`, never start/stop the live HELIX app. Write
files with Write/Edit (no shell heredocs). Every behavior gets a test. Text the user hears is plain and
honest.

## 2. Settings (the contract; `helix_settings.json` keys, all read live)

| key | type | default | meaning |
|---|---|---|---|
| `dream_enabled` | bool | false | the nightly session runs at all |
| `dream_start` | "HH:MM" | "23:00" | local time the window opens |
| `dream_hours` | number 1–12 | 8 | window length |
| `dream_auto_apply` | bool | false | merge a drafted change WITHOUT waiting for the morning, but only when the FULL test suite is green on it (§4) |
| `dream_rebuild` | bool | true | after a session that applied changes, rebuild the frozen app and relaunch it (frozen builds only; dev = "restart needed") |
| `dream_max_drafts` | int 1–30 | 10 | ceiling on drafts per session |
| `dream_last_session` | ISO date | — | one session per calendar day (stamped when a session starts) |
| `dream_report_pending` | bool | — | the morning report has not been delivered yet |
| `source_root` | path | — | frozen only: the source repo self-changes draft against (auto from build_info) |
| `dev_python` | path | — | frozen only: the interpreter that runs tests and build.py (auto from build_info) |

`evolve_enabled` keeps its meaning (the one-proposal nightly pass). When `dream_enabled` is on, the
dream session REPLACES the nightly pass for that night (Evolve's `tick` defers to the dream when a
session is enabled for the day — no double drafting).

## 3. build_info + source root (E1)

`build.py` writes `helix/build_info.json` into the bundle (`--add-data`) with
`{"source_root": "<abs repo path>", "python": "<sys.executable at build time>", "sha": "<git HEAD>",
"built_at": "<iso>"}`. `helix/config.py` gains `build_info() -> dict` (empty in dev or when missing) and
`AppPaths.source_root -> Path | None`: dev → `root`; frozen → the setting `source_root` if set, else
`build_info["source_root"]` — and only if that folder has a `.git`; else None. Likewise
`AppPaths.dev_python -> str | None` (dev → `sys.executable`; frozen → setting, else build_info, and
only if the file exists). The container constructs `SelfDevService` with `source_root or root`, and
`DreamService` refuses to run (journals why) when frozen without a usable source root — the settings
card (E2) shows that line.

## 4. The engine (E1: `helix/services/dream.py`)

```python
class DreamService:
    def __init__(self, chat, lane, selfdev, evolve, settings, clock, bus, *, paths, log_tail=None, suite_runner=None, rebuilder=None, activity=None)
    def tick(self) -> None                 # heartbeat (~15 s): start a due session; wind one down when the window ends or it's disabled
    def schedule(self, *, start: str | None = None, hours: float | None = None, enabled: bool | None = None) -> str   # validates, saves, returns a plain confirmation ("Dreaming nightly from 23:00 for 8 hours.")
    def dream_now(self, minutes: float = 30) -> str   # a session right now, bounded (for testing / "dream for an hour")
    def stop(self, reason: str = "the user asked") -> str
    def status(self) -> str                 # readable: enabled/window/next session/what's running/last session summary (never names a fenced tool)
    def morning_report(self) -> str | None  # the undelivered report, or None; delivering clears dream_report_pending
    def journal_tail(self, nights: int = 7) -> str
    @property running(self) -> bool
```

The session (a daemon thread, one at a time; everything journaled to `data/helix_dream.json` — sessions
with started/ended, plan, per-draft outcome, applied list, rebuild result — and mirrored as one line
per event in Evolve's journal so `evolve_report` still tells the story):

1. **Gate**: enabled; `now` inside the window; no session stamped today; a brain token exists; the lane
   is idle; frozen → `source_root`/`dev_python` usable. `dream_now` skips the window/stamp gates.
2. **Plan** on the growth model (`chat` = the container's `growth_chat`, Fable): `DREAM_PLAN_SYSTEM`
   reads the material (Evolve's backlog + lessons + log tail + the last nights' dream journal + a short
   repo map: the module list with line counts and test counts, built by the service from `source_root`)
   and returns up to `dream_max_drafts` improvement requests, ranked by what would make HELIX feel
   better tomorrow, each a self-contained change request (the shape `improve_helix_prompt` expects)
   with an `EFFORT: deep|standard` line (default deep). Backlog items come first. "QUIET" = nothing.
3. **Draft loop** — for each request while the window holds and the ceiling isn't hit: wait while the
   user is active (`activity()` → seconds since the last user turn; hold while < 10 min, and never
   start a draft in the window's last 20 minutes); `lane.start(request, model=growth_model.work_model(deep), unattended=True)`;
   wait for the lane (poll `busy()` every 5 s; a draft has 40 min); read the outcome from the bus
   (`SelfChangeFinished`); journal `drafted <branch>` / `failed: <reason>`.
4. **Apply** (only when `dream_auto_apply`): `selfdev.verify(branch)` (§5) runs the FULL suite on the
   branch in a fresh worktree; green → `selfdev.approve(branch)`; journal `applied <branch>` or
   `held: tests failed (<n failed, first: …>)` (a failing draft stays pending for the human). Red
   drafts never merge. `approve` may refuse (constitution/smoke/dirty tree) — journal that plainly.
5. **Reflect** once mid-session (after half the ceiling or half the window): re-plan with the outcomes
   so far appended; the planner may drop, reorder, or add requests.
6. **Wind down**: at the window end, or on `stop`, or when disabled mid-session: cancel a running draft
   (`lane.cancel()`), stamp `dream_last_session`, set `dream_report_pending`, write the summary. If any
   change was applied and `dream_rebuild` and frozen → `rebuilder.schedule(...)` then request the app's
   graceful quit (§6). In dev, journal "restart needed to load N applied changes".
7. **Morning report** text (E2 delivers it): one plain paragraph — how many drafts, applied (with
   one-line summaries), held for review, failed, whether HELIX was rebuilt, and one sentence about the
   night's theme. Example: "Last night I drafted 6 improvements and applied 4 (the camera panel
   remembers its last device; sleep phrases now cover 'nap time'; …). Two are waiting for your review.
   I rebuilt and relaunched at 6:41."

`EvolveService.tick` → early-return when `dream.covers_tonight()` (a dream session is enabled for
today's window) so the two never both draft. Keep every existing Evolve test green.

## 5. Verification before an unattended merge (E1: `SelfDevService.verify`)

`verify(change_id, *, timeout_s=1200) -> tuple[bool, str]`: a fresh worktree of the branch (same
mechanics as `_smoke_in_worktree`), then `<python> -m pytest tests -q -p no:cacheprovider -W ignore -x`
with `python = paths.dev_python or sys.executable`, cwd = that worktree, output captured; returns
(all green, the last ~30 lines). The web bundle is not rebuilt here (build.py rebuilds it). A draft that
changed `web/` also runs `npm run build` there when `npm` is on PATH and `node_modules` exists in the
worktree (skip — don't fail — when it can't). Keep `approve()` for humans exactly as it is; add a
keyword `verified: bool = False` that appends "(full test suite green)" to its returned line.

## 6. Rebuild + relaunch (E1: `helix/adapters/rebuild.py` + `scripts/rebuild_and_relaunch.py`)

`Rebuilder(paths, settings)`: `available() -> bool` (frozen, `source_root` and `dev_python` usable, the
script exists in `source_root/scripts`), `schedule(*, reason: str) -> Path` writes a job JSON under
`data/rebuild/` (`{"source_root", "python", "exe", "data_dir", "port", "token", "launch", "reason", "requested_at"}`
— `launch` is the desktop shortcut path if `%USERPROFILE%\OneDrive\Desktop\HELIX.lnk` or
`%USERPROFILE%\Desktop\HELIX.lnk` exists, else the exe) and spawns
`<python> <source_root>/scripts/rebuild_and_relaunch.py <job>` DETACHED (new process group, no console,
stdio to `data/rebuild/rebuild.log`), then returns; the CALLER quits the app (webboot's `graceful_quit`
via a bus event `RebuildRequested`, which the web shell answers by calling the quit hook — E1 adds the
event and the hook in webboot; E2 wires nothing here). The script: wait until no `HELIX.exe` runs (≤
180 s; else abort and log), back up `dist/HELIX` → `dist/HELIX.prev` (replace an older .prev), run
`build.py` (cwd = source_root, log captured, ≤ 40 min), on success start `launch` and wait for
`http://127.0.0.1:<port>/api/snapshot?t=<token>` to answer 200 (≤ 240 s); on a build failure or a
non-answering app, restore `dist/HELIX.prev` → `dist/HELIX` and start it; write
`data/rebuild/last_result.json` (`{"ok", "built", "restored", "seconds", "message", "at"}`) that the
morning report reads. Pure-Python stdlib only (it runs outside the app). Tests drive the script's
functions with fakes (no real build).

## 7. Tools, persona, face (E2)

Tools (fenced = in `BUILD_TOOLS`): `dream_schedule(start?, hours?, enabled?)` (fenced) → `dream.schedule`;
`dream_now(minutes?)` (fenced) → `dream.dream_now`; `stop_dreaming()` (fenced) → `dream.stop`;
`dream_status()` (readable) → `dream.status`. Spoken phrases in `vocabulary._TOOL_PHRASES`. Voice
shapes the persona teaches: "dream tonight from eleven for eight hours", "no dreaming tonight", "stop
dreaming", "dream for an hour now", "how did you sleep?" / "what did you dream?" → `dream_status` and
the morning report. Persona paragraph "DREAMING" after "YOU GROW": what a dream session is, that it
plans and drafts on Fable, that applied changes are test-gated and the app rebuilds at dawn when set
so, that the user can disable it any time, and that HELIX tells the morning report once, briefly, when
first spoken to after a session — never in the middle of a task, never twice.

Morning report delivery (E2, `shell.py`): on the first user submission after `dream.morning_report()`
returns text, prepend it as its own HELIX bubble (spoken when voice is on), then run the turn as usual;
also show it on the Settings card. Heartbeat: `self.c.dream.tick()` beside `evolve.tick()`. Events:
`dream` status pushes (`{"t": "dream", "running": bool, "line": str}`) when a session starts/ends so
the face shows a small "◐ dreaming" chip on the console (App.tsx) while it runs.

Settings card (E2, `Settings.tsx`, a new "Dreaming" section after "HELIX"): enabled toggle, start time
(`<input type="time">`), hours (1–12), "Apply green changes automatically" toggle with one plain
warning line, "Rebuild and relaunch after applying" toggle, max drafts, a line "Plans and drafts on
Fable (the growth model)", the status line from `dream_status` (next session / last session summary /
the frozen-without-source-root warning), and a "Dream for 30 minutes now" button (`POST
/api/dream/now`). Routes: `_SETTING_KEYS` gains the six dream keys; `GET /api/dream` → `{status, running,
report}`; `POST /api/dream/now` `{minutes}`; `POST /api/dream/stop`.

## 8. Quality bars

1. **Nothing merges red.** An unattended merge happens only after the full suite passed on that exact
   branch; a red draft is held for the human with the failure named.
2. **The window is the window.** No draft starts outside `[start, start+hours)` except `dream_now`;
   disabling stops a running session within one heartbeat; the user's activity pauses it.
3. **Rebuild is reversible.** The previous build is kept and restored automatically if the new one
   fails to build or to answer; the result is reported in the morning.
4. **Frozen truth.** The frozen app drafts against the source repo through `source_root`; when that
   isn't possible the session says so instead of pretending.
5. **Fable.** Planning and drafting use the growth model (`work_model(deep=True)` for the coder);
   `dream_status` states the model in use.
6. **Suite green, web builds clean, existing Evolve/self-dev tests untouched in meaning.**

## 9. What shipped — E2, the face (2026-09-04)

Built to the contracts above against a fake engine; E1's `DreamService` lands in the same merge.

- **Tools** (`helix/services/tools.py`): `attach_dream(dream)` late-binds the engine (like
  `attach_evolve`). `dream_schedule(start?, hours?, enabled?)` → `dream.schedule(start=, hours=,
  enabled=)` with only the named fields (a partial call keeps the saved values; garbage numbers read as
  absent and the tool asks instead of calling; the engine's `ValueError` is relayed as a plain sentence);
  `dream_now(minutes?)` → `dream.dream_now(minutes)` (default 30); `stop_dreaming()` → `dream.stop("the
  user asked")`; `dream_status()` → `dream.status()` with the three fenced names scrubbed from the text as
  a backstop. The three controls sit in `conversation.BUILD_TOOLS`; `dream_status` is readable.
- **Voice**: phrases in `domain/vocabulary._TOOL_PHRASES`; the persona's DREAMING paragraph in
  `prompts.CONSOLE_SYSTEM` right after YOU GROW — the shapes, Fable, the test gate, the dawn rebuild, the
  Settings switch, and the report told once, never mid-task (pinned in `tests/test_prompts.py`,
  placement included).
- **Shell** (`helix/api/shell.py`): `dream.activity = shell.seconds_since_activity` (a submission, a
  tap, and a stop all count); `dream.tick()` beside `evolve.tick()`; the `{"t": "dream", "running",
  "line"}` push on start/end — subscribes to `helix.domain.events.DreamStateChanged(running, line)` when
  that class exists (guarded import), else polls `dream.running` per heartbeat; both paths dedupe through
  one state. The morning report on the first user turn: taken once from `morning_report()` (never while a
  turn is busy — a queued message delivers it when its turn starts), its own bubble ahead of the reply,
  spoken joined ahead of the reply's speech, and named in that turn's self-situation block. Also
  `dream_state()`, `dream_now()`, `dream_stop()`, `dream_settings_changed()` for the routes; the snapshot
  carries `dream`; a mid-dream draft bubble drops the apply prompt; a plain stop mid-dream points at
  "stop dreaming"; the self-situation block says when a session is drafting.
- **Routes** (`helix/api/server.py`): the six keys in `_SETTING_KEYS`; `dream_setting()` defaults on GET
  and `read_dream_setting()` coerces on PUT (types and ranges) — an unreadable clock or number is refused
  and the stored value kept, answered as `{ok, changed, rejected: {key: why}}` in the engine's words, as
  `schedule()` saves nothing; `GET /api/dream` → `{available, status, running, line,
  report, frozen_without_source, model}`; `POST /api/dream/now {minutes}`; `POST /api/dream/stop`; a save
  that touched a dream key tells the shell to push the state at once. `frozen_without_source` is
  computed as `sys.frozen and AppPaths.source_root is None` (no dependency on the status wording).
- **Face**: `store.ts` (`DreamState`, the `dream` event), `App.tsx` (the "◐ dreaming" chip, top right
  under the nav, opens Settings; the snapshot seeds it), `Settings.tsx` (the Dreaming card per §7, live
  through the event stream).
- **Tests**: `tests/test_dream_tools.py` (offer/dispatch/fence/scrub/phrases against a fake engine),
  `tests/test_prompts.py` (the DREAMING bullet and its placement), `tests/test_webshell.py` (the activity
  clock, the report once and never mid-task, the spoken order, the heartbeat and the event, the card's
  reads and buttons, the routes over plain ASGI, the settings coercion matrix).

Merge notes: the container must call `tools.attach_dream(container.dream)` after constructing the
engine; the shell reads `container.dream`, `container.paths.source_root` and
`container.growth_model.resolve()`, tolerating each missing. `GET /api/dream`'s `report` is the last
report the shell TOLD, else the one still waiting — peeked through the engine's `pending_report()`, which
never consumes it (only `morning_report()`, on the first user turn, does); the card's status line carries
the last session summary either way.
