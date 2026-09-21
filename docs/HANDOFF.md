# HELIX - hand-off for the next conversation

Paste this file (or point the new conversation at `docs/HANDOFF.md`) and it can pick up where
this one left off. Keep it short and current; the long story is in `HELIX_MARK1_PLAN.md` (§16
changelog) and `docs/CHANGES_<date>.md`.

## Who and where

* Brian Sullivan (Mark 1, Oats Overnight). Brother Brendan built HELIX; branch `mark1` off `main`
  is ours; `main` and `v3` stay untouched. Repo `BrianSullivanMark1/HELIX`; on Brian's PC at
  `C:\Users\bgsul\OneDrive\Desktop\HELIX`; the console with `dev.ps1` at
  `C:\Users\bgsul\OneDrive\Desktop\BRMS_MES_WEB_APP\BRMS_MES_WEB_VERSION`.
* Kate and Brendan will run it too (`docs/KATE_SETUP.md`). Prod allowlist: Brian, Brendan, Kate.

## The working agreement (verbatim, keep it)

"You write the code. I commit and deploy through my console. Every delivery ends with 'Your
end': file list, what to deploy, any SQL, how to verify. Nothing is claimed that wasn't run.
Built-but-not-run says so. Describe the objective concisely before starting. Decisions come to
me as questions, in plain language. Each phase updates HELIX_MARK1_PLAN.md." Concise numbered
instructions. Plain names in the UI, THE FORGE's shapes in HELIX's sci-fi/neural art.

## The rules that must survive

1 WMS/MRP deploys pass no `--set-env-vars`, `--vpc-connector`, `--service-account`. 2 MES env
vars have one source of truth: `backend/deploy.ps1`. 3 `AppCheck` flips one env row at a time.
4 Prod refuses local-run and record-clearing. 5 A prod Cloud Run service that doesn't exist is
refused, never created. 6 Secrets are read at container start (bounce the service). 7 ASCII in
anything a script prints and in all .ps1. Also: production is human-only (allowlist, typed gate,
audit row first); plant data is never written; `dev.ps1` is wrapped, not rewritten; the nightly
dream reaches the fleet only behind a toggle, dev, draft-only; HELIX never deletes repos or
files; repos are created private; Brendan's files are not rewritten - new components sit beside
them; new work is UI first, logic after.

## How code moves

* The sandbox keeps a copy: Python in `/home/claude/fleet` (patched copies of `server.py`,
  `container.py`, `webboot.py` under `fleet/patch/`), the face in `/home/claude/web`; tests with
  `python -m pytest -q` (317 green), build with `npm run build`, a mock server for screenshots.
* Delivery: files staged under `/mnt/user-data/outputs/<round>/`, written to the PC with the
  device bridge (force), verified by md5 on the PC. Never edit a PC file in place without a
  `git show HEAD:` copy (one such edit once truncated `container.py`).
* After each delivery Brian runs: kill the old backend
  (`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*HELIX*main.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`),
  `python -m pytest -q`, `cd web; npm run build; cd ..`, relaunch, then commits.

## Where things are (2026-09-22, late)

* Backend: `helix/services/{fleet,deploy,radio,jobs,vault}.py`, `helix/adapters/{gcloud_fleet,github_fleet,console_scripts,gcs_radio,gcp_secret_manager}.py`,
  `helix/api/{fleet_routes,deploy_routes,radio_routes,jobs_routes,joke_routes,vault_routes,say}.py`,
  `helix/domain/{fleet,project_links,secret_scan}.py`, `helix/ports/{fleet,radio,secrets}.py`; composition in `helix/app/container.py`.
* Face: `web/src/pages/{Console,Talk,Settings}.tsx`, `web/src/components/{Organism,Radio,Tasks,Strandbar,Tip,HelixMark,Wordmark,StatusFace,Backdrop,Boot,Sparks,Shutdown,Vault}.tsx`,
  stores `web/src/lib/{store,jobs,chime,api,perf}.ts`.
* THE VAULT: `/api/secrets` (Brendan's knowledge vaults keep `/api/vault/{slug}`); reached by REST
  with the person's gcloud token (one spawn, cached); values travel once in the request body and are
  never read back (no `access` on the port, by design); delete is gated like
  production (allowlist, typed name, Are you sure, delete-anyway when something reads it); audit
  rows in `data/deploy_audit.jsonl`; rotation policy in `data/vault_policy.json`. Not yet run
  against the real project from the PC - the first open on Brian's PC is the live test.
* Docs: `HELIX_MARK1_PLAN.md`, `docs/HELIX_RADIO.md`, `docs/CHANGES_2026-09-22.md`, `docs/KATE_SETUP.md`.

## Status board (what is real, what is built but unproven, what is not built)

**Proven on Brian's PC** (run, seen): the Console board (Cloud Run + health reads), Git window
reads (branches, graph, merge check, secrets scan), CURRENT TASKS end to end (register, dock, log,
toasts, chimes), the radio deck (shelves, cache settings, DJ, stage), the docked face and its
leap, tips, the governor, Power off, the boot curtain, 326 tests green, clean builds.

**Built, run only in the sandbox (the PC is the proof):** THE VAULT against the real project
(the REST rewrite went in after the first cut hung on "Opening the vault"); a real deploy from
HELIX's Deploy window (`dev.ps1 -Action be-deploy` wrapped) and Stop on a running deploy
(taskkill of the script tree); rollback (one `update-traffic`); the radio's live token path,
a streamed upload and a music video from the bucket; the Firebase CLI row in Settings; the
completion pulse in each backdrop scene; the orbits and the breach lattice under a real GPU;
real fps numbers (Brian was to send the badge's line on Console and Talk).

**Not built - in the order Brian wants them:**

1. **ECHO QA/PROD for real** - see the next section; the Monday-morning round.
2. **Version control from HELIX** - Save (commit + push a draft) and Save & GO LIVE, THE FORGE's
   two buttons; today HELIX reads git but never commits, pushes or merges. Then the merge itself,
   the conflict editor (accept/reject hunks) and HELIX's own terminal (decided: leaving PyCharm).
3. **The voice**: Gemini-TTS via Google Cloud Text-to-Speech (decided; each person may pick a
   voice, quality never varies; task endings spoken if it costs next to nothing); think-out-loud
   lines ("ooh, let me take a look", "hmm, didn't expect this") while the face searches; lips from
   the audio's own amplitude; edge-tts the free fallback. To decide: one voice per person or per app.
4. **The ears**: random lines landing in the Talk box ("Good, how are you doing? Hey Deb.") - the
   hands-free ears transcribe room audio and HELIX's own voice (the web voice plays in the browser,
   so the Qt ears' playback gate never sees it). Candidates: tell the backend when the page plays
   audio (mute the ears), push-to-talk by default, echo cancellation.
5. **Talk modes** (conversation / build / ...) and a cheap cleanup pass (a small model tidies a
   rambling ask before the big one) - propose the modes, Brian decides.
6. **The HELIX capture window** (photo + video clip, themed previews) beside Brendan's panel -
   decided 2026-09-22: a new window, it scales better. The Talk row's camera menu already opens
   the camera and hints at it.
7. **Radio playlists** on top of shelves (decided: shelves now, playlists later).
8. **Slack** read-only first; Google calendar / mail; scheduled listeners (the dashed section on
   the Console).
9. **The nightly dream reaching the fleet** - only behind a toggle, only `dev`, draft-only.
10. **New app wizard** (THE FORGE's six sections) and "Copy an existing app" - plan section 12.

## The next round: ECHO QA/PROD for real (design, and the questions to ask first)

What exists: the gear's "Create an environment" is gated (typed app name + Are you sure +
allowlisted identity), writes an audit row and returns THE PLAN (`services/deploy.py:create_plan`):
the Cloud Run service name (`brms-echo-api-qa`; prod drops the suffix, MES's pattern), the env-var
source, the Hosting site, and the fleet-table change. Nothing runs.

What makes it run (each a step in one CURRENT TASK, each line streamed, stop = declared):

1. **dev.ps1's ECHO block becomes environment-aware** (it is hard-wired to dev: `ApiSvc=
   'brms-echo-api-dev'`, `Envs=@('dev')`, `HostingArg='hosting'`, `EchoBackendEnv` with
   `ALLOWED_ORIGINS` on the dev site). Like MES: `ApiSvc` per env, `HostingArg="hosting:$Env"`,
   `Envs=@('dev','qa','prod')`, `$ProjectEnvs.echo` = all three, `ALLOWED_ORIGINS` swapped per env.
   This is Brian's console file (rule: wrapped, not rewritten) - so it is a scoped edit of one
   block, delivered as a diff for Brian to read and apply, never a rewrite by HELIX at runtime.
2. **The ECHO repo's `firebase.json`** gains hosting targets (`qa`, `prod`) and `.firebaserc` the
   sites - a code change in ECHO's repo, committed by a person.
3. **HELIX runs the plan** (`services/deploy.py:create_run`, new): `firebase hosting:sites:create
   <site> --project windy-celerity-392822`, `firebase target:apply hosting <env> <site>` (in the
   ECHO checkout), then the first backend ship through dev.ps1 (`-App echo -Env qa -Action
   be-deploy`), then the first frontend ship (`fe-deploy`). The Cloud Run service is CREATED by
   that first `gcloud run deploy` - this is the explicit create lane, so rule 5 (deploy never
   creates a prod service) still stands for the Deploy window. ECHO passes `--set-env-vars` and
   `--service-account echo-proxy@...` because dev.ps1's ECHO block does (rule 1 is WMS/MRP only).
4. **The fleet table** (`helix/domain/fleet.py`) gains `Service("ECHO", Env.QA, "brms-echo-api-qa",
   "<site>", ...)` and PROD - a code change HELIX proposes as a diff and a person commits; the
   board reads the new cells on the next refresh. Until committed, the create task's last line
   says so.
5. Tests: the create run as a job with a fake script runner; the four gates; the fleet-table diff.

Questions for Brian before building (plain, one at a time):

* The QA and PROD **site names** for ECHO. Dev is `manufacturing-execution-system-mes-dashboard-dev`
  (an MES-era name). Keep the pattern (`...-dashboard-qa`, `...-dashboard` for prod) or start clean
  (`oo-echo-qa`, `oo-echo`)? The plan today swaps `-dev` for `-qa`.
* Does ECHO prod get its own **service account** and **ALLOWED_AUTH_DOMAINS**, or the dev ones?
* Should the create run **stop after QA** the first time (read it together before PROD)?
* Who applies the dev.ps1 diff - Brian by hand (safest), or HELIX writes the block behind the
  same typed gate?

## Monday: Kate on MRP dev (checklist)

1. Her PC: `docs/KATE_SETUP.md` - gcloud signed in as kate@mark1online.com, the console checkout
   with `dev.ps1`, a GitHub token (repo scope), MRP linked to her folder. Settings > The Board >
   The tools on this PC must be all green (the menu glows until it is).
2. Before she deploys, **one real dev deploy from HELIX on Brian's PC** (ECHO dev or MRP dev) -
   the Deploy window, watched in CURRENT TASKS - is the proof the wrapped `dev.ps1` path works
   headless; it has only run in the sandbox against a fake script.
3. She commits with her own tools (HELIX does not commit yet); HELIX deploys the linked folder.
4. Dev and QA only for Monday. She is on the prod allowlist, but prod is typed-gate + Are you sure.

## Loose ends (small, none blocking)

* The Talk page still carries the cortex and the 3D shell; the docked head and the stage carry
  the orbits. If Brian wants the cortex gone on Talk too, it is one line in `Organism.tsx`.
* The vault's "who reads it" is a grep of the linked folders and the console checkout only; a
  reader HELIX is not linked to is invisible, and the delete gate says so in words.
* Chrome DevTools prints "Unable to load image data:image/svg+xml..." on the radio button - that is
  the React DevTools extension snapshotting the SVG, not HELIX.
* `helix/services/files.py` (Brendan's) warns about an invalid escape sequence under pytest -
  harmless, not ours.
* `test_camera.py`'s pre-existing failure from 2026-09-17 no longer shows; the full suite is green.
* Firebase CLI presence is read in Settings but has never been exercised by a deploy from HELIX.
* Performance: if a real PC is still slow after the governor, the next suspects are WebView2 GPU
  acceleration and the face shader's per-pixel cost (a cheaper face shader at lean/minimal).
* Prod DB stays read-only for diagnosis; plant data is never written - no code path exists for it,
  keep it that way when the terminal arrives.
