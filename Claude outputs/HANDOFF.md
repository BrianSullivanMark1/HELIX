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
* THE VAULT: `/api/secrets` (Brendan's knowledge vaults keep `/api/vault/{slug}`); values go down
  gcloud's stdin and are never read back (no `access` on the port, by design); delete is gated like
  production (allowlist, typed name, Are you sure, delete-anyway when something reads it); audit
  rows in `data/deploy_audit.jsonl`; rotation policy in `data/vault_policy.json`. Not yet run
  against the real project from the PC - the first open on Brian's PC is the live test.
* Docs: `HELIX_MARK1_PLAN.md`, `docs/HELIX_RADIO.md`, `docs/CHANGES_2026-09-22.md`, `docs/KATE_SETUP.md`.

## Open decisions and the next work (Brian's words, 2026-09-22 pm)

* THE VOICE: Gemini-TTS via Google Cloud Text-to-Speech (decided; each person may pick a voice, quality never varies; task endings spoken if it costs next to nothing), styled in plain words, with
  think-out-loud lines ("ooh, let me take a look", "hmm, didn't expect this") while the face
  searches; lips from the audio's own amplitude; edge-tts stays as the free fallback. Paid
  quality is fine. One voice vs one per person/app: to decide. Narrate task endings out loud: to decide.
* Talk modes (conversation / build / ...) and a cheap cleanup pass (a small model tidies a
  rambling ask before the big one) - propose modes, decide.
* Random lines landing in the Talk box ("Good, how are you doing? Hey Deb."): the hands-free ears
  transcribe room audio and HELIX's own voice; the web voice plays in the browser so the Qt ears'
  playback gate does not see it. Fix candidates: tell the backend when the page plays audio (mute
  the ears), push-to-talk by default, echo cancellation.
* A themed HELIX capture window (photo + video clip, themed previews) beside Brendan's panel - decided 2026-09-22 pm: build the new window, it scales better.
* Faces: the docked head and the stage carry the ORBITS (rings + motes, `Orbits` in Organism.tsx);
  the stage's net is `NeuralLayer breach` (a sheet the head breaks through and that heals). The Talk
  page keeps the cortex and the 3D shell. If Brian wants the cortex gone there too, it is one line.
* Performance: the governor is in (`web/src/lib/perf.ts`); Brian reports the menu's fps line from his PC after each round; if still slow, next suspects are WebView2 GPU acceleration and the face shader's per-pixel cost (a cheaper face shader at lean/minimal).
* ECHO QA/PROD for real (the create lane runs the plan; Firebase CLI + project id in Settings).
* The merge + conflict editor (accept/reject hunks) and HELIX's own modern terminal (leaving PyCharm).
* Slack read-only first; Google calendar/mail; scheduled listeners.
