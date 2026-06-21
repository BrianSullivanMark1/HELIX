# HELIX — Design & Architecture

> The technical source of truth for how HELIX works today. For the product vision, read
> [`BLUEPRINT.md`](BLUEPRINT.md).

---

## 1. What HELIX is

HELIX is a **local-first desktop app-builder**. You describe an app in plain language; HELIX uses the
Claude coding agent to write it into its own isolated, versioned project; the app then appears in your
menu, ready to run. HELIX can also improve its *own* code the same way, behind a human-approval gate.

It is a single PyQt6 process. The only external dependency for its core function is **Claude** — the
Anthropic API key for the conversation, and the Claude Code CLI for the coding agent.

---

## 2. Architecture at a glance

```
        Console (orb + conversation)        <- voice / type
                  |  intents
        ai/actions.py  (the tool router)     <- the "hands"
                  |
   +--------------+---------------+
   build an app          improve HELIX itself
   selfdev/builds         selfdev/coder+engine
        |                        |
   per-app workspace        a review branch on
   data/builds/<slug>/      HELIX's own repo, then
   (own git repo)           approval-gated merge
                  |
        Foundation: Claude . memory (SQLite) . settings . git
```

- **Console** (`interfaces/qt_app.py`) — the orb, the conversation (XpertTab), and the menu/tasks/archive shell.
- **Router** (`ai/actions.py`) — maps the model's tool calls to real engine functions.
- **The Forge** (`selfdev/`) — the coding agent + the safe branch/draft/approve/merge/version machinery.
- **Foundation** (`core/`) — config, settings, SQLite memory, conversation store, logging.

---

## 3. Module reference

### `main.py`
Entry point. Pre-warms the local speech-to-text model **before** PyQt6 is imported (a native-lib load
order requirement on Windows), then calls the CLI.

### `helix/core/` — foundation
- `config.py` — resolves the app root (repo in dev, the folder next to the .exe when frozen) and the
  `data/` dir (DB, settings, logs, build workspaces).
- `settings.py` — `AppSettings`, a small JSON store at `data/helix_settings.json`. All keys flow through it.
- `memory.py` — `SQLiteMemory` at `data/helix.db`: Claude usage/cost, the version Archive
  (`interface_versions`), and per-feature provenance. (Also retains some legacy tables.)
- `conversation.py` — `ConversationStore`: persistent chat history (its own tables in the same DB).
- `reliability.py` — crash-guard excepthook + rotating logger.
- `mailer.py` — neutral email/SMS primitives (used by the optional self-improvement email approval).

### `helix/ai/` — the model layer
- `claude.py` — direct Anthropic Messages-API client (stdlib urllib, no SDK): `complete()`, `chat()`
  with tool-use + prompt caching. Needs an Anthropic key. Drives the conversation.
- `research.py` — prompt builders, incl. `build_jarvis_chat_system` (the Console's system prompt).
- `actions.py` — the **tool router**: `build_app`, `list_builds`, the self-improvement tools
  (`improve_helix`, `remove_feature`, `audit_dead_code`), the approval tools (`approve_change`,
  `reject_change`, `list_pending_changes`, `fix_recent_crashes`), and `show_screen`. Plus
  `run_chat_turn` (the multi-turn model<->tools loop) and the spoken-confirmation gate.
- `speech.py` — optional neural TTS (edge-tts), OS-voice fallback.
- `transcribe.py` — optional local STT (faster-whisper). Voice degrades to text-only if absent.

### `helix/selfdev/` — the Forge (the engine that writes code)
- `builds.py` — **per-app workspaces**: create `data/builds/<slug>/` (own git repo), the
  `build_app_prompt`, the `build_app` orchestrator, list/delete, and a best-effort `entry_point` for the runner.
- `coder.py` — runs the **Claude Code CLI** (`claude.exe`, headless, streaming) to edit files on a
  branch. Resolves the CLI and Claude auth (subscription token or API key). `run_coding_task(prompt=...)`
  targets either a build workspace or HELIX's own repo.
- `engine.py` — the **approval gate** for HELIX self-changes: record pending -> constitution scan ->
  smoke-check (imports the app in an isolated worktree) -> `--no-ff` merge -> restart.
- `gitops.py` — git write primitives (init, branch, commit, revertible merge, worktree, restore, push).
- `constitution.py` — the **Twelve Commandments**, locked settings, protected paths, and an integrity
  fingerprint. The laws a self-writing system cannot rewrite.
- `versioning.py` — the **Archive**: git history -> a SQLite index, with restore-to-version, a pinned
  default, and a reset-to-root factory lifeline.
- `restart.py` — self-restart after a merge (supervised exit-42, or self-spawn).
- `registry.py` — `MENU_FEATURES`, the list of apps shown in the menu. **Ships empty.**
- `triggers.py` — optional crash->fix drafting (default OFF).
- `mailer.py` — optional email approval of self-changes (default OFF).

### `helix/tasks/` — `registry.py`: runnable "action" apps shown in Tasks (ships empty).

### `helix/agents/` — goal-driven automations
- `registry.py` — settings-backed agent store (goal + trigger + enabled) and `run_agent`, which drives
  the `ai/actions` tool-loop toward an agent's goal. Anything needing approval pauses instead of
  auto-running. v1 trigger is manual ("Run now"); scheduled triggers build on the same definitions.

### `helix/interfaces/`
- `cli.py` — argparse entry; bare `helix` opens the desktop app (`ui`).
- `qt_app.py` — the whole desktop UI: `run_qt_app`, `PresenceOrb`, `ConsoleView`, `PanelHost`,
  `Launcher`, `TasksView`, `ArchiveTab`, `BuildView`, `XpertTab` (the conversation), `HelixMainWindow`,
  and `apply_hud_style`.

---

## 4. The Console (`qt_app.py`) — UI map

A four-page `QStackedWidget`:

| Index | Screen | What it is |
|-------|--------|------------|
| 0 | `ConsoleView` | the orb home + the conversation (default) |
| 1 | `PanelHost` | one summoned panel at a time (Settings, Archive, or a built app) |
| 2 | `Launcher` | the menu — `New app`, `Settings`, and a card per built app |
| 3 | `TasksView` | the run list for action-type apps |

`HelixMainWindow` wires these together, registers a `BuildView` panel for each existing app, runs the
self-improvement background beats (restart-if-pending, optional crash-fix, optional email approval), and
owns the Claude-key field in Settings.

---

## 5. The core loop — building an app

1. The user describes an app. `run_chat_turn` lets the model call `build_app(name, request)`.
2. `build_app` requires confirmation (it spends Claude time). On "yes", `execute_confirmed` calls
   `builds.build_app`.
3. `builds.create_workspace` makes `data/builds/<slug>/`, `git init`s it, commits a scaffold.
4. `coder.run_coding_task(repo_dir=workspace, prompt=build_app_prompt(...))` runs the Claude Code CLI
   in the workspace; the app's files are written and committed on a branch, then merged to the
   workspace's `main` (a revertible `--no-ff` commit).
5. The app self-registers: `HelixMainWindow._on_build_created` adds a `BuildView` panel + a menu card and
   opens it. `BuildView` can run the app (HTML -> browser; Python -> a new console) or open its folder.

Improving HELIX itself follows the same coder path but targets HELIX's own repo and **must** pass the
approval gate in `engine.py` before anything merges.

---

## 6. Data model

- `data/helix_settings.json` — `AppSettings`. Key setting: `claude_api_key`. (Voice device/speed,
  optional autonomy toggles, etc.)
- `data/helix.db` — `SQLiteMemory`: Claude usage/cost, the version Archive, feature provenance; plus
  `ConversationStore`'s chat history.
- `data/builds/<slug>/` — one git repo per built app, with a `.helixbuild.json` manifest (name +
  originating request — the app's "blueprint").
- `data/helix.log` — rotating log.

`data/` is gitignored and is **never** bundled into a build. A fresh install creates it empty.

---

## 7. Guardrails (`selfdev/constitution.py`)

A self-writing program's only real safety is a law it cannot rewrite. The constitution declares the
Twelve Commandments, locked settings (e.g. `human_approval_required`), permanent menu keys
(Settings/Archive), and `PROTECTED_PATHS` (the safety machinery the coder may never edit). Every HELIX
self-change funnels through `engine.approve`, which scans the change against the protected paths and
refuses to merge a violation. A fingerprint tripwire pauses autonomous self-editing if the laws are
tampered with. Built apps run in their own workspace and are told never to reach outside it.

---

## 8. Blank out of the box & first run

A clean clone/build has no keys and no data. On first launch HELIX creates an empty `data/`, the menu is
blank (`New app` + `Settings`), and the Console shows a banner: *"Add your Claude API key to start
building apps."* Once the key is saved in Settings, the conversation is live and the user can build.

The coding agent uses the **Claude Code CLI** when it's installed (more capable), and otherwise falls
back to an **API-based coder** (`selfdev/api_coder.py`) that builds with only the Anthropic key — so a
fresh download builds apps with no CLI required. The CLI path stays the preferred one for power users.

---

## 9. Packaging

`build.py` drives PyInstaller (`--onedir`, windowed) -> `dist/HELIX/HELIX.exe`. It **never** bundles
`data/`, so no keys or history can leak into a shipped build. `--with-voice` bundles the optional
STT/TTS stack.

---

## 10. Known gaps / next

- **Coding capability.** The API-coder fallback (no CLI) writes single-file apps well; complex,
  multi-file apps are stronger via the Claude Code CLI. Both work; the CLI is just more capable.
- **Running generated apps.** HTML apps open in the browser anywhere. Python apps need Python on the
  machine — in a frozen build (no Python) the runner falls back to opening the folder. The builder is
  biased toward dependency-free HTML for this reason.
- **Verification of HELIX self-changes in a frozen build.** `engine.smoke_check` import-checks via the
  Python interpreter; in a frozen exe `sys.executable` is the app, not Python, so it's a dev-mode
  safeguard. (Builds aren't affected — this is only the HELIX-edits-HELIX path.)
- **`memory.py` slimming.** Retains legacy tables/methods from the old pillars; harmless but worth pruning
  to the Archive/usage/provenance essentials.
- **Cross-platform.** Today Windows-first (CLI path resolution, console flags). macOS/Linux later.
