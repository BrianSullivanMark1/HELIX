# FORGE_INFO — how THE FORGE works

Written 2026-09-07 against build `2026-09-06e`. Every claim below was checked
against the code (`server.py`, `static/index.html`, `new_project.py`,
`scan_projects.py`, `duck-radio/`, the launchers, and the `command-center`
skill), not copied from the older docs. Where the code and the older docs
disagree, this file follows the code and says so in §19.

**Updated 2026-09-15/16.** Three changes, all checked against the code the
same way: every project moved off the `git_source` CI deploy style (§8 — this
is the one that was silently breaking deploys), HOW TO START gained a sixth
option (§12), and the WHAT TO TEST bench grew a waking state (§10). Changed
paragraphs carry their own date so a later reader can tell what is original
and what was patched in.

---

## 1. What it is

THE FORGE (formerly DEVELOPMENT COMMAND CENTER) is a local release console for
Brendan's 18 dashboard projects. It is one Python file (`server.py`, stdlib
only, no pip packages) serving one HTML file (`static/index.html`) at
`http://127.0.0.1:8877`. From it you can see git status of every project,
Save (commit + push a draft), Save & GO LIVE (push main → GitLab CI deploys to
Databricks Apps), roll back to a previous deploy, approve merge requests,
manage Databricks secret scopes, run a project locally on a test bench, create
and delete whole projects, and have local Claude Code make changes to a
project through a conversation.

The projects live in `C:\Users\brendans\Claude\Projects\*` (17 of them as of
2026-09-16, alongside THE FORGE's own folder) plus
`C:\Users\brendans\Desktop\METROLOGY_AGENT\DIMENSIONAL METROLOGY AGENT` — 18
in total, and that Desktop one is why anything sweeping "every project" must
go through `discover_projects()` rather than globbing `Projects\` (§8). Each
is a GitLab repo under `gitlab.com/rivian/dimensional-metrology/` and a
Databricks App at `<slug>-4136265416458520.aws.databricksapps.com`. THE FORGE
is itself a repo: `rivian/dimensional-metrology/the-forge`.

---

## 2. Folder layout

| Path | What it is |
|---|---|
| `server.py` (6411 lines) | The whole backend: HTTP handler, job runner, file-queue watcher, git/GitLab/Databricks/Claude helpers. `BUILD = "2026-09-06e"` near line 6186. |
| `static/index.html` (16,634 lines, ~908 KB) | The whole UI: CSS + markup + JS in one file. `PAGE_BUILD` must equal `BUILD`. |
| `static/audio/*.mp3` | 24 shop-soundtrack tracks for FORGE RADIO (gitignored). |
| `new_project.py` | Scaffolds a new dashboard project. Run by the `create_project` queue action, never imported. |
| `scan_projects.py` | Prints project status JSON + `server_alive`. Used by the Cowork `command-center` skill. |
| `duck-radio/` | Shared front-end blocks (radio player + Dashboard Docs) baked into every dashboard, plus `apply.py` to re-apply them. Moved here from TORQUE BYPASS DASHBOARD on 2026-09-06. |
| `queue/` | File queue: `pending/`, `done/`, `uploads/`, `duck_requests/`, `heartbeat.txt` (gitignored). |
| `wt/` | Temporary git worktrees used for rollbacks (gitignored). |
| `*.json` | Server state — see §16. |
| `gitlab_token.txt` | GitLab group access token `forge-console` (Owner role, `api` scope). Gitignored; never in a repo or a queue job. |
| `LAUNCH_COMMAND_CENTER.bat`, `LAUNCH_THE_FORGE.command`, `FORGE_WATCHDOG.bat`, `CREATE_DESKTOP_SHORTCUT.bat` | Launchers — §3. |
| `forge_anvil.ico/.svg`, `command_center_radar.ico` | Icons (radar one is the old duck). |
| `bench/` | Throwaway HTML design mockups (gitignored). |
| `*.md` | Docs — §18. |

---

## 3. Running it

**Windows.** `CREATE_DESKTOP_SHORTCUT.bat` makes a `THE FORGE.lnk` on the
Desktop that runs `cmd.exe /c LAUNCH_COMMAND_CENTER.bat` (targeting cmd.exe so
it can be pinned to the taskbar). The launcher finds Python in this order:
`pythonw.exe` beside `py -3`'s interpreter (windowless, preferred) → `where
pythonw` → `py -3` → `python` → WSL `python3 --no-browser`. Pass `-v` for a
visible console that runs `server.py --no-idle-quit`. The launcher itself does
not check the port or open a browser; `server.py` owns both.

**macOS/Linux.** `LAUNCH_THE_FORGE.command` does `nohup python3 server.py &`.
Never actually run on a Mac; the code paths are cross-platform via
`login_shell()` (WSL on Windows, bash elsewhere).

**Startup (`main()`).** Binds `ThreadingHTTPServer` to `127.0.0.1:8877`
(`DCC_PORT` env overrides). **The port is the single-instance lock**: if the
bind fails and `/api/ping` answers, the new process records a collision in
`instances.json`, opens the console in the browser, and exits. Threads started:
`queue_watcher` and `idle_watchdog`. Then it opens the console via
`open_console()`: a **browser tab by default**; `settings.console_window =
"app"` opens a Chrome/Edge `--app=` window instead (the only mode in which
"Close THE FORGE" can close its own window).

**Idle quit.** `ALIVE` is stamped on every HTTP request. The page sends
`sendBeacon("/api/bye")` on `pagehide`/`beforeunload`. The watchdog quits the
server 12 s after a bye, or 150 s after the last request if no bye was seen,
but never while a job is running, a `queue/pending/*.json` exists, or a test
bench process is alive. `--no-idle-quit` disables it.

**Auto-relight.** `FORGE_WATCHDOG.bat` polls `/api/settings` every 60 s and
restarts `python server.py` if it fails. Drop a shortcut to it in
`shell:startup` if you want that.

**Build stamp.** `buildCheck()` fetches `/api/ping`; if `build !== PAGE_BUILD`
an amber bar (`#stalebar`) says the page is newer than the server and needs a
restart. Any `{error:"not found"}` from `api()` also triggers it. **Bump both
constants whenever the page starts calling a new endpoint.** Server changes
need a restart; page changes need only a refresh.

---

## 4. Architecture in one breath

- **HTTP**: `class Handler(BaseHTTPRequestHandler)`. `do_GET`/`do_POST` are a
  flat if/elif chain on `self.path`; no router. Only `/` and `/index.html`
  serve static HTML. Every other file-serving route (`/audio/<f>`,
  `/api/dev-file`, `/api/prompt`) uses `Path(name).name` to block traversal.
- **Jobs**: `JOBS` dict in memory (never evicted). `new_job(title)` →
  `job_log(jid, line)` → `job_done(jid, ok)`. Extra fields: `help = "auth" |
  "conflict"`, `conflicts`. `run_streaming()` pipes a child process line by
  line into the log and registers the child so `/api/job-stop` can kill it
  (`taskkill /F /T` on Windows — WSL and git spawn children). One
  `threading.Lock` per project path, so jobs on different projects run in
  parallel and a second job on the same project logs `-- waiting --`.
- **Page**: `MODE === "app"` gates all the live behaviour. `load()` polls
  `/api/projects` every 60 s, `pollJobs()` polls `/api/jobs` every 3 s (and
  this is what keeps `ALIVE` fresh), approvals every 60 s, `healthSweep()`
  hourly. `api(path, opts)` wraps fetch and normalises dead/stale servers.
- **Every git the console runs** goes through `git_env()`: `GIT_TERMINAL_PROMPT=0`,
  `GCM_INTERACTIVE=never`, no askpass, `credential.interactive=never`. It
  cannot open a credential window (theme §32). Native git if on PATH, else
  `wsl --cd <path> -e git`.

---

## 5. Projects and status

`discover_projects()` lists every folder under `PROJECTS_DIR` except
`DEVELOPMENT_COMMAND_CENTER` and dotfolders, plus `EXTRA_PROJECTS`.
`/api/projects` runs `project_status()` over them in an 8-thread pool. Per
project: `is_git`, `branch`, `ahead`/`behind`, `changes[]`, `last_commit`,
`created`, `url` (scraped from `URL="…"` in `deploy_dbx_wsl.sh`), `has_dbx`
(that script exists), `last_deploy` (`deploys.json`), `deploy_stale` (dirty
tree or a commit newer than the last deploy), avatar, outreach.

Card pills, verbatim: `no git` · `✎ N unsaved` / `✓ all saved` · `↑N to push`
· `🦢 awaiting reply` (from `outreach.json`, status `waiting`) · `👑 N to
approve` (open MRs, click → approvals inbox) · pipeline chip after a ship ·
`⟳ working…` · `▲ live app outdated` / `✓ live & current`.

Card controls: OPEN APP (when `url`), TEST flask, expand → changed-files list
with a COPY button (reads from the `projects` array, not the DOM), `⚒ DEV`,
`⑉ Git`, `⏱ Versions` (when `has_dbx`), `📁 Open` (folder or any installed
editor: VS Code / Cursor / PyCharm / Sublime / Notepad++; choice saved in
`settings.editor`), bin (delete), commit-message box + **Write it** (AI
message) + **Save** + **Save & GO LIVE** (when `has_dbx`). Right-click the
avatar to recast the mascot.

Header: rotating logo, filter box with suggestions, New Project (hammer),
FORGE CHAT 🔥, FORGE VOICE, FORGE WORKS, THE ANVIL (terminal), ↑ Push ALL,
▲ Deploy ALL, reload, FORGE RADIO, hamburger → **Look & feel / Settings /
About THE FORGE / Close THE FORGE**.

---

## 6. Three ways to drive it

1. **The browser page** — calls `/api/*` directly; long operations return a
   job id and the page watches it in the drawer.
2. **Claude in chat** (the `command-center` Cowork skill) — cannot reach
   gitlab.com or Databricks auth from the sandbox, so it writes a job file to
   `queue/pending/<id>.json` and polls `queue/done/<id>.json`. Status comes
   from `python3 scan_projects.py`; liveness from `queue/heartbeat.txt` being
   under 15 s old. If the server is off, the job waits and runs on next start.
3. **Scheduled tasks / side-panel artifact** — tasks named `dcc-push-<slug>`,
   `dcc-deploy-<slug>`, `dcc-push-all`, `dcc-deploy-all` each write a queue
   job (the artifact bridge cannot call tools). `mallard-approvals` DMs new MRs
   and turns APPROVE/DECLINE replies into `approve_mr`/`decline_mr` jobs. New
   projects need their own pair of tasks created.

---

## 7. The file queue

`queue_watcher()` loops every **2 s**: writes the epoch to
`queue/heartbeat.txt`, picks up `pending/*.json` (sorted), **deletes the file
immediately**, and starts `run_queue_job(job)` on a thread. Every ~60 s it also
sweeps stale `.git/index.lock` files (older than 5 min) across all projects,
and prunes `done/*.json` older than 24 h. The loop swallows every exception —
the watcher must never die.

**Job file** (`queue/pending/<id>.json`):

```json
{"id": "<id>", "action": "push|deploy|save|ship|deploy_all|…",
 "project": "<EXACT folder name>", "message": "<commit message>",
 "model": "haiku|sonnet|opus|<full id>|"" (optional)"}
```

**Done file** (`queue/done/<id>.json`) is the raw job dict — `id, title,
status ("running"|"ok"|"failed"), log[], started`, plus `help`/`conflicts` —
rewritten once a second while the job runs, and once more 1.2 s after it ends.

**Actions the server accepts:** `push`, `deploy`, `save`, `ship`,
`deploy_all`, `forge_self`, `approve_mr` (needs `iid`), `decline_mr`,
`duck_build` (`sid`), `duck_update` (`sid`, `project`, `text`), `duck_save`,
`setup_forge`, `migrate_main`, `finish_main`, `generate_avatar` (`name`,
`prompt`), `create_project` (`name`, `description`, `size`, `secrets`). The
skill documents only `push`/`deploy`/`deploy_all`; the rest are used by the
page and the scheduled tasks. Unknown actions fail with `unknown action`.

`queue/uploads/` holds `create_project` attachments (`<qid>/`) and the spec
file handed to `new_project.py --spec` (`<qid>_spec.json` — a file, not args,
because Windows caps command lines at ~32K). `queue/duck_requests/` is written
only when the Claude CLI is missing (an avatar request parked for Claude in
chat); the server never reads it.

---

## 8. Git and deploy model

Plain-English buttons, no git jargon in the UI (theme §16).

| Action | Function | What happens |
|---|---|---|
| **Save** | `do_save` | `add -A`, commit; then push. On the default branch: **force-push `+HEAD:refs/heads/draft`** (nothing deploys). On a "line of work" (any other branch): normal push `HEAD:refs/heads/<line>`. Does not run `preflight_merge`. The success log line always says "draft" even on a line (cosmetic bug). |
| **Save & GO LIVE** | `do_ship` | CI mode on → retitles the job `ci deploy <name>` and delegates to `ci_deploy_one`. CI off → commit, `preflight_merge`, `push HEAD`, then `deploy_one`. |
| Push (queue/skill) | `do_push` → `_do_push_locked` | `add -A`, commit (tolerates nothing-to-commit), `preflight_merge`, `push <remote> HEAD`, one 5 s retry on `pre-receive hook declined`. In CI mode a real push also records a deploy/version. |
| Deploy | `do_deploy` | CI mode → `ci_deploy_one`; else `deploy_one`. |
| Deploy ALL | `do_deploy_all` | Sequential over every project with `deploy_dbx_wsl.sh`, `#### SUMMARY ####` at the end. |
| CI deploy | `ci_deploy_one` | Commit (default message `Deploy <Name> via GitLab CI`), `preflight_merge`, `push HEAD`. "Everything up-to-date" deliberately does **not** stamp a new deploy time. Otherwise `record_deploy` + `record_version` and logs the `/-/pipelines` URL. |
| Local deploy | `deploy_one` | Exactly `wsl --cd <project> -e bash ./deploy_dbx_wsl.sh` (or `bash ./deploy_dbx_wsl.sh` off Windows), streamed. On failure `explain_dbx_failure()` then `flag_secret_suspects()`. On success records `V<n>`. |

**All four commit-then-push paths** (`_do_push_locked`, `do_save`, `do_ship`,
`ci_deploy_one`) check the commit return code via `git_lockfix()` (retries once
on a young `index.lock`), distinguish "nothing to commit" from failure, and
never invent a deploy timestamp. Fix a bug in one → fix it in all four.

**Conflicts.** `preflight_merge` fetches the branch actually being pushed
(`cur_branch` or `default_branch`), merges `FETCH_HEAD`; on conflict it
captures up to 8 files' mine/theirs halves, `merge --abort`s, sets
`help="conflict"`, and the page opens the **DUCK SCUFFLE** panel
(`#scufpop`) → `/api/resolve` with `{file: "mine"|"theirs"}` → `do_resolve`.
A failed fetch is treated as OK (branch may not exist remotely yet).

**Auth failures.** `auth_failure_kind(out)` classifies git output as
`missing` or `rejected` credential (regexes tested against real GitLab
strings, including `HTTP Basic: Access denied … password or token is
incorrect` for a revoked token). `flag_auth_failure` sets `help="auth"` → the
Drill Sergeant popup (`#sgtpop`) with the fix for that kind.

**Default branch.** `default_branch(path)` asks the remote (`symbolic-ref
refs/remotes/<r>/HEAD`, then `ls-remote --symref`), caches 15 min, never
assumes `main`. `migrate_main`/`finish_main` are the master→main helpers.

**Lines of work** (`/api/line`, Git panel "LINES OF WORK" tab): `new`,
`switch` (stash / carry / pop), `request` (push + open an MR into the
approvals inbox), `delete`. Branching without the word "branch".

**Versions & rollback.** Every successful deploy appends to `versions.json`
(`{project: {versions:[{v, ts, commit, h, subject, dirty, rollback_of}],
live}}`) and `deploys.json` (`{project: epoch}`). `/api/versions` shows the
last 25 with the commit range between each. `/api/deploy-version` →
`do_deploy_version`: `git worktree add --detach wt/<name> <commit>`, copies
in the current `deploy_dbx_wsl.sh` if that commit predates it, runs the deploy
there, removes the worktree. **The working folder is never touched.**

**CI mode** (`settings.deploy_via_ci`, currently `true`). Every project has a
`.gitlab-ci.yml`: `stages:[deploy]`, `image: python:3.11`, `only: [main]`,
`tags: [kubernetes, cluster, tools]`, installs the Databricks CLI, prints
`databricks current-user me` (so the log names the identity), then one of two
styles chosen by `settings.ci_deploy_style`:

| `git_source` (retired 2026-09-15) | `sync` (**every project**, current setting) |
|---|---|
| `databricks apps deploy <app> --json '{"git_source":{"branch":"main"}}'` | `databricks sync --full . /Workspace/Shared/forge-apps/<app>` then `apps deploy <app> --source-code-path …` |
| Databricks clones the repo — the **app** must have a git repo configured and the identity needs a Databricks Git credential | Runner uploads its own checkout; no Git credential, no per-app UI setup |
| No guard for a brand-new app (first pipeline of a new project goes red) | `apps get` first; if the app doesn't exist, `exit 0` cleanly ("first deploy is done from THE FORGE") |

### The 2026-09-15 incident — why `git_source` is gone

Worth writing down, because the console's own pre-forge check was reporting
the opposite of the truth for about two weeks.

`settings.ci_deploy_style` had been flipped to `sync`, but **only 4 of 16
projects actually had a sync `.gitlab-ci.yml`** — the four scaffolded after
the flip. `do_ci_style` had never been run over the existing twelve, and it
only ever rewrites files locally, so the setting and the repos disagreed in
silence. Every `Save & GO LIVE` on those twelve pushed fine and then failed
in the pipeline with:

```
UNAUTHENTICATED: No Git credential configured, but credential required
for this repository (…/eines-GandF-dashboard-web).
```

That is Databricks refusing to clone, not a problem with the push, the code,
or the runner. It had been failing that way since the CI file was committed
on **2026-09-02**, and it was mistaken for fallout from the RPM migration —
which it was not: the RPM merge landed 2026-09-11, touched one project
(EDV COOLANT SCHEMATIC) and only its **Dockerfile**, which the CI deploy path
never builds. Changing the deploy style fixed it with no RPM change at all.

Twelve `.gitlab-ci.yml` files were rewritten to the sync style on 2026-09-15,
preserving each project's real app slug read out of its old CI file — those
are **not** derivable from folder names (`R1 G&F DASHBOARD` →
`r1-gap-flush-dashboard`, `TOOL CALIBRATION DASHBOARD` →
`tool-calibration-project`), which is why `app_slug()` reads the file first
and only falls back to slugifying. Confirmed working end to end on
EINES G&F DASHBOARD WEB, which also proves the CI identity can write to
`/Workspace/Shared/forge-apps`.

**That sweep had a hole, found 2026-09-16.** It globbed `Projects/*/`, so it
missed **DIMENSIONAL METROLOGY AGENT** — the one project that does not live
under `Projects\`. It sat on `git_source` (app `riv-dat`) for another day,
failing exactly like the rest. Rewritten 2026-09-16. Anything that sweeps
"every project" must use `discover_projects()`, which knows about the Desktop
path, rather than a glob of the Projects folder.

**New projects are not affected either way.** `new_project.py` carries its own
copies of both CI templates (`ci_template()`, chosen by
`settings.ci_deploy_style`) and its sync one is the rich version — DIMENSIONAL
DATA DECK, scaffolded 2026-09-15 after the sweep, came out correct with no
intervention.

**Two live traps this left behind**, neither fixed at time of writing:

1. `_dbxgit_needed()` decides whether the Databricks Git credential matters by
   reading `settings.ci_deploy_style` — *the setting, not the files*. That is
   exactly how the pre-forge check came to say "not needed with your setup"
   while twelve projects needed it and were failing. It should read the files.
2. `CI_SYNC_TEMPLATE` in `server.py` is the **bare** version. The files now on
   disk are richer — they carry the `apps get` guard and a plain-English
   message naming `/Workspace/Shared/forge-apps` when the sync cannot write.
   Pressing **Settings → Deploys → sync** would overwrite all 16 good files
   with the poorer template. Update the template before using that button.

`do_ci_style` (Settings → Deploys) rewrites every existing `.gitlab-ci.yml`
**locally only** — nothing is pushed. CI authenticates as whoever owns the
group variable `DATABRICKS_TOKEN`; a pipeline log showed `brendans@rivian.com`,
recorded as `settings.ci_identity` (an observation, not a guess; empty means
UNKNOWN). Which token produces that identity is still unknown.

**First deploy of a new app** always goes through the local
`deploy_dbx_wsl.sh` (creates the app, 3–5 min), regardless of CI mode.

---

## 9. GitLab integration

- `gitlab_api()` — `https://gitlab.com/api/v4`, `PRIVATE-TOKEN` from
  `gitlab_token.txt`, 25 s timeout. Project path derived from the remote URL
  (only if it contains `gitlab.com`).
- **Approvals inbox** — `get_approvals()` lists open MRs across all projects
  (30 s cache) and also dumps `approvals.json` for the Slack watcher.
  `approve_mr` = `PUT …/merge` (a real merge, source branch removed);
  `decline_mr` = `state_event: close`. Panel `#apprpop`; card pill `👑`.
- **Pipeline pill** — `/api/pipeline?name=` returns the latest pipeline
  status/url/sha; the card animates bar→hammer→armor until success/fail.
- **Token status** — `/api/token-status` (`/personal_access_tokens/self`,
  1 h cache) → `days_left`; the pre-forge check warns at ≤21 days.
- **`gitlab_can_delete(gpath)`** — reads the real access level and requires
  ≥50 (Owner). Nothing hardcodes which token is in the file; the delete panel
  adapts to what the token can do.
- **`do_forge_self`** — puts THE FORGE itself on GitLab (creates
  `rivian/dimensional-metrology/the-forge` via API if no remote), commits and
  pushes. `do_forge_pull` = stash, `pull --ff-only`, pop (FORGE WORKS panel).

---

## 10. Databricks integration

- `login_shell(cmd)` is the one choke point for shelling out (`wsl -e bash
  -lc` on Windows, `bash -lc` elsewhere). `dbx_cli(args)` runs the
  `databricks` CLI through it; `dbx_whoami()` parses `auth describe`.
- **Test bench** (`/api/test`, flask button): finds a free port, spawns
  `python server.py` in the project with `PORT=<port>` plus credentials from
  `dbx_local_auth()` — a short-lived OAuth token minted from **whoever is
  logged into the CLI on this machine** (`databricks auth token`, cached 30
  min, never written to disk). Second click stops it. Benches are reaped on
  exit. Card effect: click = quench (cold blue, steam); server answers =
  `testgo` sparks, then the tab opens.
- **Three bench states, not two** (updated 2026-09-15). `TESTBUSY[name]` is
  the middle one — spawned, waiting for the project's own server to answer.
  On a board card it is `.flaskbtn.waking` (locked, shaking flask, "WAKING").
  In the **WHAT TO TEST** panel it was missing entirely: clicking *Open the
  app* did nothing visible for a second or two and then a browser tab appeared
  out of nowhere. The panel's card (`.tstbench`) now fills like a progress bar
  while it waits, and shines when `/health` answers.

  The fill stops at **92%**, deliberately. Nothing knows how long a given
  project takes to boot — `testWait` polls `/health` and gives up at 45s — so
  a bar that reached the end and sat there would be claiming a finish that had
  not happened (§17). 100% is set from script, once the server has actually
  answered, and that is also the `shine()`.

  Mechanically: `tpBenchHtml()` draws the card and `tpBenchSync()` refreshes
  **only that card**, because a full `tpPaint()` mid-wait would rebuild the
  checklist underneath and throw away a note being typed into a failed row's
  textarea. `testBusy()` calls the sync, so the board button and the panel
  card always say the same thing. `tpBenchDone()` runs the bar to the end,
  lights it, and holds off the next sync for 1.4s so the beat is actually
  seen before the tab steals focus.
- **THE VAULT** (`#vaultpop`, Settings → OPEN THE VAULT): two-pane secret
  scope browser. Databricks records no scope creator, but `list-acls` is
  refused on scopes you don't manage, so `sweep_owners()` (background,
  cancellable, cached to `scope_owners.json`) sorts ~600 scopes into YOURS /
  read-only / others. Keys show expiry pills; Databricks stores only
  `last_updated_timestamp`, so expiry is derived: default 90-day life, warn at
  21, overridable per key in `secret_policy.json` (`never` / `days` / `date`).
  Until a rule is set the pill hedges (`~45 days left`). `secret_usage.json`
  (from grepping the project corpus) says which projects read which scope.
  Rotate / add / delete key and create / delete scope run as jobs
  (`/api/dbx-scope`), each with type-to-confirm. Values are never fetched or
  shown. `secret_alerts()` nags once per session about *your* scopes that
  something actually reads.
- **Deploy script** (`deploy_dbx_wsl.sh`, per project): preflight
  `databricks current-user me`; if `apps get` fails → `apps create
  --compute-size <SIZE>`, `apps update --json` to attach secret resources,
  `api patch /api/2.0/permissions/apps/<app>` granting group `users`
  `CAN_USE`; then always `databricks sync . <WS>` and `apps deploy <app>
  --source-code-path <WS>`. New projects use `WS=/Workspace/Shared/forge-apps/
  <slug>` (same path CI uses); the 13 older ones use
  `/Workspace/Users/brendans@rivian.com/<slug>`. Profile
  `rivian-prod-us-west-2`. Expired login fix: `databricks auth login --host
  https://rivian-prod-us-west-2.cloud.databricks.com` in WSL.

---

## 11. The AI layer (local Claude Code)

**Invocation.** `locate_claude()` finds the CLI natively (PATH, npm, `.local/
bin`, Programs) or in WSL (tries both `bash -lc` and `bash -ic`, because nvm
installs only show in interactive shells). `claude_exec(jid, prompt, extra,
cwd, timeout, stream, model)` always runs `claude -p <prompt> …`. In WSL the
prompt goes through a temp file `queue/claude_prompt_<hex>.txt` and
`$(cat …)` because quoting through wsl.exe is unwinnable. `--model` comes from
the per-call value, else `settings.claude_model` (currently `opus`);
`norm_model()` accepts `haiku|sonnet|opus` or a full id and silently drops
anything else. rc 127 = not installed → `/api/setup-forge` installs it.
`needs_login()` detects the "Not logged in" reply and opens a login terminal.

**Conversations (`/api/converse`).** One turn per call, `--output-format
json`, `--resume <sid>` when continuing. Three modes:

| project | persona | cwd | tools |
|---|---|---|---|
| `__FORGE__` (FORGE CHAT 🔥) | FORGEMASTER, `FORGE_CHAT_PREAMBLE` | the Forge folder | `Read,Glob,Grep,LS` |
| a real project (DUCK TALK from the DEV panel) | DUCK SMITH, `UPDATE_PREAMBLE` + last 3500 chars of `DUCK_NOTES.md` | the project | `Read,Glob,Grep,LS` |
| none (new-project talk) | `DUCK_TALK_PREAMBLE` | — | none |

A resumed conversation keeps the engine it started on (`talk_engine()`
enforces this server-side). Replies may end with `CHOICES: a | b | c` and/or
`TITLE: …`; `split_choices()`/`split_title()` strip these server-side so they
are never spoken or shown, and return them as data — the page shows numbered
buttons, and saying "two" equals clicking the second (`choiceResolve()`, exact
matches only). `done` = reply contains `THE DUCK IS DONE`. Sessions are stored
in `talks.json` (per project or `_global`, newest 40, with `title`, `folder`,
`pinned`, `model`), folders in `talk_folders.json`. `/api/talks` returns
`{talks, folders}`. The DEV panel opens on a **lobby** of past conversations
(PINNED → folders → TODAY / THIS WEEK / OLDER); the page's automatic hello is
filtered out by `is_canned_hello()` so rows are not all labelled the same.

**Talk window.** Mic is a keyboard: dictation fills the composer
(`micWrite`), nothing sends until you hit send. The dwarf/duck reads replies
aloud by default (`dccDwarfVoice`), with a STOP button; forge-bellows
"thinking" sound only if sounds are on. Files attach by paperclip, Ctrl+V or
drop and upload immediately to `<project>/duck_uploads/` (`/api/talk-upload`).
Unsent text is kept per project in localStorage (`dccTalkIn:<project>`).

**Making changes.**
- *Quick change* (DEV panel): `/api/duck-quick` → queue `duck_update` with
  `text` and no sid → `do_duck_update` writes `BRIEF_UPDATE.md` from the text,
  then runs Claude in the project with `--permission-mode acceptEdits
  --allowedTools <BUILD_TOOLS>` (45 min cap, streamed to the job log), then
  `add -A`, commit `DUCK TALK update: <Name>`, push. CI mode → record version
  only; else `settings.auto_deploy_after_build` decides whether `deploy_one`
  runs. `OPEN_QUESTIONS.md` is echoed if the build left one.
- *From a conversation* (END & BUILD): `/api/duck-build` → `duck_update` with
  the sid (phase 1 distils the talk into `DUCK_NOTES.md`, phase 2 writes the
  brief, then build as above) or, with no project, `duck_build`: Claude emits a
  JSON spec → `new_project.py --spec` → `BRIEF.md` → agentic build → push →
  `deploy_one`.
- *Save notes only*: `/api/talk-save` → `duck_save`.
- *AI commit message*: `/api/write-message` → `write_commit_message()` reads
  the real staged+unstaged diff (60k cap), runs Claude with no tools, returns
  subject + body. On request only; never auto-commits. Login notices are
  detected and reported as errors, not returned as messages.
- *Mascot art*: `/api/forge-pack`, `/api/fix-pack`, `/api/forge-avatars`,
  `/api/generate-duck` all use `run_claude()` to draw SVG.

**Engine picker** (`engineMount`): surfaces `global` (Settings → AI engine,
stored server-side in `settings.claude_model`), `talk`, `dev`, `art`
(localStorage `forge.eng.<surface>`; `""` = use global). Cost guidance:
watchers → Haiku, builds → Sonnet, Opus deliberately.

**FORGE VOICE** (header mic, `vcHandle`): console-wide commands — "voice
off", "<project> save / go live / ship / deploy / open app / test / talk",
"status", "approvals", "deploy all". The last-named project is remembered.
Yields entirely while the talk mic is live. `elocution()` never speaks URLs,
flags, or "CI"/"MR" (says "the pipeline", "merge request"); `speakName()`
spells short acronyms ("R 2 B I W").

**No seat except Claude-in-chat can send Slack or email.** `vip.json`
(trusted list: Ploy, Pradeep, Gaurav, Shravani Karra, Alex Vazquez; everyone
else needs Brendan's explicit OK) is policy for Claude's watchers — the server
never reads it.

---

## 12. Creating and deleting projects

**Wizard** (`#wizard`): FILL IN A FORM or TALK IT OUT, then the sections —
NAME IT / SAY WHAT IT IS / ANYTHING TO GO WITH IT? / **HOW TO START** /
HOW BIG / WHAT IT MAY READ. (Size and secrets were one section once; they
were two unrelated decisions under one heading, so they are now two.) Names
are checked live by `/api/name-check?name=` in three places — your disk, the
GitLab group, `databricks apps list` — each answering `free / taken /
unknown` (unknown never blocks). `validate_name()` also rejects
Windows-illegal characters, trailing dots and reserved device names
(`CON`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`). Three slug implementations must
agree: `server.py:slug_db()`, `new_project.py:slugify()`, page `slugDb()` —
lowercase, non-alnum → `-`, `app-` prefix if it starts with a digit, ≤30
chars cut on a word boundary. Secrets are a list of pick-or-create rows; the
scope dropdown is split YOURS / EVERYONE ELSE'S and values can only be added
on scopes you positively own (checked again server-side). Files arrive by
picker, drop or paste (pasted text becomes a named file). The draft survives
Escape via the server-side draft store (§15).

### HOW TO START — the six shells (`WSHELLS`, updated 2026-09-15)

One question, asked once, with the answers drawn rather than described. The
cards live in `WSHELLS` (`static/index.html`); the HTML three of them lay down
lives in `SHELLS` (`new_project.py`) and is injected into the scaffold's
`<main>` by `apply_shell()`.

| key | card | lays down HTML? | what happens after the scaffold |
|---|---|---|---|
| `bare` | BASE TEMPLATE | no | nothing — it lands on the board and waits |
| `dev` | TEMPLATE, THEN TALK | no | **opens the DEV conversation on it** |
| `kpi` | NUMBERS + TREND | yes | nothing |
| `table` | TABLE + FILTERS | yes | nothing |
| `split` | LIST + DETAIL | yes | nothing |
| `build` | BUILD IT FOR ME | no | `build_from_form()` — the only paid one |

`bare`, `dev` and `build` all lay down **no** shell: `SHELLS` has no entry for
any of them, so `apply_shell()` returns the scaffold untouched. The three
differ only in what happens afterwards, and two of those three differences are
purely client-side.

**`dev` — the shortcut** (Brendan, 2026-09-15: *"just a shortcut essentially
to go right into development and save clicking"*). The base template already
landed on the board and stopped, and the next thing he did every time was hit
DEV and start talking — a finish screen, a card hunt and two clicks between
making the thing and working on it, for a decision already made in the form.

The hand-off is **not** at the end of the job. The create poll watches the job
log for `== FIRST DEPLOY`, which is the moment the folder exists, git is
initialised and the push is done; at that point it closes the cinematic and
calls `wizDoneThen("build")` → `devOpen()`, while the 3–5 minute first deploy
carries on behind it. Typing during that deploy is safe rather than lucky:
`do_duck_update` takes the same per-project lock `deploy_one` holds, so the
message queues and the board reads "1 running · 1 waiting" until the deploy
lets go.

Three guards: it falls back to the finish screen if `== FIRST DEPLOY` never
appears (no deploy script, so there was no long tail to hand over during), it
does nothing if the forge was sent to the background (`FGW.bg` — that is him
saying he is busy elsewhere), and it does nothing outside the app
(`MODE !== "app"`, where the DEV panel does not exist).

Server-side, `"dev"` is inert: it is passed through in the spec and handled
entirely in the browser.

**Create job** (`/api/create-project` → queue `create_project`): attachments
land in `queue/uploads/<qid>/`, the spec in `queue/uploads/<qid>_spec.json`,
then `python new_project.py --spec <file>` (15 min cap) and, in the **same
job**, if `deploy_dbx_wsl.sh` exists, `deploy_one` for the first Databricks
deploy (3–5 min). The page shows the forging cinematic then a six-bar
**work board** (THE FOLDER / THE SCAFFOLD / THE HISTORY / THE REPO / READY /
THE LIVE APP) whose bars only land when the job log prints the matching
`== … ==` line. Escape backgrounds the view; it does not cancel.

**`new_project.py`** writes into `Projects/<NAME>/`: `.gitignore` (CAD,
push-rule extensions, junk), `Dockerfile`, `requirements.txt` (stdlib only),
`app.yaml` (`command: python server.py`, `GOOGLE_SECRET` from
`google-secret`), `.gitlab-ci.yml` (style from `settings.json`),
`DEPLOY_DBX.bat`, `deploy_dbx_wsl.sh` (SIZE + secrets JSON substituted),
`server.py` (stdlib dual-target template), `static/index.html` (family SPA
with the duck radio inlined), `DATABRICKS_APP_HOSTING.md` (copied from TORQUE
BYPASS), `CLAUDE_PROMPT.md`, `START_HERE/START_HERE.md`, and any uploads.
**If — and only if — the radio was ticked** (`if a.radio`), it calls
`register_apply()` to add the project to `duck-radio/apply.py` and write a
stub `duck-radio/docs/<slug>.json`; the sentinel regions are written empty
either way, so a project can gain the radio later by running `apply.py`.
(Corrected 2026-09-16: this file used to describe the registration as
unconditional, which is why the count in §16 looked short.)
Then `git init -b main`, a `.gitignore`-first
commit, a scaffold commit, `remote add origin
https://gitlab.com/rivian/dimensional-metrology/<slug>.git`, and **`git push
-u origin main` (push-to-create)** — no GitLab API call. It never calls
Claude or Databricks. Nothing imports it, so no sweep covers it — **run it
after editing it** (theme §31).

**Delete** (bin icon → `#delpop`): tick which of THIS PC / GITLAB /
DATABRICKS to remove (GitLab is unticked automatically if the token can't
delete), type the exact name, then a 5 s countdown that only starts once the
name matches. `do_delete_project`: **`delete_preflight` checks every selected
part first and touches nothing unless all pass**; then GitLab (`DELETE
/projects/<id>`, re-read to distinguish "gone" from GitLab's default delayed
deletion), then Databricks (`apps delete <slug>`), then the local folder
**last** (`rmtree` clearing read-only bits). Any failure stops with "the
folder on your machine has NOT been touched". Secrets are listed but never
deleted (scopes are shared). Afterwards `radio_unregister()` strips the
`apply.py` row and docs stub, and the project is removed from `deploys.json`
and `versions.json`.

---

## 13. Duck radio (shared state)

`duck-radio/` is the canonical source for two features baked into every
dashboard's single-file SPA: **FORGE RADIO** (the player; the folder and
sentinels keep the historical "duck" names on purpose) and **Dashboard Docs**
(the DATA SOURCE(S) / CONTACT hamburger entries, content in `docs/<id>.json`
per the strict format in `docs/SCHEMA.md`). Nothing loads at runtime.

`apply.py` re-inlines `duck.css`, `duck_art.js`+`duck_core.js`, `duck_ui.js`,
`docs/docs.css`, `docs/<id>.json`+`docs/docs.js` into five sentinel regions
(`DUCK-RADIO:CSS|CORE|UI`, `DOCS-CSS`, `DOCS-JS`) of each registered project.
It finds the projects root from its own path (`SRC.parents[1]`), handles
DIMENSIONAL METROLOGY AGENT via `EXTRA_ROOTS`, and has **14 of the 18
projects** registered (checked 2026-09-16). The four missing ones — EINES
TRENDS MIND THE GAP, GAGEPACK DASHBOARD, VIN BODY MAPPER, DIMENSIONAL DATA
DECK — are not an oversight: FORGE RADIO is opt-in, and `new_project.py` only
calls `register_apply()` `if a.radio`. A project can still gain the radio
later by running `apply.py`. `--dry` shows byte deltas without writing; `--only <substr>`;
`--name` overrides the radio name (else `settings.radio_name`, else `FORGE
RADIO`). It backs up to `static/.duck-backup/`, refuses to write if the file
changed underneath, and holds a project back if the shipped docs have content
the source JSON lacks (`--force` overrides). `post.py [substr]` is the live
post-apply verifier (node --check every inline script, required/stale tokens,
`DASH_DOC` byte-identical to the JSON). Tests: `test_shipped.sh`,
`test_docs_shipped.sh`, `node test_docs.js`. `check.py` is dead (hardcoded
old path, 2-tuple unpack).

Audio comes from a shared Google Drive folder proxied by each app's server;
`p_`-prefixed tracks are PIN-locked (PIN in `duck_core.js`; "a velvet rope,
not security"). As of 2026-09-06, twelve dashboards are out of date with the
shared source — a dry run shows real deltas; nothing has been applied.

---

## 14. Mascots, sound, theatre

**Packs.** Ducks are the built-in default; `packs.json` holds forged packs
(`id, name, critter, critters, sound, home, elder, sergeant, loader_svg,
logo_svg`) — the dwarves pack "The Forge Hall" exists. Look & feel → Mascot
picks or forges one (`/api/forge-pack`, local Claude invents names and art;
`fill_pack_art()` retries so a half-drawn pack is never saved). With a
non-duck pack active (`localStorage.dccPack`), `critterize()` and
`mascotizeDOM()` rewrite every duck/pond/quack/mallard/hatch word in text,
tooltips and voice — **except** inside `SCRIPT/STYLE/TEXTAREA/PRE/CODE` and
anything marked `data-raw` or `data-noun` (paths and product names must stay
copyable). Pack art is cached in `dccPackArt` and applied synchronously
before any fetch so the loader never flashes ducks. Avatars live in
`avatars.json` keyed `project` (ducks) or `pack::project`; missing avatars for
the active pack auto-forge on first render (`/api/forge-avatars`). Switching
back to ducks reloads the page.

**Sound.** WebAudio, fully synthesised, **off by default** (`dccSfx`), volume
and per-event toggles in Look & feel. Card theatre: GO LIVE / OPEN APP =
strike (white-hot flash, sparks, clang); TEST = quench (cold blue, steam) then
`testgo` sparks when the bench answers. Forging cinematic (`forgeShow`): three
hammer strikes, the name in hot metal cooling to green. Loader is an anvil +
hammer animation. Everything honours `prefers-reduced-motion`.

**Voice out.** `announce()` runs text through `critterize()` then
`elocution()`, chimes, and speaks with a preferred en-GB voice. Morning voice
briefing after 6 h away (`dccLastSeen`).

---

## 15. Health, drafts, settings

**PRE-FORGE CHECK** (`#flightpop`, `/api/preflight`): rows grouped by what
they unlock — WORKING (git remote reachable, default branch, this machine),
APPROVALS INBOX (GitLab token + expiry), SECRETS & TEST BENCH (Databricks CLI
login), CI DEPLOYS (Git credential, group CI variables, identity). Row states
`ok | setup | warn | bad | unknown | checking`; `setup` means "not configured
yet on this clone" and, like `unknown`, never counts against the verdict.
`preflight_verdict()` produces `NOT READY / CHECKING… / READY TO WORK / ALL
GOOD`. `?quick=1` paints local rows instantly before the network calls.
Failing rows carry a FIX (`/api/preflight-fix`: `dbxlogin`, `dbxgit`,
`civars`, `gltoken`, `instances`, `gitauth`) and a ▶ run button into THE ANVIL.
`healthSweep()` runs 4 s after boot and hourly and `healthTrail()` lights
hamburger → nav → header → button → row for anything new; `capGate()` warns
before GO LIVE (needs `ci`) or TEST (needs `secrets`) but always lets you
carry on.

**THE ANVIL** (`#termpop`, `/api/term`): WSL / PowerShell / cmd, cwd
restricted to THE FORGE or a project, streamed output, history, Ctrl+A scoped
to the output. Inherits your logins — that is the point.

**Drafts.** Server-side so a reboot or another browser still has them:
`drafts.json` via `draftBind/draftTouch/draftRestore/draftClear` (currently
wired only to the New Project wizard), `dev_drafts.json` via the DEV panel's
own `/api/dev-draft` path (text + attachments + engine). Rules: clear on
success not on submit; never a secret value; say it was restored; an empty
draft deletes itself. Commit-message box, ANVIL command line and Vault add-key
form are not yet wired.

**settings.json** (POST `/api/settings` whitelists each key):

| key | default | meaning |
|---|---|---|
| `deploy_via_ci` | false | push to main = deploy (currently `true`) |
| `ci_deploy_style` | — | `sync` or `git_source`; only `do_ci_style` writes it (currently `sync`) |
| `ci_identity` | `""` = UNKNOWN | observed identity CI deploys as (currently `brendans@rivian.com`) |
| `claude_model` | `""` = CLI default | global engine (currently `opus`) |
| `auto_deploy_after_build` | false | deploy after a duck build when CI is off |
| `console_window` | `tab` | `app` = Chrome/Edge `--app` window |
| `editor` | `""` | remembered "Open in" editor id |
| `radio_name` | — | ≤24 chars, baked into dashboards by `apply.py` |

**Layer ladder** (theme §3, no two overlays share a z-index): wizard 80 →
gitpop 810 → verpop 820 → avpop 840 → devpop 850 → attpop 856 → talkpop 860 →
hubpop 872 → aboutpop 874 → vaultpop/flightpop 880 → workspop 882 → msgpop 884
→ termpop 890 → scopenew 900 → polpop 905 → duckPop 930 → apprpop 940 →
scufpop 950 → sgtpop 960 → helppop 975 → firstpop 980 → rotpop 985 → cfmpop
990 → delpop 992 → chpop 993 → prpop 995 → goodnight 9990 → forgepop 9995 →
engine menu 9999 → **tooltip `#dcctip` 10000, always on top**. Every popup
gets the same X via `popxAll()`.

---

## 16. Data files (all in the Forge root)

| file | purpose |
|---|---|
| `settings.json` | §15 |
| `deploys.json` | `{project: epoch}` last deploy — drives the freshness pill |
| `versions.json` | deploy history + `live` sha per project — rollback targets |
| `avatars.json` | mascot casting, `project` or `pack::project` keys |
| `custom_ducks.json` | AI-drawn avatars `{id: {label, svg, kw}}` |
| `packs.json` | mascot packs |
| `talks.json`, `talk_folders.json` | conversations per project / `_global`; folders kept separate on purpose |
| `drafts.json`, `dev_drafts.json` | server-side drafts (gitignored) |
| `scope_owners.json`, `secret_policy.json`, `secret_usage.json`, `scope_created.json` | Vault: ownership sweep, per-key expiry rules, who-reads-what, scopes created here |
| `approvals.json` | write-only dump of open MRs for the `mallard-approvals` watcher (gitignored) |
| `outreach.json` | `{project: [{id, who, about, sent, status}]}` maintained by Claude's reply-watchers; `waiting` → card pill |
| `instances.json` | port-collision log (gitignored) |
| `vip.json` | outreach policy for Claude's watchers; not read by the server |
| `gitlab_token.txt` | GitLab token (gitignored) |
| `queue/heartbeat.txt` | epoch, every 2 s |

---

## 17. API reference (~77 routes)

**Status & git** — `GET /api/projects`, `/api/git-state?name=`,
`/api/git-log?name=`, `/api/commit?name=&h=`, `/api/versions?name=`,
`/api/prompt?name=`, `/api/editors`; `POST /api/open`.

**Save / ship / deploy** — `POST /api/save`, `/api/ship`, `/api/push`,
`/api/deploy`, `/api/deploy-all`, `/api/deploy-version`, `/api/resolve`,
`/api/line`, `/api/ci-style`, `/api/write-message`, `/api/migrate-main`,
`/api/finish-main`, `/api/forge-self`, `/api/forge-pull`; `GET
/api/forge-works`.

**GitLab** — `GET /api/approvals`, `/api/pipeline?name=`,
`/api/token-status`; `POST /api/approve`, `/api/mr-decline`.

**AI** — `GET /api/forge-status`, `/api/talks?name=`, `/api/ducks`,
`/api/duck`, `/api/packs`; `POST /api/converse`, `/api/duck-quick`,
`/api/duck-build`, `/api/talk-save`, `/api/talk-upload`, `/api/talk-meta`,
`/api/talk-folder`, `/api/setup-forge`, `/api/generate-duck`,
`/api/forge-pack`, `/api/fix-pack`, `/api/forge-avatars`, `/api/avatar`,
`/api/browse` (one directory, described — the folder picker is drawn in the
page; see FORGE_THEME.md §36).

**Onboarding** — `GET /api/welcome`; `POST /api/welcome` (`{seen}` and/or
`{declare:{cowork,limit}}`). See FORGE_THEME.md §35.

**Databricks** — `GET /api/dbx-scopes`, `/api/dbx-keys?scope=`,
`/api/dbx-auth`, `/api/dbx-sweep`, `/api/secret-alerts`,
`/api/secret-usage`; `POST /api/dbx-scope`, `/api/dbx-sweep`,
`/api/secret-policy`, `/api/test`.

**Projects lifecycle** — `GET /api/name-check?name=`,
`/api/delete-info?name=[&quick=1]`; `POST /api/create-project`,
`/api/delete-project`.

**Health / instance** — `GET /api/preflight[?quick][?force]`, `/api/ping`,
`/api/instances`; `POST /api/preflight-fix`, `/api/bye`, `/api/quit`.

**Jobs & terminal** — `GET /api/jobs` (newest 12), `/api/job/<id>`,
`/api/term-dirs`, `/api/term/<id>?from=`; `POST /api/job-stop`, `/api/term`.

**Drafts & settings** — `GET/POST /api/draft`, `/api/dev-draft`,
`/api/settings`; `GET /api/dev-file`; `POST /api/dev-file-del`.

**Media** — `GET /api/audio/index`, `/audio/<f>.mp3`, `/duck/<f>.mp3`.

---

## 18. The other docs, and what each is

| file | status |
|---|---|
| `FORGE_THEME.md` | **Normative.** 33 rules; every one exists because the same bug appeared twice. Read before building any surface. Top three: §30 light the next step rather than describing it, §17 never state a fact you only inferred, §3 no two overlays share a z-index. |
| `FORGE_STATE.md` | Running changelog / state-of-the-world, newest at top, with several superseded sections kept for the record. Good for *why*; this file is for *how*. |
| `PLAN_FORGE_FACTORY.md` | The FORGEWORKS run-through. The live copy is the `forge-factory-runthrough` Cowork artifact; when they disagree the artifact wins. |
| `PLAN_FORGE_RADIO.md` | Plan: give the radio its own configurable name and art. Step one (Radio name setting) built; not applied to any dashboard. |
| `PLAN_PLUGINS.md` | Proposed, not built. Plugins stay local; FORGEWORKS to host the shelf. |
| `PLAN_MISSION_CONTROL.md`, `PLAN_DUCK_TALK.md` | Early plans (2026-09-03); mostly landed. |
| `DEMO_RUNSHEET.md` | Stakeholder demo script. |
| `TEST_CHECKLIST.md`, `TEST_CHECKLIST_2.md` | Manual test rounds for builds `2026-09-04d` / `2026-09-05a`. History. |
| `README.md` | Pre-rebrand ("Command Center", console window). Stale. |
| `duck-radio/README.md`, `duck-radio/docs/SCHEMA.md` | Radio + Docs reference; README's "ten dashboards" is stale (14 registered). |

---

## 19. Drift and bugs found while writing this (2026-09-07)

Code-level:

1. **`do_forge_self` will crash on a failed push.** `flag_auth_failure(jid,
   out, path)` at ~line 4043 references `path`, which is not defined in that
   function → `NameError` instead of the auth diagnosis. Should be
   `str(root)`.
2. `do_save` logs "branch 'draft'" even when it pushed to a line of work, and
   does not run `preflight_merge` (a line save can be rejected non-fast-forward).
3. `radio_unregister()`'s log text still says the registry "lives in TORQUE
   BYPASS DASHBOARD"; the code correctly uses `<Forge>/duck-radio`.
4. `new_project.py`'s generated `CLAUDE_PROMPT.md` still points at `TORQUE
   BYPASS DASHBOARD/duck-radio/` (FORGEWORKS carries that stale line).
5. FORGEWORKS' shipped `deploy_dbx_wsl.sh` has lost its `\` line continuations
   and JSON quote escaping relative to the template — the secret-attach
   `--json` is not valid JSON as written. Check the template substitution.
6. `duck-radio/check.py` is dead (hardcoded `/sessions/laughing-friendly-carson`
   path; 2-tuple unpack of 3-tuple `PROJECTS`). `post.py` is the live checker.
7. The work board's code banner says "five bars"; `FORGE_STEPS` has six.
8. `job_log()` indexes `JOBS[jid]` unguarded.

Documentation drift (older docs vs. code):

- `index.html` is 16,634 lines / ~908 KB, not "10,070 lines / 548 KB".
- Hamburger is Look & feel / Settings / About / Close — there is no
  "Preferences" item (`prefsOpen()` is a shim to `hubOpen("look")`).
- `new_project.py` does **not** write `.gitattributes` (those were committed
  to the 13 repos by hand on 2026-09-05).
- `draftBind` is wired to the wizard only; the DEV panel uses its own path.
- `vip.json` is Claude-watcher policy, not something the server enforces.
- Root `.gitignore` doesn't exclude `*.md` scratch, so `FORGE_STATE.md`,
  `FORGE_THEME.md`, the PLAN_* files, **and the whole `duck-radio/` folder are
  currently untracked** in the-forge repo, alongside 13 modified tracked files.
  Commit and push the Forge after work sessions (queue action `forge_self`).

Open items carried from FORGE_STATE (still true in code):

- `__pycache__`/`*.pyc` tracked in 11 of the 13 project repos.
- Which token yields the CI identity `brendans@rivian.com` is unknown.
- TALK IT OUT closes the wizard without carrying the name/brief into the talk.
- Ownership sweep is not kicked when the wizard opens (scope grouping can be
  stale on a cold start).
- Twelve dashboards are behind the shared radio source; nothing applied.

---

## 20. Rules of thumb when changing it

- Bump `BUILD` and `PAGE_BUILD` together whenever the page gains an endpoint.
  Server change = restart; page change = refresh.
- Parse every `<script>` block (`node --check`), look for dead ids and
  missing handlers, and check z-index ties before claiming a UI change works.
- Never git-push or deploy from the Claude sandbox — no GitLab network, no
  Databricks auth, and it leaves `index.lock` files it can't remove. Use the
  queue. Read-only plumbing (`log`, `show`, `ls-files`, `status`) is fine.
- Fix a bug in one of the four commit-then-push paths → fix it in all four.
- Anything shared by all dashboards belongs in THE FORGE, not in one
  project's repo (theme §33).
- If a value identifies whoever ran the wizard, it does not belong in what
  the wizard produces.
- Never announce a fact you only inferred; `unknown` is an allowed answer.
- After editing `new_project.py` or any script the server shells out to,
  run it — nothing imports it.
