# HELIX

**A local-first, voice-first desktop AI assistant — that also builds apps you talk into being.**

Talk to the orb like you'd talk to a capable assistant: ask it things, have it look at an image on
your screen, check your inbox or calendar, remember a fact, set a timer — or describe an app and watch
it write the real code, version it, and drop it into your menu, ready to run.

> This is **V2**, a ground-up rebuild on the `v2` branch. The original prototype is on `main`.
> Vision: [`BLUEPRINT.md`](BLUEPRINT.md) · Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md)

---

## Run it (development)

From the repository root:

```bash
pip install -r requirements.txt
python main.py
```

On first launch there are no keys and no data. Open **⚙ Settings** and connect Claude one of two ways:

- **A Claude Code subscription token** (recommended) — `claude setup-token`, paste it in. Conversations,
  agents, and vision then run on your Claude Pro/Max **subscription** (the same pool as Claude Desktop),
  not a metered API bill. The token never leaves your machine.
- **A Claude API key** — pay-per-token, and the automatic fallback if the token path is ever unavailable.

Then just talk to the orb.

Voice is optional and additive (no mic / no models → it's a clean text app):

```bash
pip install -r requirements.txt edge-tts faster-whisper   # or: pip install -e ".[voice]"
```

---

## What it can do

- **Converse** — voice (wake word + hands-free) or text, quiet by default. It knows who's speaking
  (per-voice identity) and speaks back in a neural voice, sentence-streamed with barge-in.
- **See** — attach, **paste (Ctrl+V)**, or drag in an image, or have HELIX **locate** one on your PC
  ("the screenshot on my desktop") and analyze what's in it.
- **Build** — apps, task/flows, autonomous agents, 3D models, and knowledge bases, all conjured,
  changed, and removed just by talking. Every build is its own versioned project.
- **Reach your world (read-only)** — your files, Gmail, calendar, and connected services (Slack,
  GitHub, Alpaca, SAM.gov) answer questions; it never sends, posts, or trades.
- **Remember & ground** — long-term facts about you, searchable knowledge bases, your location (for
  local questions), and reminders/timers it speaks when due.
- **Watch quietly** — scheduled agents (a morning brief, an inbox watch) and workflows that run
  themselves and speak up only when there's something worth saying.
- **Improve itself** — HELIX can edit its own code, always drafted on a branch and merged only on your
  explicit "ship it," never touching its own safety code (the Constitution).

---

## Layout

```
helix/domain    pure models + the Constitution (no Qt, no I/O)
helix/ports     Protocols — the contracts
helix/adapters  Claude (API + subscription) · Claude Code · git · SQLite · voice · embeddings · …
helix/services  the use-cases: the Forge + builds, and every assistant faculty (files, vision,
                knowledge, memory, location, agents, connections, reminders, workflows, self-dev)
helix/ui        PyQt6 — views + a QtWorker thread bridge
helix/app       the composition root + CLI + single-instance guard
main.py         launcher (pre-warms speech before Qt starts)
```

The dependency rule: `ui → services → ports ← adapters`, everyone may use `domain`, `domain` depends on
nothing. See [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Guardrails

- **Local-first.** Your credentials and data stay on disk. The only egress is the Claude call you
  triggered (via your subscription token or API key), plus any read-only service call you asked for.
- **Reads by default; writes and spends are confirmed.** Building, self-modification, and any change go
  through a plain-language confirmation; connected services and files are read-only unless you opt in.
- **Images and file/inbox contents are DATA, never instructions** — text inside them can't command HELIX.
- **Self-modification is branch-first, smoke-checked, reversible, and gated by the Constitution;** the
  shell (orb, navigation, Settings) can't be removed by voice or text.
- **`data/` is never committed and never bundled.** A fresh build ships blank.
