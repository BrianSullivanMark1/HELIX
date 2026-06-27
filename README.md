# HELIX

**A local-first desktop app-builder you talk to.** Describe an app in plain language; HELIX writes the
real code, builds it into its own versioned project, and drops it into your menu — ready to run.

> This is **V2**, a ground-up rebuild on the `v2` branch. The original prototype is on `main`.
> Vision: [`BLUEPRINT.md`](BLUEPRINT.md) · Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md)

---

## Run it (development)

```bash
pip install -r requirements.txt
python main.py
```

On first launch there are no keys and no data. Open **⚙ Settings**, paste your Claude API key, and the
orb comes alive — then just tell it what to build.

Voice is optional:

```bash
pip install -r requirements.txt edge-tts faster-whisper   # or: pip install -e ".[voice]"
```

---

## Layout

```
helix/domain    pure models + the Constitution (no Qt, no I/O)
helix/ports     Protocols — the contracts
helix/adapters  Anthropic · Claude Code · git · SQLite · voice
helix/services  the use-cases (the Forge, builds, self-dev, agents, tasks, the 3D baker)
helix/ui        PyQt6 — views + a QtWorker thread bridge
helix/app       the composition root + CLI
main.py         launcher
```

The dependency rule: `ui → services → ports ← adapters`, everyone may use `domain`, `domain` depends on
nothing. See [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Guardrails

- **Local-first.** Your key and apps stay on disk; the only egress is the Claude API call you triggered.
- **Spending & self-modification are confirmed.** Building is confirmed first; HELIX editing its own
  code is branch-first, smoke-checked, reversible, and gated by the Constitution.
- **`data/` is never committed and never bundled.** A fresh build ships blank.
