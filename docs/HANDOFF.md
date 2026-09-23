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

* The sandbox does NOT survive a new conversation. Rebuild it first: on the PC, tar the tracked +
  untracked files (`git ls-files` + `--others --exclude-standard`, minus `Claude outputs/` and
  `IMAGES_ABOUT_FORGE/`) into `Claude outputs/_sandbox_sync.tar.gz`, stage it, unpack to
  `/home/claude/helix`, `git init` + one baseline commit (so every later diff is exact). Tests:
  `QT_QPA_PLATFORM=offscreen python -m pytest -q` - 2,792 tests, 21 fail on Linux only
  (`os.startfile`, Windows process reaping): take that list as the baseline and compare. Build:
  `cd web; npm ci; npm run build`. PowerShell 7 can be unpacked to `/opt/pwsh` to parse and
  SIMULATE dev.ps1 headless against a fake `gcloud` on PATH. A mock server for screenshots =
  the real `mount_fleet` / `mount_deploy` / `mount_jobs` over doubles + `web/dist` (Playwright).
* Delivery: files staged under `/mnt/user-data/outputs/<round>/`, written to the PC with the
  device bridge (force), verified by md5 on the PC. **After every delivery that touches an
  adapter: `python -c "import helix.adapters.<each changed module>"`** - the test doubles do not
  exercise the real spawn paths (2026-09-22: a missing import shipped green and hung a deploy). Never edit a PC file in place without a
  `git show HEAD:` copy (one such edit once truncated `container.py`).
* After each delivery Brian runs: kill the old backend
  (`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*HELIX*main.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`),
  `python -m pytest -q`, `cd web; npm run build; cd ..`, relaunch, then commits.

## Where things are (2026-09-20 round added; the earlier list is dated 09-22 by its author)

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
* THE CREATE LANE (2026-09-20, a stepper since 09-21 - `CREATE_STAGE` in Console.tsx): `services/deploy.py:create_run` + `judge` + `followups`,
  `domain/fleet.py:PLANNED / console_steps`, `adapters/console_scripts.py:deploy_argv(typed=, create=)`,
  `POST /api/deploy/create_run`, `GET /api/deploy/followups`; the page: Server/Site/Both, Run this
  plan, the ONLY YOU CAN DO THIS window (`FollowupsWindow` in `Console.tsx`). dev.ps1 (console
  checkout) gained `-Typed`, `-Create`, the env-aware ECHO block and a headless result line + exit
  code. ECHO's `frontend/firebase.json` is three Hosting targets. `adapters/firebase_auth.py` adds the
  new site to Firebase Auth's authorized domains (add-only); `console_scripts.find_console` finds
  dev.ps1 on the PC; `GET /api/deploy/ready` is the one READY? call. Full story: `docs/CHANGES_2026-09-20.md`.
* Docs: `HELIX_MARK1_PLAN.md`, `docs/HELIX_RADIO.md`, `docs/CHANGES_2026-09-22.md`,
  `docs/CHANGES_2026-09-20.md`, `docs/KATE_SETUP.md`.

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

1. **ECHO QA/PROD for real** - BUILT 2026-09-20, proven only in the sandbox (dev.ps1 simulated
   under PowerShell 7 with a fake gcloud; the page driven in Chromium). The PC is the proof:
   create ECHO QA from the gear, do the two console steps the window shows, open the site. Then
   one line in `domain/fleet.py` (the QA row gains its names; its `PLANNED` entry goes) so the
   board reads it. PROD is its own run (`create prod`).
2. **Version control from HELIX** - Save (commit + push a draft) and Save & GO LIVE, THE FORGE's
   two buttons; today HELIX reads git but never commits, pushes or merges. Then the merge itself,
   the conflict editor (accept/reject hunks) and HELIX's own terminal (decided: leaving PyCharm).
3. **The voice**: BUILT 2026-09-21 - `adapters/google_tts.py`, Settings > HELIX's voice (styles are
   where the accent lives; Hear it per voice). Per person = per PC's settings. LEFT: think-out-loud
   lines while the face searches; spoken task endings (a line costs a fraction of a cent - do it).
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

## ECHO QA/PROD - answered and built (2026-09-20)

Answers: sites `oo-echo-qa` / `oo-echo`, services `brms-echo-api-qa` / `brms-echo-api`; PROD reuses
dev's service account and sign-in domains; one environment per run (QA first); Claude writes the
dev.ps1 edit, Brian reviews the diff and commits. Decided for round 3 at the same sitting: the ears
take follow-ups without the name only from an enrolled voice; Build mode's tidy-up shows the tidy
ask and waits for Enter; the Talk modes and the Talk cortex are Claude's call.

Brian's standing rule from the first look (2026-09-20 evening): **Apple easy** - one line that says
ready or exactly what is in the way, plain words, HELIX does everything it can itself, every wait
shows a strand, a typed word is judged as you type. Anything that reads like a manual is wrong.

Still open after the first real run: whether `oo-echo` is free (Hosting names are global); whether
the hosting-only deploy account may create sites for ECHO (it does for WMS/MRP); console.html's
ECHO picker still offers dev only (Brian's console, his call); HELIX could learn to notice a
created-but-untabled cell by reading Cloud Run for the `PLANNED` names.

**The order after this:** 2 version control from HELIX (Save, Save & GO LIVE, merge, the conflict
editor, the terminal) - 3 the voice - 4 the ears - 5 Talk modes - 6 the capture window - 7 radio
playlists - 8 Slack / Google / listeners - 9 the dream toggle - 10 the new-app wizard.

## Monday: Kate on MRP dev (checklist)

1. Her PC: `docs/KATE_SETUP.md` - gcloud signed in as kate@mark1online.com, the console checkout
   with `dev.ps1`, a GitHub token (repo scope), MRP linked to her folder. Settings > The Board >
   The tools on this PC must be all green (the menu glows until it is).
2. Before she deploys, **one real dev deploy from HELIX on Brian's PC** (ECHO dev or MRP dev) -
   the Deploy window, watched in CURRENT TASKS - is the proof the wrapped `dev.ps1` path works
   headless; it has only run in the sandbox against a fake script. Her console checkout must be
   pulled to the 2026-09-20 dev.ps1 (HELIX says so in a sentence if it is older), and her ECHO
   checkout to the three-target `firebase.json`. Ship now offers Server / Site / Both.
3. She commits with her own tools (HELIX does not commit yet); HELIX deploys the linked folder.
4. Dev and QA only for Monday. She is on the prod allowlist, but prod is typed-gate + Are you sure.

## Loose ends (small, none blocking)

* Decided 2026-09-21: the orbits everywhere; the cortex (`Organism.tsx:Cortex`, exported) is one
  word away if it is ever wanted back. Every 3D canvas is `frameloop="demand"` + `FrameThrottle`.
* The vault's "who reads it" is a grep of the linked folders and the console checkout only; a
  reader HELIX is not linked to is invisible, and the delete gate says so in words.
* Chrome DevTools prints "Unable to load image data:image/svg+xml..." on the radio button - that is
  the React DevTools extension snapshotting the SVG, not HELIX.
* `helix/services/files.py` (Brendan's) warns about an invalid escape sequence under pytest -
  harmless, not ours.
* `test_camera.py`'s pre-existing failure from 2026-09-17 no longer shows; the full suite is green.
* Firebase CLI presence is read in Settings but has never been exercised by a deploy from HELIX.
* Performance: 2026-09-21 the CPU hog was the fleet read (20 gcloud spawns, repeated because a
  per-app read forgot the board) - now 2 spawns, merged, throttled. The badge shows the backend's
  own CPU (`PY n%`) and spawns/min: read those numbers before touching the face again. If the face
  is still the cost, the next suspects are WebView2 GPU acceleration and the face shader's per-pixel
  cost (a cheaper face shader at lean/minimal).
* Prod DB stays read-only for diagnosis; plant data is never written - no code path exists for it,
  keep it that way when the terminal arrives.
