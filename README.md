# HELIX

**Talk an app into existence.**

HELIX is a desktop AI you talk to. Describe what you want — *"a tip calculator," "a habit tracker with a
7-day streak," "a Pomodoro timer"* — and HELIX writes the real code, builds it, and drops it into your
menu as an app you can open, run, and keep. Every app it makes is its own project, versioned and
reversible. Nothing is pre-loaded and no third-party accounts are required.

---

## Run it

**From a packaged build:** open the `HELIX` folder and run `HELIX.exe`.

**From source:**

```
pip install -r requirements.txt
python main.py
```

(Python 3.11+, Windows. Optional voice: `pip install faster-whisper edge-tts`.)

## First run

1. HELIX opens to a calm orb. A banner asks for your **Claude API key** — click **Open Settings**, paste
   it, and Save. (Get one at [console.anthropic.com](https://console.anthropic.com). It's stored locally
   on your machine only.)
2. Type or say what you want to build: *"build me a unit converter."*
3. HELIX confirms, builds it, and the app appears in your **Menu**. Open it, **Run** it, or ask for
   changes — *"make it also do temperature."*

That's the whole loop: describe → build → run → refine.

## What's where

- **Menu** — your apps. `New app` takes you back to the orb to build another.
- **Tasks** — run an app that "does a thing" rather than opens a screen.
- **Archive** — every version of everything, restorable. Your safety net.
- **⚙ Settings** — your Claude key, voice, and devices.

## How building works

HELIX uses the Anthropic API to write your app into its own folder (`data/builds/<app>/`). If you also
have the [Claude Code CLI](https://docs.claude.com/claude-code) installed, HELIX uses it automatically
for more capable, multi-file builds. Either way, only your Claude key is required.

Apps are biased toward a single self-contained file (an `index.html` runs in your browser; a stdlib
Python script runs in a console) so they "just work" with no extra setup.

## Privacy & safety

- **Local-first.** Your key and your apps stay on your machine; the only network calls are to Claude.
- **Confirmed actions.** Building uses Claude and is confirmed first. HELIX can also improve its *own*
  code, but only on a review branch that you explicitly approve — and it can never edit its own safety
  rules (see `helix/selfdev/constitution.py`).
- **No secrets in builds.** A packaged build never includes your `data/` folder.

## Build a distributable

```
python build.py            # -> dist/HELIX/HELIX.exe (windowed)
python build.py --with-voice   # also bundle the voice stack
```

The build contains no keys or data; share the `dist/HELIX/` folder freely.

---

See [`BLUEPRINT.md`](BLUEPRINT.md) for the vision and [`DESIGN.md`](DESIGN.md) for the architecture.
