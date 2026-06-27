# HELIX V2 — Architecture

> The technical source of truth: how the V2 rebuild is structured and *why*. For the product vision,
> read [`BLUEPRINT.md`](BLUEPRINT.md).

This is a ground-up rebuild of the prototype (which lives on the `main` branch). The prototype proved
the idea; it also collapsed almost the entire UI into one 3,900-line file with business logic, I/O, and
Qt widgets interleaved. V2 keeps the *ideas* and discards the *structure*.

---

## 1. Principles

1. **Hexagonal (ports & adapters).** The core (domain + services) knows nothing about Qt, Anthropic,
   git, or SQLite. It talks to **ports** (Protocols). **Adapters** implement those ports against the
   real world. Swap an adapter, the core doesn't move.
2. **The dependency rule.** Dependencies point inward only:
   `ui → services → ports ← adapters`, and everything may depend on `domain`. The domain depends on
   nothing. (Enforced by review and import discipline; the domain package imports no other helix layer.)
3. **Thin views, no business logic.** Views handle layout + signal wiring and call services on a
   `QtWorker` thread; classification and rules live in services (e.g. `BuildService.categorized()`),
   never in a widget. There is no separate ViewModel layer today — the QtWorker bridge plays that role.
4. **One runtime, all Python.** A single PyQt6 process. This is deliberate: HELIX edits its *own* source
   to improve itself, and that story is only clean when everything it edits is Python it can re-run.
5. **Nothing blocks the orb.** Every Claude call, coder run, or git operation happens on a worker
   thread; results return to the UI thread via signals. The orb must always breathe.
6. **Composition at the edge.** Wiring (which adapter implements which port) happens in exactly one
   place — `app/container.py`. Nothing else constructs an adapter.

---

## 2. The layers

```
            ┌──────────────────────────────────────────────┐
            │  ui/        PyQt6 — views + viewmodels         │  (depends on services)
            └───────────────┬──────────────────────────────┘
                            │ calls
            ┌───────────────▼──────────────────────────────┐
            │  services/  use-cases / orchestration          │  (depends on ports + domain)
            └───────────────┬──────────────────────────────┘
                            │ depends on Protocols
            ┌───────────────▼──────────────────────────────┐
            │  ports/     Protocols (the contracts)          │
            └───────────────▲──────────────────────────────┘
                            │ implemented by
            ┌───────────────┴──────────────────────────────┐
            │  adapters/  Anthropic · Claude Code · git ·     │
            │             SQLite · JSON · whisper · edge-tts  │
            └──────────────────────────────────────────────┘

            domain/   pure models + rules (Constitution). No deps. Everyone may use it.
            app/      the composition root: container + bootstrap + CLI.
```

---

## 3. Package map

```
helix/
  config.py            # AppPaths — resolves app root + data/ dir (dev vs PyInstaller-frozen)
  logging_setup.py     # rotating file log + crash-guard excepthook

  domain/              # PURE. No Qt, no I/O, no other helix layer.
    models.py          #   App (a Build), AppKind (run mechanism), BuildKind (app/task/agent/model),
                       #   Version, Message/Role, PendingChange, slugify
    constitution.py    #   The Commandments, PROTECTED_PREFIXES/PROTECTED_FILES, SHELL_PREFIX,
                       #   EDITABLE_PREFIXES, LOCKED_SETTINGS + validators + fingerprint
    errors.py          #   Domain exceptions (ConfirmationRequired, ConstitutionViolation, BuildError, ...)
    events.py          #   EventBus payloads (BuildCreated / BuildIterated / BuildDeleted)

  ports/               # Protocols only — the seams.
    llm.py             #   ChatModel: chat(turns, *, system, tools) -> Reply (tool-use + caching aware)
    coder.py           #   CoderAgent: run_task(repo_dir, prompt) -> CoderResult
    repo.py            #   VersionedRepo: init/branch/commit/merge(--no-ff)/worktree/restore/diff
    stores.py          #   SettingsStore, MemoryStore, ConversationStore
    speech.py          #   SpeechIn (STT), SpeechOut (TTS)
    clock.py           #   Clock (now()) — no scattered datetime.now()
    events.py          #   EventBus: publish/subscribe

  adapters/            # Concrete implementations of the ports.
    anthropic_chat.py  #   ChatModel via the Anthropic SDK (prompt caching + tool use)
    claude_code_cli.py #   CoderAgent via the Claude Code CLI (headless subprocess, streaming)
    api_coder.py       #   CoderAgent fallback via the Anthropic API (no CLI needed)
    coder_select.py    #   FallbackCoder — prefer the CLI, fall back to the API coder
    git_repo.py        #   VersionedRepo via the git CLI
    sqlite_store.py    #   MemoryStore + ConversationStore (one SQLite file)
    json_settings.py   #   SettingsStore (one JSON file)
    speech.py          #   WhisperSpeechIn (faster-whisper) + EdgeSpeechOut/OsSpeechOut  (optional)
    tripo3d.py         #   Tripo3D — hosted neural text/image→3D backend (optional, key-gated)
    system_clock.py    #   Clock via datetime
    signal_bus.py      #   EventBus via a tiny thread-safe pub/sub
    restart.py         #   Restarter — relaunch the app after an approved self-change

  services/            # The use-cases. This is where the product behaviour lives.
    conversation.py    #   ConversationService — the model↔tools loop (prose confirm; agent runs capped)
    forge.py           #   ForgeService — the core loop: describe → confirm → build → register
    builds.py          #   BuildService — workspace lifecycle (create/list/categorized/delete/rename)
    sandbox.py         #   shared containment guards (snapshot/scan/restore) used by forge + selfdev
    selfdev.py         #   SelfDevService — improve HELIX itself, behind the Constitution gate
    agents.py          #   AgentService — saved-goal automations (run autonomously; build tools denied)
    tasks.py           #   TaskService — run a headless 'task' build in its own console
    model_baker.py     #   ModelBaker — bake a declarative model.json into assets/model.glb + a viewer
    tools.py           #   ToolRegistry — maps model tool-calls to service methods (the "hands")
    prompts.py         #   the system + coder prompts, in one place
    cancel.py          #   CancelToken / BuildHandle — cooperative stop for a running build
    # Archive is planned (no UI yet); the live rollback lifeline is bootstrap._self_heal (git restore).

  ui/                  # PyQt6 — views + a QtWorker thread bridge (no separate ViewModel layer).
    theme.py           #   HUD palette (cyan/amber/gold, dark) + apply_theme()
    orb.py             #   PresenceOrb — animated QPainter presence (paint_orb is shared with the icon)
    workers.py         #   QtWorker — run a callable on a QThread, emit result/error/progress
    voice.py           #   VoiceController — wake word, push-to-talk, TTS (optional)
    console_view.py    #   the orb home + transcript + inline animated charts/tables + input
    launcher_view.py   #   the Menu — Apps / Models / Agents / Tasks cards
    settings_view.py   #   the one ⚙ (Claude key, voice, devices, model detail)
    app_viewer.py      #   in-app web view for built HTML apps + 3D models (optional WebEngine)
    main_window.py     #   the shell: stacked pages, nav, the orb background

  app/                 # Composition root.
    container.py       #   Container — constructs every adapter + service. The ONLY wiring point.
    bootstrap.py       #   run_app() — build container, self-heal check, create MainWindow, run Qt loop
    cli.py             #   argparse entry; bare `helix` opens the desktop app

main.py                # thin launcher: pre-warm STT before Qt imports, then app.cli.main()
tests/                 # pytest — domain + services (and headless UI render) are fast to test
```

---

## 4. Key contracts (ports)

Ports are `typing.Protocol`s so adapters need no inheritance and the domain stays import-clean.

- **`ChatModel.chat(turns, *, system=None, tools=None) -> Reply`** — one model turn. `Reply` carries
  assistant text and/or a list of tool calls, plus token/cost usage. Prompt caching and tool-use are
  handled inside the adapter; the service just sees turns and tool calls.
- **`CoderAgent.run_task(repo_dir, prompt, *, on_progress=None) -> CoderResult`** — write/modify files
  in `repo_dir` on a branch and report success + a summary. Two adapters: the Claude Code CLI (preferred)
  and an API-only fallback so a fresh install with just a key can still build.
- **`VersionedRepo`** — the git verbs the Forge needs: `init`, `branch`, `commit`, `merge` (revertible
  `--no-ff`), `worktree`, `restore`, `diff`, `log`. No raw git anywhere else.
- **`SettingsStore` / `MemoryStore` / `ConversationStore`** — persistence seams. Settings is a JSON
  file; the two stores share one SQLite file (usage/cost, the version index, chat history).
- **`SpeechIn` / `SpeechOut`** — optional; the real adapters report `available() == False` when STT/TTS
  isn't present, so voice is purely additive (no null implementations needed).
- **`EventBus`** — decouples "a build finished" from "the menu refreshes." Services publish; the UI
  subscribes on the UI thread.

---

## 5. Threading model

The UI thread only ever touches widgets. `ui/workers.py` provides `QtWorker`, which runs a service call
on a `QThread` and emits `progress`, `finished(result)`, or `failed(error)`. A Claude turn looks like:

```
ConsoleView ── submit ──▶ ConversationViewModel ── QtWorker ──▶ ConversationService.run_turn()
      ▲                                                                   │ (Anthropic call, off-thread)
      └──────────── finished(reply) / progress(token) ◀───────────── signals
```

Long coder runs stream `progress` so the orb can show "Building…" without freezing.

---

## 6. The core loop (building)

A **Build** is one of four kinds — **app, task, agent, or 3D model** — and the user conjures every one by
talking to the orb. `ConversationService.run_turn` runs the model↔tools loop and offers the build tools
(`build_app`, `build_task`, `build_3d_model`, `create_agent`, plus `delete_build`).

1. The user describes what they want in the Console. The model proposes and **confirms in plain language**
   first — the spend gate is conversational (the system prompt has the model ask before it builds).
2. On "yes", the tool dispatches to `ForgeService.build(name, request, kind=…)`. (Agents skip the coder
   entirely — `create_agent` just saves a goal; it costs nothing to create.)
3. `BuildService.create_workspace` makes `data/builds/<slug>/`, `git init`s it, writes the
   `.helixbuild.json` manifest (carrying its `BuildKind`), and commits a scaffold.
4. The chosen `CoderAgent` runs in the workspace; the Forge snapshots the rest of the tree and reverts any
   write that escaped, then `BuildService.finalize` detects the entry point and **commits the result
   directly to the workspace repo** — no branch/merge. (That revertible `--no-ff` flow is the
   *self-modification* path in §7, not the build path.)
5. `EventBus.publish(BuildCreated / BuildIterated)` → `MainWindow` refreshes the Menu, whose four tabs are
   rendered straight from `BuildService.categorized()`. Opening a build loads it in the in-app `AppViewer`
   (HTML app / 3D model) or opens its folder; a task runs in its own console.

Iterating ("make the streak monthly"), renaming, and deleting are the same path on the existing
workspace — all reachable by conversation.

---

## 7. Self-modification & the Constitution

HELIX can improve its *own* code through the same `CoderAgent`, but every self-change funnels through
`SelfDevService`, which enforces `domain/constitution.py`:

- **The Commandments** — the laws a self-writing program may not rewrite.
- **`PROTECTED_PREFIXES` + `PROTECTED_FILES`** — the safety/approval machinery the coder may never edit
  (the Constitution, the approval gate, the build sandbox + its shared `sandbox.py` guards, the prompts,
  the composition root, the startup-import surface).
- **`SHELL_PREFIX`** (`helix/ui/`) — the Forge's own shell (orb, the Apps/Models/Agents/Tasks nav,
  Settings) cannot be removed by a typed/spoken command. Refused up front *and* at the gate.
- **`EDITABLE_PREFIXES`** — the only surface a self-change may touch (`.py` under `services/`/`adapters/`,
  minus the protected files): a fail-closed allowlist; anything else is refused.
- **`LOCKED_SETTINGS`** — e.g. `human_approval_required`, which cannot be toggled off by the model.

The gate: record pending → scan the diff against protected paths/shell → smoke-check (import the app in
an isolated worktree) → revertible `--no-ff` merge → restart. A fingerprint over the Constitution trips
and pauses autonomous self-editing if the laws are tampered with. Built apps are sandboxed to their own
workspace and told never to reach outside it.

---

## 8. Data model (all under `data/`, gitignored, never bundled)

- `data/helix_settings.json` — `SettingsStore`. Key: `claude_api_key`. Plus voice/device + autonomy toggles.
- `data/helix.db` — SQLite: Claude usage/cost, the version index, per-feature provenance, chat history.
- `data/builds/<slug>/` — one git repo per built app + a `.helixbuild.json` manifest (name + the
  originating request — the app's blueprint).
- `data/helix.log` — rotating log.

A fresh install creates `data/` empty. Packaging (`build.py` → PyInstaller `--onedir`) **never** bundles
`data/`, so no key or history can leak into a shipped build.

---

## 9. What changed from the prototype

| Concern | Prototype | V2 |
|---|---|---|
| UI | one 3,916-line `qt_app.py` | `ui/` split into views + viewmodels (MVVM) |
| Boundaries | Qt + I/O + logic interleaved | hexagonal: domain / ports / adapters / services |
| Anthropic client | hand-rolled urllib | the maintained `anthropic` SDK behind a `ChatModel` port |
| Wiring | constructed ad hoc | one composition root (`app/container.py`) |
| Testability | hard (Qt-coupled) | domain + services are pure and unit-tested |
| Constitution | logic + data mixed in `selfdev/` | pure rules in `domain/`, enforced by `services/selfdev.py` |

Same product, same guardrails — a structure that can actually grow.

---

## 10. Known limitations & next

- **Coder containment (hardened over four adversarial red-team rounds — 14→6→5→2 escapes, all criticals
  closed after round 1).** Self-modification is a **fail-closed allowlist** (a change may only touch
  `services/`+`adapters/` `.py`; the shell and the whole safety/startup-import surface are immutable),
  runs with git **hooks disabled** and **Bash denied**, **re-scans the branch at approval**, uses a
  **non-executing** compile smoke-check, and **verifies the coder wrote nothing outside its workspace**
  (source, `data/`, `.git/hooks` are snapshot-checked and reverted). The API coder is additionally
  hard-sandboxed by `_safe_target`. Untrusted request text is fenced and marked as data in every prompt.
  **Documented residual (low):** a prompt-injected CLI *build* can still scribble into the few gitignored
  runtime files the scan skips to avoid false positives (the app's own `helix.log`, sqlite `-wal`
  sidecars) — annoying, not a gate bypass: it cannot alter HELIX's source, the Constitution, settings,
  the approval flow, or another app's committed state. A future hardening builds in an isolated staging
  dir outside `data/`.
- **The Constitution is enforced on the self-modification path (Phase 7).** `domain/constitution.py`
  holds the rules; `services/selfdev.py` (the approval gate) is where `check()` /
  `locked_setting_violation()` are invoked before any self-change merges.
- **Frozen-build self-verification** and **cross-platform** (macOS/Linux) are later milestones; today is
  Windows-first.
