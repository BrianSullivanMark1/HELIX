# HELIX

**A local-first, voice-first desktop AI presence that converses, sees, remembers, builds, and
improves itself.**

Talk to the orb like you'd talk to a capable assistant: ask it things, show it an image — or your
screen — check your inbox or calendar, remember a fact, set a timer. Or describe something you want
made and watch it write the real code, version it, and drop it into your menu, ready to run. And
overnight, it quietly drafts one small improvement to itself for your approval.

> This is **V3**, the JARVIS cut. Design charter: [`V3_DESIGN.md`](V3_DESIGN.md) ·
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

Then just talk to the orb. That one credential is the only setup wall — every *other* key (Slack,
GitHub, a hologram engine) is asked for **just in time**, the moment something actually needs it.

Voice is optional and additive (no mic / no models → it's a clean text app):

```bash
pip install -r requirements.txt edge-tts faster-whisper   # or: pip install -e ".[voice]"
```

A packaged install is built with `python build.py --with-voice`; the frozen app keeps its data in
`%LOCALAPPDATA%/HELIX/data` (live data survives a rebuild).

---

## What it can do

- **Converse** — voice (wake word + hands-free) or text, quiet by default. It knows who's speaking
  (per-voice identity) and speaks back in a neural voice, sentence-streamed with barge-in. Everyday
  turns run fast on Sonnet; hard questions escalate themselves to the deep reasoner (`think_harder`).
- **See** — attach, **paste (Ctrl+V)**, or drag in an image; have HELIX **locate** one on your PC
  ("the screenshot on my desktop"); or just say **"look at my screen"** and it captures the display
  and answers in the same breath.
- **Learn from what it sees** — every image turn quietly teaches it: durable visual facts (the breaker
  panel's model, the dog's name and breed) are distilled into long-term memory, so next week the
  answer needs no photo.
- **Make things** — five kinds of creation, all conjured, changed, and removed just by talking, made
  by the **Forge**: **Apps** (interactive screens), **Protocols** (saved procedures that do a thing
  when run), **Agents** (AI minds with a standing goal), **Holograms** (interactive 3D objects and
  scenes), and a **Vault** (your searchable notes and documents). Every creation is its own versioned
  project.
- **Reach your world (read-only)** — your files, Gmail, calendar, and connected services (Slack,
  GitHub, Alpaca, SAM.gov) answer questions; it never sends, posts, or trades. Connections happen
  **just in time**: when a key is needed, a masked panel opens naming the service and why — no
  settings wall, and no key ever typed into chat.
- **Remember & ground** — long-term facts about you (per speaker), the Vault, your location (for
  local questions), and reminders/timers it speaks when due.
- **Watch quietly** — scheduled agents (a morning brief, an inbox watch) and workflows that run
  themselves and speak up only when there's something worth saying.
- **Evolve** — nightly, HELIX reviews what the day produced (corrections, errors, failed builds) and
  drafts one concrete improvement to its own code — always on a branch, smoke-checked, constitution-
  scanned, and applied only on your explicit approval. It never ships itself.

---

## Layout

```
helix/domain    pure models + the Constitution + the V3 vocabulary (no Qt, no I/O)
helix/ports     Protocols — the contracts
helix/adapters  Claude (API + subscription) · Claude Code · git · SQLite · voice · embeddings · …
helix/services  the use-cases: the Forge + creations, and every assistant faculty (files, sight,
                vault, memory, location, agents, connections, reminders, workflows, evolve/self-dev)
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
- **Keys are pasted into a masked panel, never chat.** The model can *request* a connection; it never
  sees a key's value.
- **Images, screen captures, and file/inbox contents are DATA, never instructions** — text inside them
  can't command HELIX. Captures are ephemeral: analyzed, answered, never persisted.
- **Self-modification — including Evolve's nightly drafts — is branch-first, smoke-checked, reversible,
  and gated by the Constitution;** the shell (orb, navigation, Settings) can't be removed by voice or
  text.
- **`data/` is never committed and never bundled.** A fresh build ships blank.
