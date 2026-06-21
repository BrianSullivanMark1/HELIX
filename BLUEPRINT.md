# HELIX — Blueprint

> **The north star.** What HELIX is and where it's going. For "how the code works today," read
> [`DESIGN.md`](DESIGN.md).

---

## 1. The heart

**HELIX is an app-builder you talk to.**

You describe an app in plain language — "a tip calculator," "a habit tracker with a 7-day streak,"
"a Pomodoro timer" — and HELIX writes the real code, builds it, and drops it into your menu as an app
you can open, run, and keep. Every app it makes is its own project, versioned and reversible.

It should feel like talking to a brilliant engineer who never gets tired: you say what you want, it
makes it, and it's yours. No setup, no accounts, no boilerplate. Download it, add your Claude key once,
and start inventing.

The test for every design choice: *could a non-programmer get a working app just by describing it?*
If a feature needs a form, a config file, and three clicks, we've failed the heart of it.

---

## 2. The two layers

1. **The Forge** — the engine. The conversation (the orb) plus the coding agent behind it. This is the
   product: it turns a sentence into working code. (Internally this is `helix/selfdev/` + the Console.)
2. **Your apps** — what the Forge makes. Each one is a self-contained project in its own folder, shown
   as a card in your menu, runnable, and versioned. (We call one an *app*; internally a *Build*.)

Everything else is just the chrome around those two things: a menu of your apps, a place to run them,
and a history you can roll back.

---

## 3. Design principles

- **Blank out of the box.** A fresh download has no keys, no data, no pre-loaded anybody's stuff. The
  first thing you see is an invitation to build, and one field for your Claude key.
- **Conversation-first.** You mostly talk or type. The screen keeps you aware; you rarely hunt through menus.
- **AI proposes, human approves — for anything that spends or changes the app itself.** Building uses
  Claude and is confirmed first; changing HELIX's own code is drafted on a branch and needs your "ship it."
- **Local-first.** Everything runs on your machine. Your key and your apps stay on disk; the only egress
  is the deliberate Claude API call.
- **Smart defaults over knobs.** New settings are a last resort, behind one ⚙.
- **Elegant + dark.** The cyan/amber HUD aesthetic and the living Presence orb stay. Nothing shouts.

---

## 4. The interface — the Console

One screen. Its signature element is the **Presence** — a living orb that *is* HELIX. You talk to the
orb; the conversation sits beneath it; everything deep is one sentence or one tap away.

```
┌─────────────────────────────────────────────────────────────────┐
│  ◉  HELIX                  “Listening.”      ☰ Menu ⚡ Tasks 🗂 Archive │
├─────────────────────────────────────────────────────────────────┤
│   ▸ Add your Claude API key to start building apps.   [Open Settings] │  (only until a key is set)
│                                                                  │
│                         (  ◉  the orb  )                          │
│                                                                  │
│     You    ·  build me a tip calculator                          │
│     HELIX  ·  A tip calculator that asks for the bill and        │
│               percent and shows the tip + total — build it?      │
│     You    ·  yes                                                 │
│     HELIX  ·  Built Tip Calculator. It's in your menu now.        │
└─────────────────────────────────────────────────────────────────┘
```

- **Menu** — your apps. The cards are the apps you've built; `New app` returns you here to the orb.
- **Tasks** — run an app (for apps that "do a thing" rather than open a screen).
- **Archive** — version history + restore + the factory-reset lifeline. Always reachable.
- **⚙ Settings** — your Claude key, voice, and devices. Everything else is a smart default.

---

## 5. The core loop

1. **Describe** — you tell the orb what you want.
2. **Build** — HELIX confirms, then the coding agent writes the app into its own workspace
   (`data/builds/<app>/`), on a branch, committed.
3. **Appears** — the app self-registers as a menu card and opens.
4. **Run / iterate** — open it from the menu, or say "make the streak monthly" to build a new version.
   Every version is kept; a bad one rolls back in one click.

Conversation → code → a runnable, versioned app → conversation, indefinitely.

---

## 6. What HELIX is **not** (anymore)

HELIX began as a personal assistant with built-in Investment, Home, Work, Fabrication, and Vision
pillars. Those were one person's tools — they needed private accounts, keys, and hardware, and they
couldn't work for someone who just downloaded the app. They've been removed. HELIX is now one thing
done well: **the engine that builds apps.** Anything those pillars did, you can now ask HELIX to *build*.

---

## 7. Guardrails (non-negotiable)

- **Spending and self-modification are confirmed.** Building uses Claude (confirmed first). Changing
  HELIX's own code is drafted on a branch and merged only on an explicit yes.
- **Self-modification is branch-first and reversible.** Never touches the live app without approval;
  smoke-checked; one-command rollback; never edits its own safety/approval code. (The Twelve
  Commandments in `helix/selfdev/constitution.py`.)
- **Apps are sandboxed to their own folder.** A built app lives in its own workspace and is told never
  to reach outside it.
- **Local-first.** Your key never leaves the machine except for the Claude API calls you triggered.

**The whole point:** make it so simple that building software is just a conversation.
