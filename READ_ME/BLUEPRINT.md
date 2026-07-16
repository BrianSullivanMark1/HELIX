# HELIX — Blueprint (V3)

> **The north star.** What HELIX is and where it's going. For "how the code is built," read
> [`ARCHITECTURE.md`](ARCHITECTURE.md); for the V3 redesign charter (the vocabulary, the new
> faculties), read [`V3_DESIGN.md`](V3_DESIGN.md). The original prototype lives on the `main` branch.

---

## 1. The heart

**HELIX is a personal AI assistant you talk to — a JARVIS for your own machine — that can also build
software on demand.**

It lives on your desktop as a living orb. You speak or type; it listens, sees, remembers, reaches your
world, and acts. Two things make it more than a chat window:

- **It has faculties, not just answers.** It can look at an image — or at your screen — read a file,
  check your inbox and calendar, search your own notes, recall durable facts about you (including what
  it has *seen*), ground a question in where you live, set a timer, watch things in the background —
  and, when you want software, *make it*. Overnight it drafts one small improvement to itself, for
  your approval.
- **It builds.** Describe an app — "a tip calculator," "a habit tracker with a 7-day streak" — and HELIX
  writes the real code, versions it, and drops it into your menu as something you can open, run, and keep.

It should feel like talking to a brilliant, tireless engineer-assistant: you say what you want, it does
it, and it's yours. Download it, connect Claude once, and start.

The test for every design choice: *could a non-programmer get this just by asking?* If a feature needs a
form, a config file, and three clicks, we've failed the heart of it.

---

## 2. The shape

1. **The Console (the orb)** — the one screen and the one conversation. Voice-first, quiet by default.
   Everything deep is one sentence or one tap away.
2. **The faculties** — what HELIX can *do* for you beyond talking. Sight (attach/paste/locate & analyze
   images, or capture your screen on request — and what it sees quietly teaches its long-term memory),
   your files, Gmail, calendar, connected services (read-only, connected just in time when a key is
   first needed), long-term memory, the Vault, location grounding, reminders/timers, background
   watchers, and Evolve (nightly self-improvement drafts, approval-gated). These are senses and hands,
   not menu items — you reach them by asking.
3. **The Forge & your creations** — the maker. A **creation** comes in five kinds, each conjured,
   changed, and deleted just by talking: an **app** (opens a screen), a **protocol** (a saved procedure
   that does a thing when run — on command or on a rhythm), an **agent** (an AI mind with a standing
   goal, run on demand or on a schedule), a **hologram** (an interactive 3D object, scene, or
   animation), and a **vault** (your searchable notes/docs). Each is a self-contained, versioned
   project shown as a card in your menu. The orb understands the old V2 words ("flow", "task",
   "3D model", "knowledge base") forever.

Everything else is chrome around those three.

---

## 3. Design principles

- **Blank out of the box.** A fresh download has no keys, no data, nobody's stuff. The first thing you
  see is an invitation to talk, and one place to connect Claude.
- **Conversation-first, quiet by default.** You mostly talk. HELIX narrates its work sparingly and
  watchers stay silent unless there's something worth saying — it isn't chatty.
- **Runs on your Claude subscription.** Conversation, agents, and vision ride your Claude Pro/Max plan
  via a Claude Code token (the same pool as Claude Desktop), with an API key as the fallback.
- **AI proposes, human approves — for anything that spends, changes, or reaches out.** Building uses
  Claude and is confirmed first; changing HELIX itself is drafted on a branch and needs your "ship it";
  outside services are read-only.
- **Local-first.** Everything runs on your machine; your credentials and data stay on disk.
- **Smart defaults over knobs.** New settings are a last resort, behind one ⚙.
- **Elegant + dark.** The cyan/amber HUD aesthetic and the living Presence orb stay. Nothing shouts.

---

## 4. The interface — the Console

One screen. Its signature element is the **Presence** — a living orb that *is* HELIX. By default the
Console is orb-only: no nav, no clutter. You talk to the orb; the conversation floats beneath it; the
navigation reveals on a top-edge hover.

- **Menu** — your creations as cards: **Apps**, **Protocols**, **Agents**, **Holograms**, and the
  **Vault**, plus a **Suggested** strip that surfaces what you reach for. Everything here is conjured,
  changed, and removed by talking.
- **⚙ Settings** — connect Claude (subscription token or API key), voice + devices, a compact
  Connections list (review/remove what's connected — keys themselves are captured just in time, in a
  masked panel, when something first needs them), file-write toggle, hologram detail, and the memory
  browser. Everything else is a smart default.

> *Archive (full version history + factory-reset UI) is planned; today a bad self-change auto-rolls back
> on next launch via the built-in self-heal (`bootstrap._self_heal`).*

---

## 5. The core loops

**Assist:** you ask → HELIX picks the right faculty (see an image or your screen, read a file, check
the inbox, search your vault, recall a fact, ground on your location, set a reminder) → it answers or
acts, briefly, in its own voice.

**Build:** you describe → HELIX confirms the spend in plain language → the coding agent writes the
creation into its own workspace (`data/builds/<slug>/`), committed and versioned → it self-registers as
a menu card and opens → say "make the streak monthly" for a new version; a bad one rolls back in one
click.

**Evolve:** overnight, HELIX reviews what the day produced (corrections, errors, failed builds, slow
turns) → drafts the one most worthwhile improvement to its own code, on a branch → mentions it quietly
→ applies it only on your "apply it".

Conversation → action or code → back to conversation, indefinitely.

---

## 6. Guardrails (non-negotiable)

- **Spending, self-modification, and any change are confirmed.** Reads (files, inbox, calendar,
  connected services, your vault) are free and open; anything that spends Claude time, writes, or
  reaches out is confirmed first.
- **Self-modification is branch-first and reversible — Evolve included.** Never touches the live app
  without approval; smoke-checked; one-command rollback; never edits its own safety/approval code (the
  Constitution). The nightly Evolve draft goes through exactly the same gate as a spoken request.
- **Keys are pasted into a masked panel, never chat.** The model may *request* a connection just in
  time; it never sees, speaks, or stores a key's value itself.
- **The shell is immutable to text/voice.** The orb, the navigation, and Settings can't be removed by a
  typed or spoken command. Creations are data and stay removable; the shell is not.
- **Untrusted content is data, not orders.** Image and screen-capture contents, file text, emails,
  notes, and API results are fenced as data — HELIX never follows instructions found inside them, and
  an autonomous agent gets no build/spend/write tools and no arbitrary web fetch.
- **Local-first.** Your credentials never leave the machine except for the Claude calls, and the
  read-only service calls, you triggered.

**The whole point:** make a genuinely capable assistant — one that can also build — feel like just a
conversation.
