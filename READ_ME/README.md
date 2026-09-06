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

That opens **the web shell** — HELIX's face is a React app served over `127.0.0.1`, shown in its own
window (Edge WebView2 via pywebview). Variants:

- `python main.py web --browser` — open in your default browser instead of the app window.
- `python main.py web --headless` — backend only (prints the tokened URL; used by the Vite dev flow).
- `python main.py qt` — the legacy PyQt6 shell, kept whole during the transition.

Working on the face itself? `python main.py web --headless` in one terminal, `npm run dev` in
`web/` in another, then open the printed URL's `?t=` token against `http://localhost:5173`.

On first launch there are no keys and no data. Open **⚙ Settings** and connect Claude one of two ways:

- **A Claude Code subscription token** (recommended) — `claude setup-token`, paste it in. Conversations,
  agents, and vision then run on your Claude Pro/Max **subscription** (the same pool as Claude Desktop),
  not a metered API bill. The token never leaves your machine.
- **A Claude API key** — pay-per-token, and the automatic fallback if the token path is ever unavailable.

Then just talk to the orb. That one credential is the only setup wall — every *other* key (Slack,
GitHub) is asked for **just in time**, the moment something actually needs it. The same goes for the
hologram engine: holograms are compiled by **OpenSCAD** (free, open source, a separate program), and if
it isn't on the machine the first time you ask for one, HELIX offers to install it (winget, about a
minute) and builds once you say yes.

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
  when run), **Agents** (AI minds with a standing goal), **Holograms** (3D models you design by
  talking — written as Python on the build123d CAD kernel, shown in a live STUDIO with parameter
  sliders that recompile in about a second, exportable as STEP/STL/3MF tuned for a Bambu P1S; the
  parts library knows real Arduino/ESP32/Pi/relay footprints so an enclosure comes out FITTING; also
  360° scenes), and a **Vault** (your searchable notes and documents). Every creation is its own
  versioned project.
- **Reach your world (read-only)** — your files, Gmail, calendar, and connected services (Slack,
  GitHub, Alpaca, SAM.gov) answer questions; it never sends, posts, or trades. Connections happen
  **just in time**: when a key is needed, a masked panel opens naming the service and why — no
  settings wall, and no key ever typed into chat.
- **Remember & ground** — long-term facts about you (per speaker), the Vault, your location (for
  local questions), and reminders/timers it speaks when due.
- **Watch quietly** — scheduled agents (a morning brief, an inbox watch) and workflows that run
  themselves and speak up only when there's something worth saying.
- **Dream** — set a window ("dream tonight from eleven for eight hours", or the Dreaming card in
  Settings) and HELIX spends the night improving itself non-stop, in rounds until the window closes —
  each round reflecting again on what the night has found. While it dreams the orb sleeps (indigo, an
  aurora drifting through it) and HELIX talks in its sleep: murmurs about the page it is reading or the
  change it is making drift past the orb, whispered aloud only when you're there. The one-draft
  "Evolve" pass it grew out of was retired on 2026-09-05. Any session that applied changes — a manual
  one too — says so, quits, rebuilds and relaunches itself, so the improvement is what runs next.
  It plans on Fable, drafts change
  after change on a branch each, and — only if you switch on automatic applying — merges a draft when
  its full test suite passes on it; anything red waits for you. It can rebuild and relaunch itself
  before you wake (the previous build is kept and restored if the new one fails), and the first time
  you speak to it in the morning it tells you, once, what the night did. "No dreaming tonight"
  switches nightly dreaming off until you turn it back on; "stop dreaming" ends the session running
  now; "how did you sleep?" reads the night back. With the Dream Mind (READ_ME/DREAM_MIND.md) the
  night also reflects on what HELIX can and can't do, researches the questions your projects raise
  on documentation, manufacturer and distributor pages, verifies engineering facts against their
  sources, and tries experiments in a throwaway copy that ships nothing — the **Dream journal** page
  (◐ Dream in the nav, or from the Menu and the Dreaming card) shows every night: what it discovered
  (each line sourced, the unverified marked), the facts it verified with host and date, what it
  tried, and what it applied.

---

## Layout

```
helix/domain    pure models + the Constitution + the V3 vocabulary + cadpy (the hologram language)
helix/ports     Protocols — the contracts
helix/adapters  Claude (API + subscription) · Claude Code · git · SQLite · voice · build123d · …
helix/services  the use-cases: the Forge + creations, and every assistant faculty (files, sight,
                vault, memory, location, agents, connections, reminders, workflows, dream/self-dev)
helix/api       THE WEB SHELL's backend: FastAPI over localhost + the shell brain + the Qt-free voice loop
helix/cad       the hologram compile worker (a subprocess; the only importer of the CAD kernel)
helix/ui        PyQt6 — the legacy shell, kept whole during the transition
helix/app       the composition root + CLI + web/Qt bootstraps + single-instance guard
web/            the React face (Vite + react-three-fiber): the orb, the console, the hologram studio
main.py         launcher (pre-warms speech first; routes web/qt/cadworker/watchdog)
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
- **Self-modification — the dream session's nightly drafts included — is branch-first, smoke-checked, reversible,
  and gated by the Constitution;** the shell (orb, navigation, Settings) can't be removed by voice or
  text.
- **`data/` is never committed and never bundled.** A fresh build ships blank.

## Amazon: HELIX searches, verifies, stages, and hands the cart over

"Find me an INMP441 mic and two 28mm speakers" → HELIX searches amazon.com itself (live prices, stars,
Prime, pictures on screen), stages verified ids at the planned quantities (the cart panel shows them),
and on "go" drives its own Chrome window to press Add-to-Cart per item and read Amazon's cart back. A
project's parts list (`save_parts` / `stage_parts`) stages a whole BOM at once and logs the handoff as
the expense trail. HELIX never buys — checkout is yours, on Amazon. Details: ARCHITECTURE.md §7c.

## The maker flow: describe a device, get a printable enclosure that fits

"A hat cam that sees, hears and talks, on a battery" → HELIX picks the parts from its component
library (132 real boards, mics, amps, speakers, cells, chargers, switches, sensors — real sizes, a
confidence per number, holes only from manufacturer drawings), you save them to the project's parts
list with their library keys and face hints, and `design_enclosure` plans the box deterministically:
a pocket, standoffs or a bay per part, the lens bore and mic hole and speaker grille through the
face, USB and switch openings on their wall, screw towers with heat-set inserts, debossed labels — then
compiles it into an ordinary hologram with a fit report. "Check fit on camera" projects it over the
live view at true scale (calibrate once on a credit card) with a ghost pocket per part, so the real
parts go inside their ghosts before anything prints; a part the library doesn't know is measured with
the camera's ruler in real millimetres. Print on your go, with a print sheet for the P1S. No dimension
is ever typed from memory. Details: ARCHITECTURE.md §7d and READ_ME/MAKER_FLOW.md.
