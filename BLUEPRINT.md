# HELIX — Blueprint

> **The north star + the interface overhaul.** This is the "where we're going" doc.
> For "what already exists in the code today," read [`DESIGN.md`](DESIGN.md) — it is the source of truth
> for the engines. This blueprint redraws the **shell** on top of those engines and names the concept.
>
> **For a fresh Claude session:** read `DESIGN.md` first (the foundation is real and substantial — vision,
> voice, self-improvement, and investing all work today). Then build the **Console** described here.
> **Keep every engine. Replace the surface.**

---

## 1. The heart

**HELIX is the mind of the house.**

Not an app you operate — a presence you talk to. It is always on. It **sees** (cameras), **hears** and
**speaks** (a wearable earpiece), **acts** across the home, and **improves its own code** when you ask.
You move around the house with a tiny earpiece and HELIX is simply *there* — answering, watching,
keeping the place stocked, handling the busywork, and getting better every week because you talked to it.

It should feel like **J.A.R.V.I.S.**: calm, capable, anticipatory, one step ahead — and **simple**. One
quiet screen. One voice. Everything else handled underneath.

The test for every design choice: *would Tony just say it out loud and trust it to be done?* If a feature
needs a tab, a form, and three clicks, we've failed the heart of it.

---

## 2. Design principles

- **Voice-first, glance-second, click-last.** You mostly *talk*. The screen keeps you *aware*. You rarely click.
- **One screen.** No tabs to hop. The whole app is a single, calm console. (The current **Investment tab**
  is the reference: one screen, the essentials visible, every knob baked to a smart default and hidden.)
- **AI proposes, human approves — for anything that spends money or reaches outward.** Trades, grocery
  orders, code merges, emails: HELIX drafts, you confirm with one word. Never silent.
- **Anticipate, don't ask.** Surface what matters (milk is low, someone's at the door, a fix is ready)
  before being asked. Pull, not push; ambient, not noisy.
- **Smart defaults over knobs.** HELIX thinks under the hood and self-calibrates. New settings are a last
  resort, tucked behind one ⚙.
- **Local-first.** Everything runs on the dedicated laptop. Data and secrets stay on the machine; the only
  egress is deliberate Claude / broker / store calls.
- **Elegant + dark.** The cyan/amber HUD aesthetic stays. Lots of breathing room. Nothing shouts.

---

## 3. The interface — **the HELIX Console**

Replace the five tabs with **one surface**: the Console. Its signature element is the **Presence** — a
living orb that *is* HELIX. You talk to the orb; a few ambient tiles keep you aware; everything deep is a
sentence away.

```
┌─────────────────────────────────────────────────────────────────┐
│  ◉  HELIX                     “Listening, sir.”              ⚙   │   Presence · status · settings
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│     ╭─ conversation ──────────────────────────────────────╮      │
│     │  You    ·  what's in the fridge?                     │      │
│     │  HELIX  ·  Milk, eggs, leftovers. You're low on      │      │
│     │            milk — add it to the Fry's order?         │      │
│     ╰──────────────────────────────────────────────────────╯      │
│                                                                  │
│   ┌ House ───────┐ ┌ Money ───────┐ ┌ Supplies ────┐ ┌ Self ───┐ │   ambient tiles
│   │ Garage clear │ │  $12,430  ▲   │ │ 2 low        │ │ 1 fix   │ │
│   │ 2 chores due │ │  +1.2% today │ │ milk · soap  │ │ ready   │ │
│   └──────────────┘ └──────────────┘ └──────────────┘ └─────────┘ │
│                                                                  │
│        🎤  hold to talk        ·        say “HELIX” to wake       │
└─────────────────────────────────────────────────────────────────┘
```

### 3.1 The Presence (the orb)
A single living indicator, center-stage, that breathes and reacts. It carries the whole JARVIS feeling.
Its states are the only "status UI" you need:

- **Idle** — slow breathing glow. HELIX is listening for "HELIX."
- **Listening** — bright pulse tracking your voice level.
- **Thinking** — a calm shimmer.
- **Acting** — a focused spin ("Drafting that… / Checking the fridge…").
- **Speaking** — pulses in time with the reply.

Wake word **"HELIX"** or push-to-talk. The earpiece (Jabra Elite 4) is the primary I/O; the laptop screen
is the *window*, not the controller.

### 3.2 The conversation
The center of the screen. What you said, what HELIX said and **did**. Voice-first; type if you prefer.
This is today's Xpert assistant **promoted to the whole app** — it already drives every faculty through the
tool router (`helix/ai/actions.py`).

### 3.3 Ambient tiles (glance, don't manage)
Three or four calm cards. Each is *awareness*, not a menu. Tap to expand; or just **ask** about it.

- **House** — the eyes: who's home / at the door, anything a camera flagged, chores due.
- **Money** — the part you love: balance, today's P/L, the equity sparkline. Tap → the full Investment
  console (keep it exactly as it is — it's the reference design). This is the one "deep" view.
- **Supplies** — the building shopping list: what's low, what HELIX wants to reorder. One tap to approve.
- **Self** — pending self-improvements (Approve / Reject) + HELIX's health (running, last self-update).

Tiles update themselves on a quiet timer. No refresh buttons in your face.

### 3.4 One settings door
A single **⚙** holds the rarely-touched config: API keys, cameras (name → source), voice device, project
repos, grocery account. Everything else is a smart default.

### 3.5 What this replaces
- The **5 tabs collapse into the Console.** Home / Enterprise / Learning become **tiles + voice intents**,
  not navigation. **Investment** survives as the deep "Money" view behind its tile (unchanged — it's the
  gold standard). **Xpert** *becomes the whole app*.
- The old per-pillar screens stay in the code as deep views, reachable from a tile or a sentence — never
  the first thing you see.

---

## 4. Architecture — faculties on a thin shell

HELIX is **one agent (X)** with **faculties**, surfaced by the Console and orchestrated by the tool router.
Domains (home, money, work) are *things it helps with*, not places you navigate.

```
                         ┌──────────────────────────────┐
            Console  ──▶  │  Presence (orb) · conversation │  ◀── voice (earpiece) / type
            (one screen)  │  ambient tiles · ⚙            │
                         └───────────────┬──────────────┘
                                         │  intents
                         ┌───────────────▼──────────────┐
            The agent X   │   tool router  (ai/actions)  │   the JARVIS "hands"
                         └───────────────┬──────────────┘
        ┌──────────┬──────────┬──────────┼──────────┬───────────────┐
     Hear/Speak   See       Act        Improve     Money          Home
     ai/transcribe vision/  home+store  selfdev/    investment/    home/
     ai/speech    (eyes)    (groceries) (self-code) (Alpaca+Claude) (chores)
        └──────────┴──────────┴──────────┴──────────┴───────────────┘
                                         │
                         ┌───────────────▼──────────────┐
            Foundation    │  Claude (brain) · memory (SQLite) · settings · always-on supervisor │
                         └──────────────────────────────┘
```

**Faculties (X's body — most already built):**
- **Hear / Speak** — local STT (`ai/transcribe`) + neural TTS (`ai/speech`), wake word, earpiece routing. ✅
- **See** — `helix/vision/`: any camera (USB or RTSP/IP), ask anything (`look`, `look_around`). ✅
- **Think** — `ai/claude` (Opus brain) + the multi-turn tool loop. ✅
- **Act** — `ai/actions` tool router: start/stop investing, home tasks, look, self-improve, **(new) order**. ✅/🔨
- **Improve** — `helix/selfdev/`: talk → it codes itself → email/voice approve → merge → auto-restart. ✅
- **Remember** — `core/memory` (SQLite) + `core/settings`. ✅
- **Stay alive** — `scripts/run_helix.py` supervisor, auto-launch, clean exit, crash survival. ✅

**To build for the house-assistant vision:**
- **The Console shell** — the new single-window UI (Presence + conversation + tiles + ⚙). *(the overhaul)*
- **Inventory + groceries** — fridge/pantry cams → "what's low" → smart list → **Fry's/Kroger cart** (official
  Cart API; you tap checkout). A new `helix/home/groceries.py` + a `Supplies` tile. Gated like a trade. 🔨
- **Ambient awareness loop** — cameras + inventory + chores feed the tiles on a timer; the door/known-face
  watch alerts proactively. 🔨
- **Devices (future hardware)** — `helix/devices/` for serial/MQTT when X grows hands. Same concept, new
  faculty. 🔭

---

## 5. What HELIX does, in plain terms

- **Runs the house.** Ask anything; it sees, knows, and handles it. "Who's at the door?" "What's in the
  garage?" "Is the laundry done?" "What's this tool?"
- **Keeps you stocked.** Watches the fridge/pantry, builds the shopping list, and on your "yes" puts the
  order in your Fry's cart.
- **Improves itself.** "HELIX, add a morning summary" → it writes the code, emails you the diff, and on
  your "ship it" merges and restarts. It also fixes its own crashes.
- **Grows the money.** Auto-invests (paper now, gated path to real), shown in the one deep view you love.
- **Talks the whole time.** Hands-free, anywhere in the house, through a tiny earpiece.

One presence. One screen. Your whole house, handled.

---

## 6. Build phases (the overhaul)

Each phase ships and is testable. Keep the engines; rebuild the shell incrementally so HELIX never goes dark.

1. **The Console shell** — new single window: Presence orb (5 states) + the conversation (promote Xpert) +
   a slim status line. Wire it to the existing router/voice. The old tabs still reachable behind a temporary
   "More" button so nothing is lost mid-migration.
2. **Ambient tiles** — House, Money, Supplies, Self. Each reads existing engines on a quiet timer. Money tile
   opens the current Investment screen verbatim.
3. **Inventory + groceries** — `groceries.py` (Kroger Cart API), the Supplies tile + voice ("order
   groceries", "what's low"), confirmation gate + spend cap. Fridge-cam → low-stock → list.
4. **Ambient awareness** — proactive: door/known-face alerts, "you're low on X," chores due — surfaced by the
   orb/tiles without being asked.
5. **Retire the tabs** — once the Console covers daily use, remove the "More" button. Deep views live behind
   tiles + voice only.

Polish throughout: motion on the orb, sound cues, the HUD palette, large-type readability across the room.

---

## 7. Guardrails (non-negotiable)

- **Money & outward actions are always confirmed.** Trades, grocery checkout, code merges, emails — HELIX
  drafts; you approve by voice or reply. Spend caps on groceries. Reuse the existing spoken-confirmation gate.
- **Self-modification is branch-first.** Never touches `main` without approval; smoke-checked; one-command
  rollback; never edits its own safety/approval code unflagged. (Already true — keep it.)
- **Privacy on people.** Camera person-analysis is **appearance only** (no identity, no web lookup). Online
  profiling stays a separate, deliberately-gated, later feature — and carries real legal weight (biometric
  laws); treat with care.
- **Local-first.** Secrets never leave the machine except for the explicit API calls.

---

## 8. For the implementing session — how to start

1. Read `DESIGN.md` end to end. The foundation is large and works — **do not rebuild engines.**
2. Build **Phase 1 (the Console shell)** in `helix/interfaces/` as a new main window; keep `run_qt_app`'s
   clean-exit + prewarm + supervisor wiring. Promote `XpertTab`'s conversation to the center.
3. Reuse the HUD stylesheet and the Investment screen as-is (the aesthetic reference).
4. Move tab by tab into tiles; keep a temporary escape hatch to old views until the Console wins.
5. Keep every change small, committed, and verified — and let HELIX help build itself.

**The whole point:** make it so simple and so present that running the house is just a conversation.
Capture that, and HELIX is no longer an app. It's JARVIS.
