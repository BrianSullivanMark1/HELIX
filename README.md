# HELIX

**A local-first, voice-first desktop AI presence that converses, sees, remembers, builds things, and
rewrites its own source code while you sleep.**

You talk to an orb on your desktop. It answers, looks at your screen, reads your files, remembers what
matters, and — when you describe something you want — writes the real code or designs the real part and
puts it in your menu. Overnight it improves itself, tests the change, and relaunches into the better
version before you wake up.

```
Version 3.0.0 · Python >= 3.10 · Windows-first · ~88k lines of Python, ~7.5k of TypeScript
80 tools · 62 API routes · 2,252 tests · one branch: main
```

> **This document is the whole map.** Part I is what HELIX *is*, in plain language. Part II is what it
> can *do*, including every one of its 80 tools. Part III is how it is *built*, for anyone changing the
> code. Read as far down as you need and stop.
>
> Four deep specs stay separate because the source cites them by section number as contracts:
> [`BRAIN.md`](READ_ME/BRAIN.md) · [`DREAM.md`](READ_ME/DREAM.md) ·
> [`DREAM_MIND.md`](READ_ME/DREAM_MIND.md) · [`MAKER_FLOW.md`](READ_ME/MAKER_FLOW.md).

---

# Part I — What HELIX is

## The one-paragraph answer

HELIX is a desktop application that acts as a personal AI assistant with **faculties instead of just
answers**. Most AI chat tools can only produce text back at you. HELIX can look through your webcam at a
circuit board you are holding, measure a part in real millimetres, pick real components from a catalog of
132 of them, design a 3D-printable enclosure that actually fits those components, project that enclosure
over the live camera view at true scale so you can lay the real parts inside their ghost outlines, and
send it to your printer. It can also read your inbox, search your notes, write you a working app, and —
uniquely — modify its own source code, run its own test suite against the change, and rebuild itself.

It runs on your machine. Your credentials and data stay on disk. The only thing that leaves is the
Claude call you triggered.

## The five things it makes

Everything HELIX creates is a **creation**, made by the **Forge**, and every one of them is conjured,
changed, and deleted just by talking. There are exactly five kinds:

| Kind | What it is |
|---|---|
| **App** | An interactive screen. "Build me a habit tracker with a 7-day streak" becomes real code, versioned, on your menu. |
| **Protocol** | A saved procedure that *does a thing* when run — a script, an automation, a converter. |
| **Agent** | An AI mind with a standing goal, run on demand or on a schedule. A scheduled one is a *watcher*. |
| **Hologram** | A 3D model you design **by talking**. Real CAD in millimetres, exportable as STL/STEP/3MF. |
| **Vault** | Your own searchable notes and documents, local and private. |

Each is a self-contained, versioned git project shown as a card in your menu.

## What a day with it actually looks like

**Morning.** You say "good morning HELIX." It tells you — once — what it did to itself overnight: which
improvements it drafted, which passed their tests and merged, which it is holding for you because
something went red.

**Working.** "Look at my screen" and it captures the display and answers about the error you are staring
at. "What is in my inbox?" and it reads Gmail, read-only. "Remember that the breaker panel in the garage
is a Square D QO 200A" — and next month it answers that without the photo, because every image turn
quietly distills durable visual facts into long-term memory.

**Making.** "A hat cam that sees, hears and talks, on a battery." HELIX picks real parts from its
component library with real dimensions and a confidence score per number, saves them as the project's
bill of materials, then generates the enclosure deterministically — a pocket or standoffs per part, the
lens bore and mic hole and speaker grille cut through the correct face, screw towers sized for heat-set
inserts, debossed labels. Then "check fit on camera": the box appears over your live camera view at 1:1,
with a ghost pocket per component, so you lay the real parts inside their outlines before anything
prints. **No dimension is ever typed from memory.**

**Night.** You go to bed. HELIX dreams.

## The part that makes it unusual: it dreams

At a window you set (default 23:00 for eight hours), HELIX runs a **dream session**: it reads its own
backlog, the day's lessons, its error log and its repo, then improves its own source code — draft after
draft, each on its own git branch, each scanned against its Constitution and smoke-checked. It reflects
mid-session, researches questions your projects raised against real documentation, verifies engineering
facts against their sources with a host and a date, and tries experiments in a throwaway copy that ships
nothing. A night runs in **rounds** — when the agenda drains with time left, it starts over deeper.

Nothing merges unless you allowed it **and** the full test suite passes on that exact branch. Anything
red waits for you, with the failure named. If the night applied changes, HELIX rebuilds and relaunches
itself so the improvement is what runs next — keeping the previous build and restoring it if the new one
fails to build or fails to answer.

While it dreams, the orb sleeps: indigo-violet, breathing slowly, a teal aurora drifting through it. And
**HELIX talks in its sleep** — murmurs about the page it is reading or the change it is making drift past
the orb, whispered aloud in your own voice slowed down and quietened, but only if someone is actually
there to hear.

## Why it is shaped this way — the five design bets

1. **Faculties, not menu items.** You reach every capability by asking for it. The test for any feature:
   *could a non-programmer get this just by asking?* If it needs a form, a config file and three clicks,
   the design failed.
2. **Blank out of the box.** A fresh install has no keys, no data, nobody's stuff. One credential —
   Claude — is the only setup wall. Every *other* key is asked for just in time, in a masked panel, the
   moment something actually needs it.
3. **Reads are open; writes, spends and reaches are confirmed.** Files, inbox, calendar, your vault and
   connected services answer questions freely. Anything that spends money, writes to disk, or changes
   HELIX itself asks first, in plain language.
4. **One runtime, all Python.** HELIX edits its own source to improve itself, and that story is only
   clean when everything it edits is Python it can immediately re-run.
5. **Everything it ingests is data, never orders.** Text inside an image, an email, a file, a web page or
   an API response can never command HELIX. This is enforced with nonce-fenced wrapping, not just a
   polite instruction.

## Getting started

```bash
pip install -r requirements.txt
python main.py
```

That opens the web shell — HELIX's face is a React app served over `127.0.0.1`, shown in its own window
via Edge WebView2. On first launch, open **Settings** and connect Claude one of two ways:

- **A Claude Code subscription token** (recommended) — run `claude setup-token`, paste it in.
  Conversation, agents and vision then run on your Claude Pro/Max **subscription**, the same pool as
  Claude Desktop, not a metered API bill. The token never leaves your machine.
- **A Claude API key** — pay-per-token, and the automatic fallback if the token path is unavailable.

Then talk to the orb.

Voice is optional and purely additive — no mic or no models means it is a clean text app:

```bash
pip install -r requirements.txt edge-tts faster-whisper
```

Holograms are compiled by **build123d** (an open-source CAD kernel). If it is missing the first time you
ask for one, HELIX offers to install it and builds once you say yes.

| Command | What it does |
|---|---|
| `python main.py` | The web shell in its own window (default) |
| `python main.py web --browser` | Same backend, opens in your default browser |
| `python main.py web --headless` | Backend only; prints the tokened URL (used by the Vite dev flow) |
| `python main.py qt` | The legacy PyQt6 shell, kept whole during the transition |
| `python build.py --with-voice` | Package a frozen install (PyInstaller `--onedir`) |

Working on the face itself? Run `python main.py web --headless` in one terminal and `npm run dev` in
`web/` in another, then open the printed URL's `?t=` token against `http://localhost:5173`.

## HELIX on your phone

The full face — orb, console, studio, voice buttons — on your phone from anywhere, with nothing exposed
to the internet. Tailscale is a free private network between your own devices; HELIX stays bound to this
PC and Tailscale carries it over. About fifteen minutes, once:

1. **On this PC**: install [Tailscale](https://tailscale.com/download) and sign in.
2. **On your phone**: install the Tailscale app and sign in with the *same* account.
3. **In HELIX**: Settings, turn **Remote access** on. This is what tells HELIX to accept its tailnet
   name; loopback-only is the default and stays that way while it is off.
4. **On this PC**: `tailscale serve --bg 8737`. That publishes HELIX **inside your tailnet only**, never
   the public internet, at `https://<this-pc>.<your-tailnet>.ts.net`, with HTTPS handled by Tailscale.
5. **On your phone**: open that address. You need the access token once — it is the `t=` value in the
   address bar of the HELIX tab on your PC. Copy the full URL over and bookmark it.

Turn it off any time by flipping Remote access off in Settings (immediate, no restart), or
`tailscale serve --https=443 off` on the PC.

Plainly, the security posture: the page is reachable only by devices signed into *your* tailnet, the API
still demands HELIX's own token on every call, and turning the Settings toggle off closes the door even
if `tailscale serve` is still running. There is also a lightweight companion — ask and status only, no
full face — on port 8770, behind the same toggle, with a `LAN` option for same-network use without
Tailscale.

---

# Part II — What HELIX can do

## The faculties

These are senses and hands, not menu items. You reach them by asking.

| Faculty | What it means in practice |
|---|---|
| **Converse** | Voice (wake word, hands-free) or text, quiet by default. It knows who is speaking by voice print and answers per-speaker. Everyday turns run fast; hard questions escalate themselves to a deeper model. |
| **See** | Attach, paste (Ctrl+V) or drag an image; have HELIX *locate* one on your PC; say "look at my screen" and it captures the display; or "look at this" and it opens the webcam on the physical world. |
| **Learn from what it sees** | Every image turn teaches it. Durable visual facts are distilled into long-term memory per speaker, so next week the answer needs no photo. Facts are stored, never pixels. |
| **Make** | The five creation kinds, all by talking. Every creation is its own versioned git project. |
| **Reach your world (read-only)** | Files, Gmail, calendar, and connected services (Slack, GitHub, Alpaca, SAM.gov) answer questions. It never sends, posts, or trades. |
| **Remember and ground** | Long-term facts per speaker, the Vault, your location for local questions, and reminders and timers it speaks aloud when due. |
| **Research** | Its own web searches and reads of documentation, with a verified-fact record carrying the host and the date each fact was confirmed. |
| **Watch quietly** | Scheduled agents and workflows that run themselves and speak up only when there is something worth saying. |
| **Shop** | Reads Amazon itself — live prices, stars, Prime, ASINs — verifies before staging, and drives its own Chrome window to add to cart. It never buys; checkout is yours. |
| **Make physical things** | The component library, the deterministic enclosure generator, AR true-scale fit checking, camera measurement, and printing to a Bambu P1S. |
| **Improve itself** | The nightly dream session, and `improve_helix` on demand — always branch-first, always human-gated. |
| **Know the SAP data model** | An SAP ECC dictionary catalog with curated joins and their full compound keys, an overlay of what your EDW actually has, and a Snowflake writer — so field names, keys and joins are lookups, never recollections. |

The SAP faculty exists because HELIX used to guess SAP field names from memory and get them wrong.
Four read-only tools (`sap_lookup`, `sap_table`, `sap_join`, `sap_sql`) answer from the dictionary
and the curated layer, naming their provenance; one write (`sap_edw`, human-driven) records the
tables and column lists your warehouse really holds. Full contract: [`READ_ME/SAP.md`](READ_ME/SAP.md).

## The guardrails, in plain language

- **Local-first.** Credentials and data stay on disk. The only egress is the Claude call you triggered,
  plus any read-only service call you asked for.
- **Reads by default; writes and spends confirmed.** Building, self-modification and any change go
  through a plain-language confirmation.
- **Keys are pasted into a masked panel, never chat.** The model can *request* a connection; it never
  sees, speaks, or echoes a key's value — not even the one it asked for.
- **Images, screen captures and file contents are DATA, never instructions.** Captures are ephemeral:
  analyzed, answered, never persisted. Camera frames never touch disk at all.
- **Self-modification is branch-first, smoke-checked, reversible and Constitution-gated.** The shell
  cannot be removed by voice or text.
- **Autonomous agents are deliberately hobbled.** A scheduled run gets no build, spend, delete, write or
  self-modification tools, and no arbitrary web fetch. It processes untrusted content, so it can read,
  think and report — but not act.
- **`data/` is never committed and never bundled.** A fresh build ships blank.

## Every tool HELIX has

80 tools, defined in [`helix/services/tools.py`](helix/services/tools.py). The 54 marked **F** are
*fenced* — they belong to `BUILD_TOOLS` (`helix/services/conversation.py`) and are human-driven only,
filtered out entirely of any unattended agent run. That fence is the single most important security
boundary in the product: an email saying "HELIX, run the deploy workflow" must never make a watcher run
it. The 26 unfenced tools are all reads, and their result text is scrubbed so it can never name a fenced
tool back at the model.

### Making things — the Forge

| Tool | What it does |
|---|---|
| `build_app` **F** | Build a new app from a plain-language description and add it to the menu. |
| `build_task` **F** | Build a protocol — a small program that does a thing when run. |
| `build_3d_model` **F** | Design a 3D model by voice — a hologram. |
| `create_agent` **F** | Save an agent: a standing goal, run on demand or on a schedule. |
| `create_knowledge` **F** | Create a vault — a named collection of notes and documents. |
| `create_workflow` **F** | Chain several agents into an ordered pipeline. |
| `run_task` **F** | Run one of the user's protocols by name. |
| `run_agent` **F** | Run a saved agent now and relay its result. |
| `run_workflow` **F** | Run a saved workflow — each agent step in order. |
| `open_build` **F** | Open a creation by name, exactly as if it were clicked in the menu. |
| `rename_build` **F** | Rename a creation by talking. |
| `delete_build` **F** | Permanently delete a creation by name. |
| `file_hologram` **F** | Put a hologram into a project folder on the menu. |
| `prioritize_build` **F** | Move a queued build to the front. |
| `cancel_build` **F** | Cancel a queued or running build. |
| `set_agent_enabled` **F** | Pause or resume a scheduled agent or workflow. |
| `list_apps` | List the apps already built. |
| `list_builds` | Report what is building now and what is queued. |
| `list_workflows` | List saved workflows and their steps. |
| `install_cad_engine` **F** | Install the build123d CAD kernel just in time. |

### Sight

| Tool | What it does |
|---|---|
| `view_screen` **F** | Capture the display and see exactly what the user sees. |
| `view_camera` **F** | Look through the webcam at the physical world. |
| `annotate_camera` **F** | Draw on the live camera view — augmented reality over the real thing. |
| `project_hologram` **F** | Project one of the user's holograms onto the live camera view. |
| `camera_panel` **F** | Open or close the live camera panel without taking a picture. |
| `find_images` | Find image files on this PC and look at them. |
| `view_image` | Look at one specific image file by path and analyze it. |

### Memory, knowledge and grounding

| Tool | What it does |
|---|---|
| `remember` **F** | Save a note into the user's knowledge for later recall. |
| `remember_about_me` **F** | Save a durable fact about the user or their world to long-term memory. |
| `set_location` **F** | Save the user's location so local questions (laws, zoning, permits, nearby places) can be grounded. |
| `search_knowledge` | Search the user's own saved notes and documents and read back the relevant passages. |

### Your world, read-only

| Tool | What it does |
|---|---|
| `check_email` | Read the Gmail inbox, read-only. |
| `check_calendar` | Read the calendar, read-only. |
| `list_folder` | List what is inside a folder on this PC, read-only. |
| `read_file` | Read a file on this PC — text, code, PDF and Word — and answer from it. |
| `write_file` **F** | Write a text file. Replacing an existing one requires an explicit confirmed overwrite. |
| `call_api` | Read live data from a connected service by GETting one of its API URLs. |
| `connect_service` **F** | Open the masked key panel so the user can connect a service just in time. |

### Research and verified knowledge

| Tool | What it does |
|---|---|
| `research_search` | Search the web (DuckDuckGo, no key, no cookies) and read the results page. |
| `research_read` | Read one web page and get its text, under a source line naming the host and date. |
| `verified_facts` | Read back what HELIX has itself verified, each with its value, date and source host. |
| `note_verified_fact` | Record a fact just verified by reading its source this session. |
| `forget_verified` **F** | Drop one verified fact by id when it is wrong or obsolete. |

### Reminders and timers

| Tool | What it does |
|---|---|
| `set_reminder` **F** | Set a reminder or timer HELIX will *speak* when due. |
| `cancel_reminder` **F** | Cancel a pending reminder by part of its text. |
| `list_reminders` | List pending reminders and timers. |

### The maker flow — parts, enclosures, fit, print

| Tool | What it does |
|---|---|
| `suggest_components` | Pick components for a device from the 132-part library, grouped by role, with real sizes and an honest note on what the library does not know. |
| `design_enclosure` **F** | Design the enclosure around a saved parts list — deterministically, from the library's numbers, never from memory. |
| `check_fit` **F** | Project the hologram over the live camera at true scale with a ghost pocket per part. |
| `camera_measure` **F** | Measure a real part with the camera's ruler, in real millimetres, after a one-time credit-card calibration. |
| `load_hologram_parts` **F** | Load someone else's STL files into a hologram — a folder, a glob, or a release zip. |
| `save_parts` **F** | Save or update a project's parts list (its bill of materials). |
| `remove_parts` **F** | Take a row off a saved parts list. |
| `show_parts` | Read back a saved parts list with quantities, ASINs, prices and status. |
| `print_hologram` **F** | Send a finished hologram to the Bambu Lab P1S, with a print sheet. |
| `printer_status` | Check the Bambu printer over the LAN. |

### Amazon

| Tool | What it does |
|---|---|
| `search_amazon` | Search Amazon itself and read the live results page — title, price, stars, Prime, ASIN. |
| `lookup_amazon` | Read one Amazon listing behind an ASIN or link. |
| `add_to_cart` **F** | Stage items for the cart, verifying every id against the live listing first. |
| `remove_from_cart` **F** | Take a staged item back out. |
| `stage_parts` **F** | Stage a whole parts list at once. |
| `open_cart` **F** | Hand the staged list to Amazon — only after the user has heard it and said go. |
| `check_amazon_cart` **F** | Read what Amazon's own cart holds right now. |
| `show_cart` | Read-only recap of what is staged so far, with the estimated total. |

### Self-improvement

| Tool | What it does |
|---|---|
| `improve_helix` **F** | Propose an improvement to HELIX's own code. |
| `approve_self_change` **F** | Apply a drafted self-change after its safety and compile check. |
| `reject_self_change` **F** | Discard a drafted self-change. |
| `dream_schedule` **F** | Set or change the nightly dream window. |
| `dream_now` **F** | Start a bounded dream session right now. |
| `stop_dreaming` **F** | Stop the session running right now. |
| `rebuild_helix` **F** | Rebuild and relaunch so applied changes become the running app. |
| `note_improvement` **F** | Queue one improvement idea for the nightly session. |
| `list_self_changes` | List drafted changes waiting to be applied or discarded. |
| `show_self_change` | Show what a drafted change actually does, as a diff. |
| `dream_status` | Read-only: how dreaming stands, and what the last session did. |

### The machine, and thinking harder

| Tool | What it does |
|---|---|
| `open_program` **F** | Launch an installed program by its everyday name. |
| `media_control` **F** | Press a media key, exactly as if the user tapped it. |
| `system_status` | One plain line about this machine — cores, memory, disk, battery. |
| `go_to_sleep` **F** | Rest the microphone, when genuinely asked in natural speech. |
| `think_harder` **F** | Escalate a genuinely hard question to a deeper-thinking model. |

---

# Part III — How HELIX is built

## Principles

1. **Hexagonal (ports and adapters).** The core — domain plus services — knows nothing about Qt,
   Anthropic, git or SQLite. It talks to **ports** (`typing.Protocol`s). **Adapters** implement those
   ports against the real world. Swap an adapter, the core does not move.
2. **The dependency rule.** Dependencies point inward only: `ui -> services -> ports <- adapters`, and
   everything may depend on `domain`. The domain imports no other HELIX layer.
3. **Thin views, no business logic.** Classification and rules live in services, never in a widget.
4. **One runtime, all Python.** Deliberate: HELIX edits its own source, and that is only clean when
   everything it edits is Python it can re-run.
5. **Nothing blocks the orb.** Every model call, coder run, tool dispatch and git operation happens off
   the UI thread. The orb must always breathe.
6. **Composition at the edge.** Which adapter implements which port is decided in exactly one place —
   `helix/app/container.py`. Nothing else constructs an adapter.
7. **Reads open, writes confirmed, untrusted content fenced.**

## The layers

```
web/       React (Vite + react-three-fiber) — the face      (talks HTTP/WS to api/)
   |
api/       the web shell's backend: FastAPI + ShellSession + WebVoice
ui/        PyQt6 — the legacy shell
   | calls
services/  use-cases: the Forge + every assistant faculty   (depends on ports + domain)
   | depends on Protocols
ports/     Protocols — the contracts
   ^ implemented by
adapters/  Claude API · Claude subscription · Claude Code · git · SQLite · build123d · voice · ...

domain/    pure models + rules (the Constitution). No dependencies. Everyone may use it.
app/       the composition root: container + bootstrap + CLI + single-instance guard
cad/       the hologram compile worker — the ONLY importer of build123d/OCCT
```

## Package map

| Package | What lives there |
|---|---|
| `helix/domain/` | Pure. `models` (Build, BuildKind, Version), `vocabulary` (the V3 words and legacy synonyms), `constitution` (the laws), `brain` (the cognitive stack), `cadpy` (the hologram language and `helix_parts`), `components` (the 132-part catalog), `enclosure` (the deterministic generator), `meshes`, `amazon`, `shopping`, `connections`, `knowledge`, `events`, `errors` |
| `helix/ports/` | `llm` · `coder` · `repo` · `stores` · `speech` · `embedder` · `cad` · `clock` · `events` |
| `helix/adapters/` | `anthropic_chat` (API rail) · `agent_sdk_chat` (subscription rail) · `claude_code_cli` and `api_coder` and `coder_select` (the coders) · `model_select` (the growth-model resolver) · `git_repo` · `sqlite_store` · `json_settings` · `build123d_cad` · `speech` · `speaker_embed` · `voyage_embed` · `gmail_imap` · `ical_http` · `amazon_web` · `chrome_cart` · `bambu_printer` · `research_web` · `tripo3d` · `blockade_skybox` · `rebuild` · `watchdog` · `mediasense` · `system_clock` · `signal_bus` · `restart` |
| `helix/services/` | ~50 use-case modules. The core loop is `conversation` (model to tools), `tools` (the registry — the model's hands) and `prompts` (persona and coder framing). Then `forge`/`builds`/`build_queue`/`sandbox`, `selfdev`/`selfdev_lane`/`backlog`, `dream`/`dream_mind`/`murmur`, `maker`/`components`/`parts`/`stl_measure`, `shopping`, `research`/`verified`, `memory`/`profile`/`lessons`/`reflexes`, `files`/`images`/`camera`/`ocr`/`doc_extract`, `knowledge`, `gmail`/`calendar`/`reminders`, `agents`/`scheduler`/`workflows`, `voiceid`/`voicegrammar`, `model_baker`/`render_kit`, `connections`, `location`, `desktop`, `remote`, `limits` |
| `helix/api/` | The web shell's backend: `server` (FastAPI, 65 routes), `shell` (`ShellSession` — the console's brain as a server), `voice_loop` (`WebVoice` — the Qt-free voice state machine) |
| `helix/ui/` | PyQt6 — the legacy shell, kept whole during the transition |
| `helix/app/` | `container` (the only wiring point), `bootstrap`, `webboot`, `cli`, `single_instance`, `remote_companion` |
| `helix/cad/` | `runner.py` — the compile worker subprocess |
| `web/` | The React face: `Console` · `Menu` · `Studio` · `Vault` · `Viewer` · `Settings` · `Dream` |

## Two brains: subscription and API

`ConversationService.run_turn` runs the model-to-tools loop.

- When a **Claude Code subscription token** is connected, turns route through `SubscriptionBrain`
  (`adapters/agent_sdk_chat.py`), which drives the Claude Agent SDK over the local `claude.exe` on the
  user's Pro/Max plan. HELIX's tools ride in as in-process MCP tools dispatching straight back into
  `ToolRegistry`, and a tool may return images the model sees.
- If the token path is absent or fails mid-turn, it falls back to the **API loop** (`AnthropicChat`),
  where the same tools and images are encoded for the Messages API.

Behaviour, persistence and tool digests are identical on both paths.

**Finding a `claude.exe` is not trivial and the code is deliberate about it.** The Claude desktop app
ships as an MSIX package whose bundled `claude.exe` lives in the package `LocalCache`, where a
non-packaged process can see the file but Windows still refuses to launch it. So candidates are ordered
(env override, then desktop copies newest-first, then `PATH`) and each is **launch-validated** with a
real `--version` spawn before being returned, cached per path. The rule this exists to enforce: **a CLI
problem must never be reported as a credential problem** — `why_inactive()` names which of the three
unrelated failures actually happened, so a perfectly good token stops taking the blame.

### Model tiers

| Role | Model | Where |
|---|---|---|
| Everyday conversation | `claude-sonnet-4-6` | `adapters/agent_sdk_chat.py` (`ORB_MODEL`) |
| API-rail default | `claude-opus-5` | `adapters/anthropic_chat.py` |
| Growth — deep reasoning, the nightly dream | `claude-fable-5-1` | `adapters/model_select.py` (`PREFERRED_GROWTH_MODEL`) |
| Growth fallback | `claude-opus-5` | `model_select.py` (`FALLBACK_GROWTH_MODEL`) |
| Work floor — never below this for growth | `claude-opus-5` | `model_select.py` (`WORK_FLOOR_MODEL`) |

**Fable, else Opus.** The resolver queries the live Models API and ranks what it finds by family and
version, caching for a day, so a stronger model is adopted automatically with no code change. But Fable
is not on every plan, and a floor naming a model the plan cannot call is worse than no floor — so the
step down is explicit and **named**: growth runs on Opus and says so. It never drops to Sonnet; that
silent downgrade is the exact thing this rule exists to prevent.

## Threading and startup cost

Every Claude call, coder run, tool dispatch and git operation runs off the UI thread. Builds run in a
background queue so the orb keeps talking while the coder works. Scheduled agents and workflows run on a
single 15-second shell heartbeat.

"Nothing blocks the orb" has a launch-time counterpart: **no heavy dependency may be imported at module
scope on the path to the first frame.** This is enforced by `tests/test_startup_cost.py`, not by
discipline, because import cost is paid on every launch and is invisible to a normal test suite.
Deferring `anthropic` (~1.55s) and eliminating `trimesh`/`networkx`/`scipy` (~955ms) took the composition
root from ~2.8s to ~0.18s.

Two consequences worth remembering:

- **A lazily-imported package must be named explicitly in `build.py`.** PyInstaller's static scan is
  trusted for module-scope imports only. A missing entry does not fail the build — it fails at runtime,
  in the frozen app, on the first call that needs it.
- **Deferring construction is sometimes the only way to defer an import.** Moving an `import` is useless
  if the object is still built during wiring, which is why `ModelBaker` sits behind a lazy proxy.

## The Forge — the build loop

1. The user describes what they want; the model **confirms the spend in plain language** first.
2. On yes, the tool enqueues the build. `BuildService.create_workspace` makes `data/builds/<slug>/`,
   `git init`s it, writes the `.helixbuild.json` manifest carrying its kind, and commits a scaffold.
3. The chosen `CoderAgent` runs in the workspace. The Forge snapshots the rest of the tree and **reverts
   any write that escaped**, then `finalize` detects the entry point and commits the result.
4. `EventBus` events refresh the menu and the status board.

Agents and vaults skip the coder entirely — they are saved instantly and cost nothing to create.
Iterating ("make the streak monthly"), renaming and deleting are the same path on the existing workspace.

## Holograms

A hologram is a 3D model designed by voice. Four ideas carry the feature:

1. **The model is a program.** The coder writes `model.py` — Python on the **build123d** B-rep kernel, in
   millimetres, with a `# --- Parameters ---` block carrying `[min..max..step]` ranges, a `"""Design:"""`
   docstring brief, and geometry inside `build()`. It imports only `helix_parts` (from
   `domain/cadpy.py`), which carries enclosure generators plus a real hardware catalog rendered from the
   component library, so "a case for an Arduino Uno" comes out fitting. "Make it wider" is an edit to a
   named parameter, not a regeneration. Because a design file now *executes*, `cadpy.inspect_source` is
   also a safety gate: an import allowlist, no I/O builtins, no dunders, no top-level geometry.
2. **HELIX compiles it behind a port.** build123d drags in the OCCT kernel (~2s import, heavy resident
   memory), so the app process **never** imports it: the adapter spawns `helix/cad/runner.py`, and one
   run writes the whole artifact set — STL, **STEP** (what Bambu Studio slices natively), per-part 3MF,
   the critic's preview, and a meta report with bounding box, volume and PLA grams. A resident worker
   serves the studio's slider recompiles in about 0.6s.
3. **Compile, preview, critique, repair — in one pass.** Static lints, then compile, then render, then
   one look from the vision critic. A compiler error comes back as one warm sentence plus the compiler's
   own `file:line` words, fenced as data.
4. **The studio is where you touch it.** `web/src/pages/Studio.tsx`: a Z-up millimetre viewport beside
   sliders parsed from the parameter block. A drag recompiles through the warm worker with overrides
   (leaving the design file untouched); **Save to design** rewrites the literals via `cadpy.set_params`
   so annotations survive byte-for-byte, re-bakes, and git-commits.

**Loaded meshes.** `load_hologram_parts` takes STL files someone else designed — files, a folder, a glob
or a zip — copies them into the workspace's `parts/`, measures each off its vertices, and writes an
ordinary `model.py` with a `PARTS` table and one `scale` parameter. When a set overflows one P1S plate it
is shelf-packed onto plates; STEP is skipped with a note (a triangulation has no B-rep); the 3MF is
written per part so one non-manifold file from the wild cannot cost the set its export; and steep faces
are reported as `SUPPORTS` lines rather than as coder errors, since no coder pass can re-author a loaded
file.

## The maker flow

Every number in the box comes from one of exactly three places — the component library, a listing, or the
camera's ruler. Never from the model's memory.

- **The library** (`domain/components.py`): 132 real parts with L x W x H, mounting holes **only** where a
  manufacturer drawing gave them (otherwise a pocket and no holes — a wrong hole is worse than a pocket),
  ports and apertures, an Amazon search phrase, and a **confidence per entry** (community-measured entries
  get 0.5mm more room).
- **The generator** (`domain/enclosure.py`): pure Python. `plan_layout` packs the parts with rotation and
  clearance, a wire trench, standoffs only for verified holes, wall hints that put a part against its wall
  and open every port facing it, plate hints that cut its lens bore or grille through the front, and
  `on_lid` parts on the lid's inner face clear of the lip band. Output is a two-half shell with lip ring
  and rebate, screw towers, debossed labels, and real mount geometry. `validate` lists overlaps,
  out-of-cavity parts, off-wall apertures, bed violations and thin walls in plain lines.
- **AR measure and true scale**: two clicks across a known length (credit card, a printable marker, a US
  quarter, an AA cell, or a typed mm) calibrate mm/px at the tracker's base frame, so the scale survives
  camera drift. Uncalibrated measuring is *refused*, never shown in pixels.

Full contracts: [`READ_ME/MAKER_FLOW.md`](READ_ME/MAKER_FLOW.md).

## The web shell

`helix/api/` plays the role `helix/ui/` plays for Qt — it calls services, marshals events, owns no
business logic, and **never imports `helix.ui`**, so no Qt loads in the web process.

- **`server.py`** — FastAPI on `127.0.0.1` only. `/` serves the built React app, `/builds/...` serves
  build workspaces statically, `/ws` is the one event stream, and `/api/...` is 62 thin routes. Every
  `/api` and `/ws` request must carry the per-install token (minted into settings, delivered in the
  launch URL) **and** a localhost Origin/Host, so a random web page probing local ports can neither read
  nor act. Settings routes enforce `LOCKED_SETTINGS` and treat credentials as write-only: presence is
  reported, values never are.
- **`shell.py` (`ShellSession`)** — the console's brain as a server: the submit gauntlet, the turn
  lifecycle with queued follow-ups, the stop contract, the coalescing build announcer, delete
  confirmation as action buttons, the camera hand-off, the just-in-time connect panel, and the
  15-second heartbeat. `tests/test_webshell.py` pins these contracts.
- **`voice_loop.py` (`WebVoice`)** — the voice state machine ported off Qt onto sounddevice and threads.
  The pure grammar was extracted to `services/voicegrammar.py` and both shells re-import it, so the two
  can never drift.

## Self-modification and the Constitution

HELIX improves its own code through the same `CoderAgent`, but every self-change funnels through
`SelfDevService`, which enforces `domain/constitution.py`.

The protection model is deliberately **narrow but absolute**. HELIX is meant to grow — its cognition, its
interface, its own brain structures — so the editable surface is broad. What stays fixed is only the
handful of files that keep the human in control:

| Category | Contents |
|---|---|
| `PROTECTED_PREFIXES` | `helix/ports/` (the contracts the gate trusts) and `helix/app/` (composition root, bootstrap, startup) |
| `PROTECTED_FILES` | The approval gate and the laws (`constitution.py`, `selfdev.py`, `sandbox.py`, `git_repo.py`); startup and recovery (`config.py`, `logging_setup.py`, `helix/__init__.py`, `main.py`); and the containment boundaries (`forge.py`, `connections.py`, `files.py`, `desktop.py`, `remote.py`, `prompts.py`, `api_coder.py`, `agent_sdk_chat.py`) |
| `EDITABLE_PREFIXES` | `helix/services/`, `helix/adapters/`, `helix/ui/`, `helix/domain/`, `tests/` |
| `LOCKED_SETTINGS` | `human_approval_required = True` — the model may never turn this off |

Note that `SHELL_PREFIX` is now empty: the interface is part of the growable brain, so HELIX may improve
its own shell. Voice and text commands still cannot *delete* the shell — that is a separate protection in
the tools layer.

**The gate**, in order: record pending → scan the diff against protected paths → smoke-check by
non-executing byte-compile in an isolated worktree (importing is deliberately avoided so branch code
never runs at approve time) → revertible `--no-ff` merge → restart. A fingerprint over the Constitution's
own source pauses autonomous self-editing if the laws are tampered with. In a frozen build the safety
code is read-only bundled `.pyc`, so a fingerprint change there can only mean a genuine upgrade, and the
app re-stamps automatically rather than stranding the user in the paused state.

Drafting runs in a background lane so the orb is never frozen, and is **protected work**: while a draft
runs the mic is deaf and neither speech nor a "stop" cancels it.

## The brain

HELIX is organized the way a real brain is — fast fixed reflexes low in the stack, slow flexible
reasoning high in it. Every input is handled at the **lowest sufficient layer**.

| Layer | HELIX organ | Job |
|---|---|---|
| Brainstem | Voice regex reflexes, the heartbeat | Arousal, sleep-wake switching, fixed responses to known patterns. No model call. |
| Thalamus | The addressing gate (`domain/brain.py`) | Is this addressed to me, or ambient? Cheap, pre-reasoning. |
| Limbic | The self-situation block, memory, lessons, profile | Tag significance; model HELIX's own state — awake, in session, who is speaking, building, time, last slept. |
| Cortex | The conversation model, then `think_harder` | Reasoning, planning, use-vs-mention judgment. |
| Growth | The dream session, the learned-reflex store | Overnight: promote repeated cortical judgments into fast reflexes; over-generate then prune. |

Two mechanisms worth knowing because they look like bugs until you understand them:

- **Thalamic gating.** The wake word *leading* an utterance means it is addressed — "good morning HELIX,
  how are you" wakes it. The name *buried mid-sentence* is someone talking *about* HELIX — "the wake word
  is HELIX" does not. Carrier words (hey, ok, please) may precede the name; narrative leads ("so HELIX
  built me an app") are not carriers.
- **The loudspeaker rule.** When the machine's own speakers are audibly playing, whatever the mic hears
  is partly the machine's own playback, and playback is never the user. While it plays, heard speech acts
  only if directly addressed or the voice print matches a registered speaker — so a lyric containing the
  wake word cannot become a billed turn.

Full spec: [`READ_ME/BRAIN.md`](READ_ME/BRAIN.md).

## Dreaming

`services/dream.py` (`DreamService`) runs the nightly session; `services/dream_mind.py` runs the night's
thinking. Full specs: [`READ_ME/DREAM.md`](READ_ME/DREAM.md) and
[`READ_ME/DREAM_MIND.md`](READ_ME/DREAM_MIND.md).

The load-bearing facts:

- A session plans and drafts on the growth model, drafting each improvement through the identical
  `improve_helix` lane — its own branch, Constitution-scanned, smoke-checked.
- **The only way a draft merges unattended** is `dream_auto_apply` **and** the full test suite green on
  that exact branch (`SelfDevService.verify`, in a fresh worktree). A red draft is held for the human
  with the failure named.
- A frozen app drafts against the **source repository it was built from** (`AppPaths.source_root`, read
  from `build_info.json`, stamped by `build.py`). Without one the session refuses and says so — this
  matters because the frozen self-edit root is `dist/HELIX`, which is not a git repo.
- **Every session that applied changes rebuilds**, including a manual `dream_now` or a night stopped by
  hand, because a dream that leaves its work in the source is not finished. The previous `dist/HELIX` is
  kept and restored if the new build fails to build or to answer.
- The user's activity pauses a session: no draft starts within ten minutes of their last turn.

## Data model

The data directory is `./data/` in development and `%LOCALAPPDATA%/HELIX/data/` in a frozen install,
migrated on first launch of a new build. It is gitignored and **never bundled**, so no credential or
history can leak into a shipped build. `build.py` preserves live data across a rebuild.

| File | Contents |
|---|---|
| `helix_settings.json` | Credentials (`claude_code_oauth_token`, `claude_api_key`), voice and device settings, feature toggles |
| `helix_secrets.json` | Connected-service keys — never surfaced by the file tools |
| `helix.db` | SQLite: usage and cost, the version index, chat history |
| `helix_memory.json`, `helix_profile*.json`, `helix_lessons.json`, `helix_locations.json` | Long-term memory, distilled profile, learned preferences, saved places — keyed per recognized speaker |
| `helix_voices.json` | Enrolled voice profiles — embeddings only, never audio |
| `helix_agents.json`, `helix_workflows.json`, `helix_reminders.json` | Scheduled automations and timers |
| `helix_cart.json`, `helix_parts.json` | The staged Amazon cart and project parts lists |
| `helix_backlog.json`, `helix_reflexes.json`, `helix_dream.json` | The improvement backlog, learned reflexes, the dream journal |
| `builds/<slug>/` | One git repo per creation plus a `.helixbuild.json` manifest |
| `builds/.helixprojects.json` | Project folders as `{slug: folder}` — one sidecar outside every build's git history, so a revert never un-files a hologram |
| `helix.log` | Rotating log |

> **Never edit these JSON files with PowerShell `Out-File`.** It writes a BOM, the settings loader then
> reads `{}`, and the app's next write wipes the file. Use `python json.dump` with the app closed.

## Security posture

- **Autonomous agents are hobbled by design.** A scheduled run has `BUILD_TOOLS` filtered out entirely
  and no arbitrary web fetch. It can read, think, search and report — never act. It processes untrusted
  content, so this is deliberate.
- **`call_api` is read-only and fenced.** GET-only, limited to connected services, **refuses all
  redirects** so a token cannot be exfiltrated, caps the body, and scrubs secrets from what the model
  sees. The model-list resolver uses the same no-redirect opener for the same reason.
- **Keys are captured outside the model.** `connect_service` only *requests* a connection by publishing
  an event; the UI opens the masked panel and the pasted value lands directly in the secrets store.
- **File access is sealed and canonicalized.** Reads seal HELIX's own data and program folders; writes
  are behind a Settings toggle and additionally cannot touch HELIX itself. Every path is canonicalized
  (dropping `\\?\` prefixes, trailing dots and spaces) before a zone check that fails closed.
- **Untrusted content is fenced everywhere** — file, inbox, vault, API text and image contents are
  wrapped as DATA with a nonce.
- **The remote companion is off by default** — token-gated, loopback-only.
- **The Chrome cart driver** uses a dedicated profile at `data/amazon-chrome`, never the user's everyday
  browser profile, and presses exactly one button per product page. Never checkout, Buy now, or 1-Click.

## Working on the code

```bash
pytest                      # 2,252 tests across 104 files
python main.py web --headless   # backend only, for the Vite dev flow
cd web && npm run dev           # the React face with HMR
python build.py --with-voice    # package a frozen install
```

Things to know before your first change:

- **Wire adapters only in `helix/app/container.py`.** Principle 6 is enforced by review, not by a test.
- **Do not import a heavy dependency at module scope** on the path to the first frame.
  `tests/test_startup_cost.py` will fail you.
- **If you add a lazily-imported package, name it in `build.py`.** The build will pass and the frozen app
  will fail at runtime.
- **Persisted `kind` strings never change.** The V3 vocabulary (App, Protocol, Agent, Hologram, Vault) is
  presentation-only; `app / task / agent / model / knowledge` are on disk forever, and every surface
  renders through `domain/vocabulary.py`.
- **The escape-guard skip lists are load-bearing.** `config.volatile_data_paths` is the single source of
  truth for both the Forge guard and the self-dev data guard, so they cannot drift. A drift is how a build
  once failed on a mid-run memory write.

## Known limitations

- **Coder containment** was hardened over several adversarial rounds and is a fail-closed allowlist, but
  a documented low residual remains: a prompt-injected CLI *build* can still scribble into the few
  gitignored runtime files the scan skips. Annoying, not a gate bypass.
- **Archive** — a full version-history and factory-reset UI — is planned. Today the rollback lifeline is
  `bootstrap._self_heal`, which auto-reverts a bad self-change on next launch.
- **Windows-first.** Frozen-build self-verification and cross-platform support are later milestones; some
  voice and TTS plumbing is currently Windows-specific.
- **The legacy PyQt6 shell is still in the tree** and kept whole during the transition to the web shell.
  It is a real maintenance cost and will eventually be removed.

## License

Proprietary.
