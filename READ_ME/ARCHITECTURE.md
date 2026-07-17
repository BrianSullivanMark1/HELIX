# HELIX V3 — Architecture

> The technical source of truth: how HELIX is structured and *why*. For the product vision, read
> [`BLUEPRINT.md`](BLUEPRINT.md); for the V3 redesign charter, read [`V3_DESIGN.md`](V3_DESIGN.md).

This is a ground-up rebuild of the prototype (which lives on the `main` branch). The prototype proved
the idea; it also collapsed almost the entire UI into one ~3,900-line file with business logic, I/O, and
Qt widgets interleaved. V2 kept the *ideas* and discarded the *structure*, growing from an app-builder
into a full voice-first assistant with the same discipline. V3 keeps the machine and redesigns the
surface: the presentation-only vocabulary (App · Protocol · Agent · Hologram · Vault), sight
(`view_screen` + visual memory), just-in-time connections, and the nightly Evolve drafter.

---

## 1. Principles

1. **Hexagonal (ports & adapters).** The core (domain + services) knows nothing about Qt, Anthropic,
   git, or SQLite. It talks to **ports** (Protocols). **Adapters** implement those ports against the
   real world. Swap an adapter, the core doesn't move.
2. **The dependency rule.** Dependencies point inward only:
   `ui → services → ports ← adapters`, and everything may depend on `domain`. The domain depends on
   nothing. (The domain package imports no other helix layer.)
3. **Thin views, no business logic.** Views handle layout + signal wiring and call services on a
   `QtWorker` thread; classification and rules live in services, never in a widget. There is no separate
   ViewModel layer — the QtWorker bridge plays that role.
4. **One runtime, all Python.** A single PyQt6 process. This is deliberate: HELIX edits its *own* source
   to improve itself, and that story is only clean when everything it edits is Python it can re-run.
5. **Nothing blocks the orb.** Every Claude call, coder run, tool dispatch, or git operation happens on a
   worker thread; results return to the UI thread via signals. The orb must always breathe.
6. **Composition at the edge.** Wiring (which adapter implements which port) happens in exactly one
   place — `app/container.py`. Nothing else constructs an adapter.
7. **Reads open, writes confirmed, untrusted content fenced.** Faculties that only read (files, inbox,
   calendar, connected services, your own vault) are freely available; anything that spends, writes,
   or reaches out is confirmed. Everything a tool pulls in from the outside is wrapped as DATA the model
   must never treat as instructions.

---

## 2. The layers

```
    ui/        PyQt6 — views + a QtWorker thread bridge      (depends on services)
       │ calls
    services/  use-cases: the Forge + every assistant faculty (depends on ports + domain)
       │ depends on Protocols
    ports/     Protocols (the contracts)
       ▲ implemented by
    adapters/  Claude API · Claude subscription (Agent SDK) · Claude Code · git ·
               SQLite · JSON · whisper · edge-tts · embeddings · Gmail/iCal · …

    domain/    pure models + rules (Constitution). No deps. Everyone may use it.
    app/       the composition root: container + bootstrap + CLI + single-instance guard.
```

---

## 3. Package map

```
helix/
  config.py            # AppPaths — app root + data dir (dev vs PyInstaller-frozen; %LOCALAPPDATA% when frozen)
  logging_setup.py     # rotating file log + crash-guard excepthook

  domain/              # PURE. No Qt, no I/O, no other helix layer.
    models.py          #   App (a Build), AppKind, BuildKind (app/task/agent/model/knowledge), Version, Message, slugify
    vocabulary.py      #   the V3 words — kind_label/kind_title ("task"→"protocol", "model"→"hologram",
                       #     "knowledge"→"vault"), legacy synonyms, and speakable tool labels. Presentation
                       #     only: persisted kind strings never change. (tool_labels.py is a shim into it.)
    constitution.py    #   Commandments, PROTECTED_PREFIXES/FILES, SHELL_PREFIX, EDITABLE_PREFIXES, LOCKED_SETTINGS
    errors.py          #   Domain exceptions (ConfirmationRequired, ConstitutionViolation, BuildError, BuildCancelled, …)
    events.py          #   EventBus payloads (BuildCreated/Iterated/Deleted/Started/Progress/Finished, SelfChange…,
                       #     ConnectRequested — the model asked to connect a service just in time)
    knowledge.py       #   passage chunking for the vault RAG
    connections.py     #   known connectable services (Slack/GitHub/Alpaca/SAM.gov) + how their key is attached

  ports/               # Protocols only — the seams.
    llm.py             #   ChatModel.chat(turns,*,system,tools)->Reply; blocks: Text/ToolUse/ToolResult/Image; ToolOutput
    coder.py           #   CoderAgent.run_task(repo_dir, prompt) -> CoderResult
    repo.py            #   VersionedRepo: init/branch/commit/merge(--no-ff)/worktree/restore/diff
    stores.py          #   SettingsStore, MemoryStore, ConversationStore
    speech.py          #   SpeechIn (STT), SpeechOut (TTS)
    embedder.py        #   Embedder — text→vector (vault RAG) and the speaker-embedding seam
    clock.py           #   Clock (now()) — no scattered datetime.now()
    events.py          #   EventBus: publish/subscribe

  adapters/            # Concrete implementations of the ports.
    anthropic_chat.py  #   ChatModel via the Anthropic SDK (prompt caching, tool use, images in turns + tool results)
    agent_sdk_chat.py  #   SubscriptionBrain — turns on the user's Claude plan via the Agent SDK + claude.exe;
                       #     PreferredChat routes plain chat there, API chat is the fallback; tools bridge as MCP tools
    claude_code_cli.py #   CoderAgent via the Claude Code CLI (headless subprocess, streaming)
    api_coder.py       #   CoderAgent fallback via the Anthropic API (no CLI needed)
    coder_select.py    #   FallbackCoder — prefer the CLI, fall back to the API coder
    git_repo.py        #   VersionedRepo via the git CLI
    sqlite_store.py    #   MemoryStore + ConversationStore (one SQLite file)
    json_settings.py   #   SettingsStore (one JSON file)
    speech.py          #   WhisperSpeechIn (faster-whisper) + EdgeSpeechOut/OsSpeechOut (neural + OS voice)
    speaker_embed.py   #   neural speaker embeddings (WeSpeaker CAM++ onnx) for voice identity
    voyage_embed.py    #   text embeddings for the vault RAG (optional; keyword fallback otherwise)
    gmail_imap.py      #   read-only Gmail over IMAP
    ical_http.py       #   read-only calendar over a private iCal URL
    tripo3d.py / blockade_skybox.py  # hosted hologram-asset / skybox backends (optional, key-gated)
    watchdog.py        #   process/parent watchdog for auto-restart lifecycles
    system_clock.py · signal_bus.py · restart.py

  services/            # The use-cases. Where the product behaviour lives.
    conversation.py    #   ConversationService — the model↔tools loop; routes to the subscription brain or the API loop
    tools.py           #   ToolRegistry — maps model tool-calls to service methods (the model's "hands")
    prompts.py         #   the system + coder prompts, in one place
    forge.py · builds.py · build_queue.py · build_status.py · sandbox.py · cancel.py   # the maker + build lifecycle
    selfdev.py · selfdev_lane.py · evolve.py    # improve HELIX itself (gated) + the nightly Evolve drafter
    model_baker.py · materials.py · render_kit.py                                       # bake declarative holograms → glb + viewer
    agents.py · scheduler.py · workflows.py                                             # saved-goal automations + pipelines
    files.py           #   the user's disk: list/read always; write behind a toggle; find_images/view_image for vision
    images.py          #   attached/located images → model-ready vision blocks (Pillow: orient, downscale, base64);
                       #     capture_screen for view_screen (ImageGrab off the GUI thread, ephemeral like every image)
    knowledge.py       #   the Vault — searchable notes/documents + local RAG (search/create/remember)
    memory.py · profile.py · lessons.py         # long-term facts, who-you-are, learned prefs; memory.py also
                       #     distills durable VISUAL facts from image turns (after_image_turn, per speaker)
    location.py · recommend.py · suggestions.py                                         # place grounding, usage ledger, nudges
    connections.py     #   read-only call_api to connected services (host-allowlisted, redirect-refusing, secret-scrubbing)
                       #     + CONNECTABLE, the just-in-time connect registry behind the connect_service tool
    gmail.py · calendar.py · reminders.py                                               # inbox/calendar reads + spoken timers
    remote.py          #   optional token-gated loopback companion
    voiceid.py         #   who is speaking — the per-utterance identity decision + enrollment flow
    doc_extract.py · attachments.py                                                     # PDF/Word text + bundling file context

  ui/                  # PyQt6 — views + a QtWorker thread bridge (no separate ViewModel layer).
    theme.py · orb.py · shader_orb.py       # HUD palette + the animated Presence orb (QPainter, optional GPU shader)
    workers.py                              # QtWorker — run a callable on a QThread, emit result/error/progress
    voice.py                                # VoiceController — wake word, identity gate, streamed TTS, barge-in
    chat_input.py                           # the prompt box: Enter sends, and paste/drag an image to attach it
    console_view.py                         # the orb home + transcript + inline charts/tables + image previews + input
    launcher_view.py                        # the Menu — Apps / Protocols / Agents / Holograms / Vault cards + Suggested
    settings_view.py · connections_dialog.py · commands_view.py · knowledge_view.py · memory_view.py
                                            #   connections_dialog also hosts show_connect_panel — the masked
                                            #   just-in-time key panel a ConnectRequested event opens
    app_viewer.py                           # in-app web view for built HTML apps + holograms (optional WebEngine)
    build_status.py · main_window.py        # the status board + the shell (stacked pages, nav, orb background)

  app/                 # Composition root.
    container.py       #   Container — constructs every adapter + service. The ONLY wiring point.
    bootstrap.py       #   run_app() — build container, self-heal check, MainWindow, run Qt loop; aboutToQuit teardown
    remote_companion.py#   the optional loopback listener (off by default)
    single_instance.py #   a per-data-dir named mutex so a second launch raises the running window
    cli.py             #   argparse entry; bare `helix` opens the desktop app

main.py                # thin launcher: take the single-instance lock + pre-warm STT before Qt imports, then app.cli.main()
tests/                 # pytest — domain + services (and headless UI render) are fast to test; ~700 tests
```

---

## 4. Key contracts (ports)

Ports are `typing.Protocol`s so adapters need no inheritance and the domain stays import-clean.

- **`ChatModel.chat(turns, *, system=None, tools=None) -> Reply`** — one model turn. A turn's blocks are
  `Text`, `ToolUse`, `ToolResult` (which may carry `images` a located photo handed back), and `Image`
  (an attached picture the model sees). `Reply` carries assistant text and/or tool calls plus token/cost
  usage. Prompt caching, tool use, and vision live inside the adapter.
- **`CoderAgent.run_task(repo_dir, prompt, *, on_progress=None) -> CoderResult`** — write/modify files in
  `repo_dir` and report success + a summary. The Claude Code CLI is preferred; an API-only coder is the
  fallback so a fresh install with just a credential can still build.
- **`VersionedRepo`** — the git verbs the Forge needs (`init`, `branch`, `commit`, revertible `--no-ff`
  `merge`, `worktree`, `restore`, `diff`, `log`). No raw git anywhere else.
- **`SettingsStore` / `MemoryStore` / `ConversationStore`** — persistence seams. Settings is JSON; the
  two stores share one SQLite file (usage/cost, the version index, chat history).
- **`SpeechIn` / `SpeechOut`** and **`Embedder`** — all optional; the real adapters report
  `available()==False` (or fall back) when the model/library isn't present, so voice, neural TTS, speaker
  identity, and semantic vault search are purely additive.
- **`EventBus`** — decouples "a build finished / progressed" from "the UI refreshes." Services publish;
  the UI subscribes on the UI thread.

---

## 5. Threading model

The UI thread only ever touches widgets. `ui/workers.py` provides `QtWorker`, which runs a service call
on a `QThread` and emits `progress`, `finished(result)`, or `failed(error)`. A turn looks like:

```
ConsoleView ── submit ──▶ QtWorker ──▶ ConversationService.run_turn()
      ▲                                     │ (subscription brain OR Anthropic call, off-thread; tools dispatch off-thread)
      └────── finished(reply) / progress(line) ◀── signals
```

Builds run in a background **queue** (they no longer block the turn); the orb keeps talking while the
coder works, and completion is announced separately over the `EventBus`. Scheduled agents and workflows
run on the single shell heartbeat.

---

## 6. Two brains: subscription and API

`ConversationService.run_turn` runs the model↔tools loop. When a **Claude Code subscription token** is
connected, turns route through **`SubscriptionBrain`** (`adapters/agent_sdk_chat.py`), which drives the
Claude Agent SDK over the local `claude.exe` on the user's Pro/Max plan — the same pool as Claude
Desktop, not metered API billing. HELIX's tools ride in as **in-process MCP tools** that dispatch
straight back into `ToolRegistry`; a tool may return **images** (a located photo) as MCP image content
the model sees. If the token path is absent or fails mid-turn, it falls back to the **API loop**
(`AnthropicChat`), where the same tools, images, and tool-result images are encoded for the Messages
API. Persistence, tool digests, and behaviour are identical on both paths. `PreferredChat` sends plain
(no-tool) chat — the distillers — to whichever brain is active.

**Vision** flows on both paths: images the user attaches/pastes/drags ride on the user turn; images
HELIX *locates* on disk (`find_images` / `view_image`) come back inside the tool result; and
`view_screen` captures the display itself (`images.capture_screen`, Pillow `ImageGrab` off the GUI
thread) into the same pipeline. `services/images.py` normalizes every image (EXIF-orient, downscale to
~1568px, re-encode, base64) before it's sent; every capture is ephemeral — never persisted. After an
image turn answers, `MemoryService.after_image_turn` distills durable *visual* facts into per-speaker
long-term memory in the background, so what HELIX saw stays answerable without the photo.

---

## 7. The Forge — building (the core maker loop)

A creation (a **Build** internally) is one of five kinds — **app, protocol, agent, hologram, or
vault** (persisted kinds `app / task / agent / model / knowledge`; the new words are rendered through
`domain/vocabulary.py`) — and the user conjures every one by talking to the orb. `ToolRegistry` offers
the build tools (`build_app`, `build_task`, `build_3d_model`, `create_agent`, `create_knowledge`,
`create_workflow`, plus rename/open/run/delete — tool names are internal and never change). A delete or
a self-change is never performed from the model loop alone — it asks the UI for one real human
confirmation first.

1. The user describes what they want; the model **confirms the spend in plain language** first (the
   system prompt has it ask before it builds).
2. On "yes", the tool enqueues the build; `BuildService.create_workspace` makes `data/builds/<slug>/`,
   `git init`s it, writes the `.helixbuild.json` manifest (carrying its `BuildKind`), and commits a scaffold.
3. The chosen `CoderAgent` runs in the workspace; the Forge snapshots the rest of the tree and reverts any
   write that escaped, then `BuildService.finalize` detects the entry point and commits the result to the
   workspace repo. (Agents/vaults skip the coder — they're saved instantly and cost nothing to create.)
4. `EventBus` events refresh the Menu (rendered from `BuildService.categorized()`) and the status board.
   Opening a creation loads it in the in-app `AppViewer` (HTML app / hologram), opens the vault manager,
   or runs a protocol in its own console.

Iterating ("make the streak monthly"), renaming, and deleting are the same path on the existing
workspace — all reachable by conversation.

---

## 8. Self-modification & the Constitution

HELIX can improve its *own* code through the same `CoderAgent`, but every self-change funnels through
`SelfDevService`, which enforces `domain/constitution.py`:

- **The Commandments** — the laws a self-writing program may not rewrite.
- **`PROTECTED_PREFIXES` + `PROTECTED_FILES`** — the safety/approval machinery the coder may never edit
  (the Constitution, the approval gate, the build sandbox, the prompts, the composition root, the
  connections/files/subscription adapters, the startup-import surface).
- **`SHELL_PREFIX`** (`helix/ui/`) — the Forge's own shell (orb, navigation, Settings) cannot be removed
  by a typed/spoken command. Refused up front *and* at the gate.
- **`EDITABLE_PREFIXES`** — the only surface a self-change may touch (`.py` under `services/`/`adapters/`,
  minus the protected files): a fail-closed allowlist; anything else is refused.
- **`LOCKED_SETTINGS`** — e.g. `human_approval_required`, which cannot be toggled off by the model.

The gate: record pending → scan the diff against protected paths/shell → smoke-check (non-executing
byte-compile in an isolated worktree — importing is deliberately avoided so branch code never runs at
approve time) → revertible `--no-ff` merge → restart. A fingerprint over the Constitution pauses
autonomous self-editing if the laws are tampered with. Drafting runs in a background lane so the orb
isn't frozen. Built apps are sandboxed to their own workspace and told never to reach outside it.

**Evolve** (`services/evolve.py`) is a *client* of this same gate, never a bypass: nightly it mines
what the day produced (lessons from corrections, logged errors, failed builds, slow turns), picks the
one most worthwhile improvement, and drafts it through the identical `improve_helix` pipeline — branch,
smoke-check, constitution scan. It never applies anything itself; the draft waits for the user's
explicit approval like any other self-change.

---

## 9. Data model (all under the data dir, gitignored, never bundled)

The data dir is `./data/` in development and `%LOCALAPPDATA%/HELIX/data/` in a frozen install (migrated on
first launch of a new build). `build.py` preserves live data across a rebuild.

- `helix_settings.json` — `SettingsStore`. Credentials: `claude_code_oauth_token` (subscription) and/or
  `claude_api_key`; plus voice/device, wake word, autonomy, and feature toggles.
- `helix_secrets.json` — connected-service API keys (Slack/GitHub/Alpaca/SAM.gov, Gmail, iCal), kept
  private and never surfaced by the file tools.
- `helix.db` — SQLite: Claude usage/cost, the version index, chat history.
- `helix_agents.json` · `helix_workflows.json` · `helix_reminders.json` — scheduled automations + timers.
- `helix_memory.json` · `helix_profile*.json` · `helix_lessons.json` · `helix_locations.json` — long-term
  memory, distilled profile, learned preferences, saved places (keyed per recognized speaker).
- `helix_voices.json` — enrolled voice profiles (embeddings only, never audio).
- `builds/<slug>/` — one git repo per build + a `.helixbuild.json` manifest (name + originating request).
- `helix.log` — rotating log.

A fresh install creates the data dir empty. Packaging (`build.py` → PyInstaller `--onedir`) **never**
bundles it, so no credential or history can leak into a shipped build.

---

## 10. Security posture (highlights)

- **Autonomous agents are hobbled by design.** A scheduled/agent run gets **no** build/spend/self-mod/
  delete/write tools (`BUILD_TOOLS` is filtered out) and **no arbitrary web fetch** — it can read, think,
  search, and report, but not act. It processes untrusted content (Slack/GitHub/email/notes/images), so
  this is deliberate.
- **`call_api` is read-only and fenced.** GET-only, limited to connected services, refuses all redirects
  (so a token can't be exfiltrated), caps the body, and scrubs secrets from what the model sees.
- **Keys are captured just in time, outside the model.** The `connect_service` tool only *requests* a
  connection: it publishes a `ConnectRequested` event, the UI opens the masked `show_connect_panel`
  naming the service and why, and the pasted value lands directly in the secrets/settings store. The
  model never sees, speaks, or echoes a key's value — not even the one it asked for.
- **File access is sealed and canonicalized.** Reads seal HELIX's own data/program folders (except
  `data/builds`); writes are behind a Settings toggle and additionally can't touch HELIX itself. Every
  path is canonicalized (drops `\\?\` prefixes, trailing dots/spaces) before a zone check, which fails
  closed. `find_images` honors the same seals.
- **Untrusted content is fenced everywhere** — file/inbox/vault/API text and image contents are
  wrapped as DATA with a nonce, and the system prompt forbids treating any of it as instructions.
- **The remote companion is off by default** — token-gated, loopback-only, and limited to read/ask +
  run-agent through the same tool fence.

---

## 11. Known limitations & next

- **Coder containment** was hardened over several adversarial red-team rounds; self-modification is a
  fail-closed allowlist, runs with git hooks disabled and Bash denied, re-scans the branch at approval,
  uses a non-executing compile smoke-check, and verifies the coder wrote nothing outside its workspace. A
  documented low residual: a prompt-injected CLI *build* can still scribble into the few gitignored
  runtime files the scan skips — annoying, not a gate bypass.
- **Archive** (a full version-history + factory-reset UI) is planned; today the live rollback lifeline is
  `bootstrap._self_heal` (a bad self-change auto-reverts on next launch).
- **Windows-first.** Frozen-build self-verification and cross-platform (macOS/Linux) are later milestones;
  some voice/TTS plumbing is currently Windows-specific.
