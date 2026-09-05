# HELIX V3 — Architecture

> The technical source of truth: how HELIX is structured and *why*. For the product vision, read
> [`BLUEPRINT.md`](BLUEPRINT.md); for the V3 redesign charter, read [`V3_DESIGN.md`](V3_DESIGN.md).

This is a ground-up rebuild of the prototype (which lives on the `main` branch). The prototype proved
the idea; it also collapsed almost the entire UI into one ~3,900-line file with business logic, I/O, and
Qt widgets interleaved. V2 kept the *ideas* and discarded the *structure*, growing from an app-builder
into a full voice-first assistant with the same discipline. V3 keeps the machine and redesigns the
surface: the presentation-only vocabulary (App · Protocol · Agent · Hologram · Vault), sight
(`view_screen`, the `view_camera` show-me window + visual memory), just-in-time connections, and the
nightly Evolve drafter.

**The V3 overhaul (Sept 2026) moved the face to the web.** The brain did not move: services, ports,
adapters, domain — all unchanged in role. What changed: the default shell is now a **React app served
over 127.0.0.1** by a FastAPI backend (`helix/api/`), shown in an Edge WebView2 window (pywebview) —
`helix qt` keeps the legacy PyQt6 shell whole during the transition. Voice runs Qt-free in the
backend process (the pure grammar was extracted to `services/voicegrammar.py`; both shells share it).
And the hologram engine is now **build123d** (the OCCT B-rep kernel): a hologram is `model.py`, the
studio has live parameter sliders on a warm kernel, and exports include STEP — the format Bambu
Studio slices natively.

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
    web/       React (Vite + react-three-fiber) — the face   (talks HTTP/WS to api/)
       │
    api/       the web shell's backend: FastAPI + ShellSession + WebVoice (depends on services)
    ui/        PyQt6 — the legacy shell                       (depends on services)
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

  api/                 # THE WEB SHELL (see §7b): server.py + shell.py + voice_loop.py. Never imports ui.
  cad/                 # the hologram compile worker (runner.py) — the ONLY importer of build123d/OCCT.

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
    scad.py            #   OpenSCAD as data: the helix.scad helper library (+ its model-facing cheat-sheet), and the
                       #     pure readers of a hologram's source — parse_params (the customizer block), parse_brief
                       #     (the design header), friendly_error (compiler stderr → one warm sentence + detail),
                       #     inspect_source (cheap lints before a compile is even attempted)

  ports/               # Protocols only — the seams.
    llm.py             #   ChatModel.chat(turns,*,system,tools)->Reply; blocks: Text/ToolUse/ToolResult/Image; ToolOutput
    coder.py           #   CoderAgent.run_task(repo_dir, prompt) -> CoderResult
    repo.py            #   VersionedRepo: init/branch/commit/merge(--no-ff)/worktree/restore/diff
    stores.py          #   SettingsStore, MemoryStore, ConversationStore
    speech.py          #   SpeechIn (STT), SpeechOut (TTS)
    embedder.py        #   Embedder — text→vector (vault RAG) and the speaker-embedding seam
    cad.py             #   CadEngine — compile a hologram's model.scad (compile_stl/export_3mf/render_png), install()
                       #     it just in time, install_hint(); every call answers a CadResult, never raises
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
    openscad_cli.py    #   CadEngine via the OpenSCAD command line: finds the binary (settings override, PATH, the
                       #     usual install dirs; openscad.com over .exe on Windows — the .exe swallows stdout), runs it
                       #     windowless + time-boxed with cwd at the model, installs it with winget on request
    tripo3d.py / blockade_skybox.py  # hosted reference-mesh / 360° skybox backends (optional, key-gated)
    watchdog.py        #   process/parent watchdog for auto-restart lifecycles
    system_clock.py · signal_bus.py · restart.py

  services/            # The use-cases. Where the product behaviour lives.
    conversation.py    #   ConversationService — the model↔tools loop; routes to the subscription brain or the API loop
    tools.py           #   ToolRegistry — maps model tool-calls to service methods (the model's "hands")
    prompts.py         #   the system + coder prompts, in one place
    forge.py · builds.py · build_queue.py · build_status.py · sandbox.py · cancel.py   # the maker + build lifecycle
    selfdev.py · selfdev_lane.py · evolve.py    # improve HELIX itself (gated) + the nightly Evolve drafter
    model_baker.py     #   the hologram baker: lints, compiles model.scad through the CadEngine, renders the preview,
                       #     asks the vision critic once, writes the exports + the self-contained technical viewer
    render_kit.py      #   the Three.js stage an ANIMATED hologram's hand-authored index.html runs on (embedded JS)
    agents.py · scheduler.py · workflows.py                                             # saved-goal automations + pipelines
    files.py           #   the user's disk: list/read always; write behind a toggle; find_images/view_image for vision
    images.py          #   attached/located images → model-ready vision blocks (Pillow: orient, downscale, base64);
                       #     capture_screen for view_screen (ImageGrab off the GUI thread, ephemeral like every image)
    camera.py          #   the view_camera hand-off: the tool's worker parks on a CameraRequest until the
                       #     ui/camera_view window settles it with ONE webcam frame (cancel-aware, time-boxed)
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
    mediasense.py                           # playback sense — WASAPI render meter (ctypes, no deps): is the
                                            #   machine itself audibly playing? feeds voice.py's playback gate
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

### 5a. Startup cost — what may not be imported before the first frame

"Nothing blocks the orb" has a launch-time counterpart: **no heavy dependency is imported at module
scope on the path to the first frame.** Import time is paid by every user on every launch, it happens
before anything is on screen, and it is invisible to the test suite — so it is enforced by a test
(`tests/test_startup_cost.py`) rather than by discipline. Importing `app.container` cost ~2.8s until two
stacks were deferred to their point of use:

| Stack | Cost | Deferred to | Needed when |
| --- | --- | --- | --- |
| `anthropic` | ~1.55s | `AnthropicChat._client_for_current_key` | the first API-rail call — never, on a subscription-only launch |
| `trimesh` + `networkx` + `scipy.spatial` | ~955ms | *gone* — holograms compile through the `CadEngine` (OpenSCAD, a separate program); the baker imports nothing heavy | never; the test keeps the stack forbidden so it cannot creep back |

That took the composition root from ~2.8s to ~0.18s, and `app.bootstrap` from ~2.27s to ~0.28s. Two
consequences worth remembering:

- **A lazily-imported package must be named explicitly in `build.py`.** PyInstaller's static scan is
  trusted for module-scope imports only; deferred ones are pulled in with `--collect-submodules`
  (`anthropic`, `claude_agent_sdk`) or `--collect-all` (`trimesh`). A missing entry does not fail the
  build — it fails at runtime, in the frozen app, on the first call that needs the package.
- **Deferring construction is sometimes the only way to defer an import.** Moving the `import` statement
  is useless if the object is still built during wiring, which is why `ModelBaker` sat behind a proxy
  (`_LazyBaker`) instead of merely having its import moved. The baker is light now, but the proxy stays:
  the Forge only reaches for it from a build worker, so the viewer's page templates are parsed on the
  first hologram rather than before the first frame. That proxy is wiring, so it lives in
  `app/container.py` per principle 6. The `OpenScadCli` adapter itself is constructed eagerly — stdlib
  only, nothing spawned until a hologram is compiled.

Diagnostics obey the same rule: the "which Claude rail is live" check probes whether a `claude.exe` will
actually launch, so it runs on a daemon thread rather than between launch and the first frame.

**What remains, and why.** With imports deferred, the largest pre-first-frame cost on a voice-enabled
launch is the STT pre-warm in `main.py` — measured ~2.5s (faster-whisper ~1.7s + the neural speaker model
~0.7s). That one is *not* removable in place: faster-whisper's native runtime crashes the process on
Windows if it is built after `QApplication` initializes, so it must precede every Qt import. It is
already skipped entirely when `voice_input_on` is off. Moving it off the startup path at all would mean
moving STT into a **separate process** (the constraint is same-process only) and paying for audio IPC —
a real option if launch latency ever matters more than that complexity, but not a local fix.

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

### 6a. Finding a `claude.exe`, and never blaming the token for it

`resolve_claude_cli()` treats "the file is on disk" as a *candidate*, not an answer. The Claude desktop
app ships as an MSIX package, so its bundled `claude.exe` lives in the package's `LocalCache`, where a
non-packaged process (HELIX) can see the file and Windows still refuses to launch it — its dependencies
resolve through the package graph. `CreateProcess` fails with a not-found error, the Agent SDK reports
`CLINotFoundError`, and the old resolver dead-ended on a path it had just "found" — while never trying a
perfectly good standalone `claude` on `PATH`, because the desktop copy was preferred outright.

So candidates are ordered (`HELIX_CLAUDE_CLI` override → desktop copies newest-first → `PATH`) and each
is **launch-validated** with a real `--version` spawn before it is returned. Launchability is cached per
path, so this costs no subprocess after the first resolution; `reset_cli_cache()` forgets it when a CLI
is installed or updated.

Two consequences the code depends on:

- **`allow_probe=False` for anything on the GUI thread.** Validating means spawning a ~278 MB exe.
  `SettingsView`'s "which brain is live" label is built before the first frame and refreshed on every
  Save, so it asks without probing and accepts an optimistic answer; the container warms the cache on a
  daemon thread at startup, so that optimism only applies in the first moment after launch. A wrong
  guess there costs one turn that falls back to the API rail — never a frozen window.
- **A CLI problem must never be reported as a credential problem.** Three unrelated failures (no token,
  no SDK, no launchable CLI) used to collapse into one message telling the user to check their token.
  `SubscriptionBrain.why_inactive()` names which one it actually is, and `PreferredChat` attaches that
  reason when the API rail also has no key — so a perfectly good token stops taking the blame.

**Vision** flows on both paths: images the user attaches/pastes/drags ride on the user turn; images
HELIX *locates* on disk (`find_images` / `view_image`) come back inside the tool result; and
`view_screen` captures the display itself (`images.capture_screen`, Pillow `ImageGrab` off the GUI
thread) into the same pipeline. `view_camera` turns the webcam on the *physical* world ("what is this
part I'm holding?"): the tool publishes `CameraRequested` and parks its worker on a
`services/camera.py` CameraRequest; the GUI thread opens `ui/camera_view.py` — a silent live-preview
window (mirrored preview, un-mirrored capture so markings read correctly) with a countdown and a
Capture button — and the one frame it hands back rides the same `encode_image_bytes` pipeline. The
wait is cancel-aware and time-boxed, and every window exit settles the request, so a turn can never
hang on an open camera. `services/images.py` normalizes every image (EXIF-orient, downscale to
~1568px, re-encode, base64) before it's sent; every capture is ephemeral — never persisted (camera
frames never touch disk at all). After an image turn answers, `MemoryService.after_image_turn`
distills durable *visual* facts into per-speaker long-term memory in the background, so what HELIX
saw stays answerable without the photo. Like `view_screen`, `view_camera` sits in the `BUILD_TOOLS`
fence: an unattended agent can never open the webcam.

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

### 7a. Holograms — a 3D model you design by talking

A **hologram** is a 3D model the user designs by voice — "a wall bracket for a 2-inch pipe with two
mounting holes", then "make it 100 wide and add a gusset" — drafted in OpenSCAD, shown as an
engineering-style drawing, exportable for printing. Four ideas carry the whole feature:

1. **The model is a program.** The coder writes `model.py` — Python on the **build123d** B-rep kernel,
   millimetres, a `# --- Parameters ---` block with `[min..max..step]` ranges and `[a, b]` choices, a
   `"""Design: …"""` docstring brief, geometry inside `build()` — importing only `helix_parts`
   (the library in `domain/cadpy.py`, seeded beside the source by the baker: enclosure generators plus
   a REAL hardware catalog — Arduino/Mega/Nano, Pi 4/Zero/Pico, ESP32 DevKitC modeled correctly as
   hole-less, relay modules — so "a case for an Arduino Uno" comes out fitting). "Make it wider" is an
   edit to a named parameter in source, not a regeneration; Python is the language LLMs write most
   accurately. Because a design file now EXECUTES, `cadpy.inspect_source` is also a safety gate: an
   import allowlist, no I/O builtins, no dunders, no top-level geometry (overrides land between exec
   and `build()`). The prompt that teaches all of this lives in `services/prompts.py`.
2. **HELIX compiles it, behind a port.** `ports/cad.py` (`CadEngine`) is the seam; today's adapter is
   `adapters/build123d_cad.py`, and tests use fakes. build123d drags in the OCCT kernel (~2s import,
   heavy resident memory), so the app process NEVER imports it: the adapter spawns the
   `helix/cad/runner.py` worker (`python -m helix.cad.runner` in dev; `HELIX.exe cadworker` frozen),
   and ONE run writes the whole artifact set — STL + **STEP** (Bambu Studio's native food) + 3MF (a
   real `Mesher` export, per-part) + the critic's preview (a numpy/Pillow software render: no GL,
   headless-safe) + a meta report (bbox, volume, PLA grams) — where OpenSCAD paid three full compiles.
   The adapter caches the run per source-sha, so the baker's compile→3mf→preview sequence costs one
   kernel session; a resident `--serve` worker (kernel imported once) serves the studio's slider
   recompiles in ~0.6s. When the kernel is missing, the model proposes a **just-in-time install**
   (`install_cad_engine` → pip, dev only; frozen builds ship it) that the user confirms — still in the
   `BUILD_TOOLS` fence, still pre-flighted by `build_3d_model` so a coder run is never spent on a
   design nothing can compile.
3. **Compile → preview → critique → repair, inside the one repair pass.** The Forge's pre-finalize check
   for a MODEL build *is* `ModelBaker.check()`: static lints (`inspect_source`) → compile to
   `assets/model.stl` → render `assets/preview.png` → one look from the vision critic (wired in the
   container on the API rail's web-fenced chat; it abstains with no key or on any failure). A compiler
   error comes back as one warm sentence plus the compiler's `file:line` words, fenced as data; a
   critique comes back as "Looking at the rendered preview (assets/preview.png): …" — both ride the
   Forge's existing one-pass `repair_prompt`, which tells the coder to *look* at the picture before
   touching `model.scad`. A missing engine reads as "not the coder's fault"; `bake()` then writes a
   friendly install page instead of failing the build.
4. **The viewer is a technical illustration, not a render.** `bake()` writes `index.html`: flat matcap
   shading with crease-edge lines on dark slate, a millimetre grid, axes, bounding-box dimensions, a
   section plane, wireframe and shading toggles, the parameter panel ("say: make <name> <value>"), and
   STL / 3MF / SCAD export links (plain `<a download>`; inside HELIX, `ui/app_viewer.py` accepts those
   downloads into the user's Downloads folder and says so in its header — QtWebEngine cancels a
   download nobody accepts silently, so without that slot the export row is dead in-app). It is
   **self-contained**: the vendored three.js r128 UMD build
   (`helix/ui/assets/three.min.js`, handed to the baker as a plain Path by the container) is copied
   beside it, the STL rides in a `window.HELIX_STL` sidecar, and every other datum is inlined — no CDN,
   and no `fetch()`/XHR, so the same page opens inside HELIX's `QWebEngineView` **and as a plain
   `file://` page in a Chrome tab** (Chrome blocks local fetches over `file://`; a `<script src>`
   sidecar is the one thing it allows). No bloom, no image-based lighting, no exposure boost.

4b. **The studio is where you touch it.** In the web shell a hologram opens in the STUDIO
(`web/src/pages/Studio.tsx`): a Z-up millimetre viewport (flat shading + crease edges, grid, orbit)
beside sliders parsed from the parameter block. A drag debounces, recompiles through the warm worker
with overrides (the design file untouched), and updates the mesh + print panel live; **Save to
design** rewrites the literals via `cadpy.set_params` (annotations survive byte-for-byte), re-bakes,
and git-commits. A Bambu P1S panel checks the 256³ bed and puts STEP first.

What did not change: a **360° environment** is still a Blockade Labs panorama in a skybox viewer; an
explicit **reference** ("show me what a real X looks like") is still a Tripo mesh in a small GLB viewer
— a likeness to look at, never the design; an **animated** hologram is still a hand-authored Three.js
page on `render_kit.py`. Retired engines migrate on their next edit: the primitive-JSON engine
(`materials.py`) and now the OpenSCAD engine (`model.scad`) both read as "redraw this as model.py" in
the same build's repair pass, while their old generated pages keep working untouched.

### 7b. The web shell — how the React face talks to the brain

`helix/api/` plays the role `helix/ui/` plays for Qt — it calls services, marshals events, owns no
business logic, and **never imports helix.ui** (no Qt loads in the web process):

- **`server.py`** — FastAPI on `127.0.0.1` only. `/` serves the built React app (`web/dist` in dev,
  `helix/webui` frozen); `/builds/…` serves build workspaces statically (apps and viewers iframe
  natively — the QtWebEngine download hack is gone); `/ws` is the ONE event stream; `/api/…` is
  ~40 thin routes. Every `/api` and `/ws` request must carry the per-install token (minted into
  settings, delivered in the launch URL) and a localhost Origin/Host — a random web page probing
  local ports can neither read nor act. The Settings routes enforce `LOCKED_SETTINGS` and treat
  credentials as write-only (presence reported, values never).
- **`shell.py` (`ShellSession`)** — the console's brain as a server: the submit gauntlet in the Qt
  order (sleep/wake as commands, cleanup-offer answers, stop phrases, the kept-message
  no-credential hold), the turn lifecycle with queued follow-ups, the stop contract, the 900 ms
  coalescing build announcer, cleanup-offer queue, delete confirmation as action buttons, the
  ANTICIPATE chip with its attempt-charged limiter, the QUIET sentinel, the situation/speaker
  blocks, the camera hand-off, the JIT connect panel, and the 15 s heartbeat (evolve, reminders as
  ONE spoken line, one scheduled run per tick). Everything the user sees rides `push()`; everything
  they do arrives as a method call from the routes. `tests/test_webshell.py` pins the contracts.
- **`voice_loop.py` (`WebVoice`)** — the VoiceController state machine ported off Qt onto
  sounddevice + threads: the same listen gate ("the mic is live only while HELIX is genuinely
  idle", camera-session exception included), the same playback gate over the machine's own audio
  (`adapters/mediasense.py` — moved from ui/, shim left behind), the same identity gate/enrollment
  flow, sleep-means-sleep, TTS streaming with generation-counter preemption. The pure grammar left
  ui/voice.py verbatim for **`services/voicegrammar.py`**; both shells re-import it, so the two can
  never drift.
- **`app/webboot.py`** — the web counterpart of bootstrap.py: container, recovery, uvicorn on a
  thread, the pywebview window (or `--browser`/`--headless`), and the same teardown duties.
  `helix` boots the web shell by default now; `helix qt` keeps the legacy shell.
- **`web/`** — the React face (Vite, react-three-fiber, zustand, Tailwind): the GLSL circuit-sphere
  orb (state-driven, audio-reactive, tap-to-talk), the console (plain-text bubbles ALWAYS — the
  untrusted-string rule survives the port — with SVG charts/tables for viz blocks and action
  buttons that round-trip the shell's registry), the hologram studio, menu, settings, vault,
  viewer, the getUserMedia camera modal (capture by button or the backend's voice shutter), and the
  masked JIT connect modal with the warn-once mis-paste guard. Fonts are bundled — offline-first.

---

### 7c. The Amazon faculty — eyes, hands, and the parts list

**Why it was rebuilt (Sept 2026).** The first cut let the model "find" products through web-search
snippets and open Amazon's remote add-to-cart link. Two things broke in practice: the model could not
see Amazon (ids drifted between turns, prices were guessed, the user screenshotted listings one by one
and asked "is this the right part?"), and the add-to-cart link now bounces every open through Amazon's
sign-in page with `openid.pape.max_auth_age=3600` — a password prompt whenever the browser's last
authentication is over an hour old, so "open the cart" rarely showed a cart.

**Eyes — `domain/amazon.py` + `adapters/amazon_web.py`.** HELIX reads amazon.com itself: plain HTTPS
GETs with a browser's headers (no cookies, no credentials), redirects followed only on Amazon hosts,
one read per ~1.2 s, ten-minute cache, Amazon's robot wall detected and reported as *unavailable*
(never parsed as "no results"). `parse_search` turns the results page into `Product` rows (title,
price, stars, rating count, Prime, "bought last month", picture, ASIN — organic before sponsored,
deduplicated); `parse_listing` turns a product page into a `Listing` (live price, availability, brand,
bullets, item model number and specs, option pickers, whether a plain Add-to-Cart button exists, the
quantity picker's ceiling). Tools: `search_amazon` (with an optional budget) and `lookup_amazon` — both
read-only and readable on autonomous runs; their text never names a fenced tool.

**Verification before staging — `services/shopping.py`.** `add_to_cart` checks every id against the
live listing (the search cache first, then a page read): an id Amazon has no page for is refused by
name, the price recorded is the one READ (a model-passed price is only a fallback), the listing's own
title and picture ride on the staged line, and a listing without a plain Add-to-Cart button is
flagged. The staged list is persisted to `data/helix_cart.json` and every change publishes
`CartChanged` — the face's cart panel (`web/src/components/CartDock.tsx`) redraws from that snapshot
(pictures, ± quantity, remove, the running estimate, *Hand to Amazon*, *Amazon's cart?*). Search
results publish `ProductsFound` → picture cards in the transcript with a *Stage* button
(`/api/cart/stage`), verified like any staging.

**Hands — `adapters/chrome_cart.py`.** On the user's go, `open_cart` drives HELIX's OWN Chrome window
(a dedicated profile at `data/amazon-chrome`, never the user's everyday browser profile) over the
DevTools protocol on localhost: for each staged line it opens the real product page, sets the quantity
picker (rounds when the picker caps below the count), presses Amazon's own Add-to-Cart button, then
opens Amazon's cart page and READS IT BACK — the tool result is what the cart holds, item by item;
misses stay staged with the reason (option needed, buying-options only, unavailable). Amazon keeps a
guest cart per profile, so it works before any sign-in; the first sign-in in that window merges the
cart into the account (an Amazon Business login there makes every HELIX cart a business order). The
driver presses exactly one button per product page and opens one cart URL — never checkout, Buy now,
1-Click, or a form. Chrome writes its port to `DevToolsActivePort`; a window left open is reused.
Without Chrome the old link handoff remains, with its sign-in catch stated and `resend_last` to send
the same link again. `check_amazon_cart` reads the real cart on demand (no more cart screenshots).

**The parts list — `services/parts.py` (`data/helix_parts.json`).** A project's BOM as a record:
rows with name, spec, planned quantity, verified ASIN and price, status (need / on hand / staged /
carted). `save_parts` upserts by name, `show_parts` recaps (readable, non-coaching), `stage_parts`
stages every needed row with an id at its planned quantity and names the unresolved ones for
`search_amazon` → `add_to_cart(project=…)`; a handoff flips linked rows to carted and writes a ledger
line (date, project, items, estimated spend) — the expense trail on HELIX's side.

**Fence.** `open_cart`, `add_to_cart`, `remove_from_cart`, `check_amazon_cart`, `stage_parts`,
`save_parts`, `remove_parts` are BUILD_TOOLS (human-driven only). The three stores and the Chrome
profile folder are on the guard skip list (`config.VOLATILE_STORE_NAMES`) — a browser writes into its
profile constantly, exactly the live churn a coder guard must ignore.

### 7d. The maker flow — parts, enclosure, fit, print

**Why (Sept 2026).** The IronEye session — a hat camera with vision, hearing and speech, for the
P1S — took four hours and never got a printable box: the model guessed component sizes into
`model.py` constants, re-derived the BOM from chat memory every turn, and the AR panel could show a
hologram over the camera but not at a known scale, so "will the XIAO fit in that pocket" had no
answer. The maker flow (spec and contracts: `READ_ME/MAKER_FLOW.md`) makes every number in the box
come from one of three places — the component library, a listing, or the camera's ruler — and
turns the four-hour improvisation into one conversation.

**The library — `domain/components.py` + `services/components.py`.** A 132-part catalog of maker
staples (boards, cameras, mics, amps, speakers, cells, chargers, switches, displays, sensors, motors,
drivers, LEDs, connectors) with L × W × H, mounting holes ONLY where a manufacturer drawing gave
them (otherwise `mount=pocket` and no holes — a wrong hole is worse than a pocket), ports and
apertures (lens, mic, grille, screen), an Amazon search phrase, a source kind and a confidence per
entry (community-measured ≤ 0.7 gets 0.5 mm more room). `find` resolves keys, names and spoken
aliases and never guesses; `lipo_from_code` reads a pouch cell's size code; `adhoc` carries a part
the user measured; `kit_for` ranks candidates per role for a need ("vision, hearing, speech").
`ComponentService.suggest` is the readable brief (roles, 2–3 sized candidates, honest notes, no
fenced tool named); `resolve_parts` maps a parts list to components. The parts list itself
(`services/parts.py`) gained the physical fields the box is designed from: `component` (the key),
`length/width/height`, `face` (which wall a lens/port/speaker must reach), `on_lid`.

**The generator — `domain/enclosure.py` + `helix_parts` additions in `domain/cadpy.py`.** Pure
Python: `plan_layout(spec)` packs the parts (rotation, clearance, a wire trench, standoffs only for
verified holes, rib-walled pockets, bays, rings; wall hints put a part against its wall and open
every port facing it; plate hints flip a part face-down and cut its lens bore, mic hole or hex
grille through the front; `on_lid` parts go on the lid's inner face, clear of the lip band and
towers) into a two-half, plate-face-down shell with lip ring + rebate, 2–4 screw towers on the
tower formula, debossed labels, and real geometry for the mount options (strap tabs, wall tabs, a
DIN clip, foot recesses). `model_source` emits ordinary `model.py` (parameter block with `wall`,
`clearance`, `corner_r`, `lid_style`, `label_deep`, one `<label>_extra` per pocket; a `# --- Layout
---` table) so the studio's sliders and the LLM edit path work on it unchanged; `layout_json` is
the §6 record the camera panel draws ghosts from; `validate` lists overlaps, out-of-cavity parts,
off-wall apertures, bed and thin walls in plain lines. The `BOARDS` block of `helix_parts` is now
rendered from the catalog at import (`render_boards`), so `board("xiao_esp32s3_sense")` works in
any hologram and the catalog is the single source of truth.

**AR measure and true scale — the camera panel (workstream C).** Measure mode (📐): two clicks
across a known length (credit card long edge 85.60 mm, short edge 53.98, the printable HELIX
marker 80.0, a US quarter, an AA cell, or a typed mm) calibrate `mm/px` at the tracker's base
frame, so the scale survives camera drift; drag = a length, shift-drag = a box, labelled rows,
a 10 mm grid; uncalibrated measuring is refused, never shown in pixels. Send posts one batch to
`POST /api/camera/{id}/measure` and the shell turns it into ONE line — `Measured: XIAO 21.1 × 17.6
mm; … (0.19 mm/px, card long edge)` — settling a parked `camera_measure` tool or becoming a turn.
A calibrated projection lands a hologram at 1:1 (a "↺ 1:1" snaps back after a drag), and a
hologram with a layout draws each component as a labelled ghost pocket on the shell's floor, its
apertures and screws marked, so the real parts are laid inside their ghosts on the desk.

**The brain — `services/maker.py` (`MakerService`), wired in the container.** Four tools:
`suggest_components` (readable: the library brief), `design_enclosure` (resolve the project's rows
— a row with neither a library match nor a size STOPS it with a precise line per row; hardware rows
and rows noted "no pocket" are listed, not pocketed — then `plan_layout` → `validate` →
`model_source` → the MODEL build `'<project> enclosure'` is created or updated: `model.py` +
`assets/layout.json` written, `ModelBaker.prepare`, one kernel compile through the shared
`CadEngine` (the compiler's own words on failure, and a rollback: a new build is deleted, an
update is `git reset --hard`), `bake`, `BuildService.finalize` (the version commit),
`BuildCreated`/`BuildIterated` for the menu and studio — and the plain fit report: outer size,
every pocket, the wall openings with their estimated heights flagged, screws, PLA grams and the
size measured off the compiled mesh, `validate`'s problems and the runner's print warnings),
`check_fit` (raise the panel if needed, project with the layout, say how to calibrate), and
`camera_measure` (park the ruler ask on the panel, wait up to 300 s cancel-aware, relay the line).
`print_hologram` now carries a **print sheet** (P1S settings, the parts with planned sizes and
measured grams, screws/inserts, the assembly order from the brief) and the studio shows it; the
studio's **Components & fit** panel lists each pocket and its cuts, shows problems in amber, and
its "Check fit on camera" button goes through `POST /api/holograms/{slug}/project` — the same shell
command path the tool takes. `GET /api/holograms/{slug}` carries `layout` and `print_sheet`. The
persona's "THE MAKER FLOW" paragraph runs the five steps in order and ends with the rule: never
hand-type a dimension you didn't read from the library, a listing, or the ruler.

**Fence.** `design_enclosure`, `check_fit`, `camera_measure` are BUILD_TOOLS (a build write; the
camera on the user's screen). `suggest_components` stays readable and its text names no fenced
tool. Nothing here prints or buys by itself, and the camera opens only on the user's request.

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
  A hologram workspace holds the design and what HELIX made of it: `model.scad` (THE design, the only
  file the coder edits), `helix.scad` (the helper library, refreshed every compile), `assets/model.stl`
  (the compiled mesh — viewer and printer both eat it), `assets/model.3mf` (slicer export, best effort),
  `assets/preview.png` (what the critic looked at), `assets/model.stl.js` (the STL as a `file://`-safe
  `<script>` sidecar), `assets/three.min.js` (the vendored viewer library) and `index.html` (the generated
  viewer, stamped with a sentinel so a hand-authored animated page is never overwritten).
- `scad_libraries/` — on `OPENSCADPATH` for every compile; drop BOSL2 (or any library) here and
  `include <…>` just works. Need not exist.
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
