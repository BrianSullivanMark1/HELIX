# HELIX V3 — Design Charter

V3 is the JARVIS cut. Same machine underneath — the Forge, the orb, the subscription brain, the
sentinel — but the surface is redesigned around one idea: **one presence that converses excellently,
sees, remembers, acts, and improves itself.** No settings walls, no jargon taxonomy, no dead weight.

## 1. The new vocabulary

Five words, all speakable, all obvious. This is a **presentation-layer** rename: every persisted
`kind` string, folder name, and JSON format stays byte-identical to V2 (a V2 data dir loads in V3
untouched). The rename lives in labels, prompts, UI text, and voice — one source of truth:
`helix/domain/vocabulary.py` (new; `tool_labels.py` folds into it).

| V2 word            | V3 word       | What it is                                                        |
|--------------------|---------------|-------------------------------------------------------------------|
| App                | **App**       | An interactive screen HELIX builds. Kept — no better word exists. |
| Task / "Flow"      | **Protocol**  | A saved procedure that DOES a thing when run — on command or on a rhythm. JARVIS runs protocols. |
| Agent              | **Agent**     | An AI mind with a standing goal. Kept — the right word. Scheduled agents are **watchers**. |
| Model (3D)         | **Hologram**  | The visual channel: an interactive 3D object, scene, or animation the orb conjures. |
| Knowledge (base)   | **Vault**     | The user's own saved notes, documents, and gathered results — searchable, local, private. |

Umbrella: things HELIX makes are **creations** (internal kind strings remain `app / task / agent /
model / knowledge`). The maker keeps its name: the **Forge**.

**Voice compatibility:** the orb understands the old words forever. "Build me a flow", "delete that
task", "show me a 3D model", "my knowledge base" all resolve to the new concepts — synonyms are listed
in the system prompt, so nothing the user learned in V2 breaks.

## 2. What is removed or merged

- **The Connections settings wall** — the per-service API-key grid in Settings is gone as the primary
  path. Keys are captured **just in time** (see §4). A single slim "Connections" manager remains for
  review/removal only.
- **`tool_labels.py` + `_progress_label` duplication** — one vocabulary module feeds both the spoken
  labels and the progress pills.
- **Vestigial UI** — any view unreachable from the V3 shell is deleted (candidates confirmed during
  implementation: legacy commands view, dead tabs).
- **Prompt sprawl** — the persona is rewritten tighter (see §6); duplicated guidance collapses.

## 3. New faculty: Sight (screen + images that teach)

- **`view_screen`** — a new always-on tool: "look at my screen", "what am I looking at?", "help me
  with this error" → HELIX captures the display (Pillow `ImageGrab`, off the GUI thread), runs it
  through the existing vision pipeline, and answers in one breath. The capture is ephemeral like every
  image — never persisted.
- **Visual memory (auto-training on images)** — every image turn now *teaches* HELIX. After it
  answers, a background distiller (same pattern as `MemoryService.after_turn`) extracts durable
  visual facts — "the silver breaker panel in Brian's garage is a Square D QO 200A", "the dog is a
  brindle boxer named Rex" — and stores them per speaker in long-term memory. Next week, "what was
  that breaker model?" answers from memory, no photo needed. Facts, not pixels, are stored.
- **Vision excellence** — image turns get dedicated guidance: read all text verbatim when asked,
  count precisely, name what's off, compare multi-image sets, and answer the question asked rather
  than describing generically.

## 4. New faculty: just-in-time Connections

The rule: **HELIX asks when it needs, never before.** When a capability needs a key (a watcher, a
built protocol, `call_api`, Tripo holograms), the model calls **`connect_service`** — a masked,
native dialog appears naming the service and why; the user pastes the key there (never in chat, never
spoken); it lands in the same encrypted-at-rest secrets store V2 used. All V2 injection machinery
(env for protocols/apps, read-only `call_api`, redirect refusal) is unchanged. Settings keeps only a
compact "Connections" list — what's connected, when last used, one Remove button each. No key value
is ever displayed back.

## 5. New faculty: Evolve (self-improvement without Claude sessions)

The self-improvement loop moves **inside** HELIX. A seeded watcher, **Evolve** (nightly), mines what
the day produced — lessons learned from corrections, errors in the log, failed builds, slow turns —
and drafts a concrete improvement through the existing `improve_helix` pipeline (branch, smoke-check,
constitution scan). It never applies anything itself: the draft surfaces as a quiet suggestion chip
and one line in the Morning Brief ("I drafted a fix for the reminder repeat bug — say apply it when
ready."). Approval and rollback are the V2 gates, untouched. The constitution's protections
(PROTECTED_FILES, SHELL_PREFIX, human-approval lock) are exactly as strong as before — Evolve is a
*client* of the gate, never a bypass.

## 6. Conversation excellence

- **Persona v3** — the CONSOLE_SYSTEM prompt is rewritten in the new vocabulary, tighter and more
  JARVIS: lead with the answer, one breath by default, dry wit allowed one clause at a time,
  anticipate the next need with at most one short offer. All V2 behavioral hard rules kept
  (confirm-before-spend, no markdown aloud, no tool names aloud, untrusted-data fencing).
- **Context assembly kept** — per-speaker profile, memory, location, lessons, ambient vault recall
  already ride every turn; visual memory (§3) joins them.
- **Web is a reflex** — current-fact questions search first, answer plainly, never disclaim "no
  internet". (Already true in V2; the persona keeps it front of mind.)
- **think_harder stays** — hard questions escalate to the deep reasoner; everything else stays fast.

## 7. Compatibility & invariants

- Persisted formats: **unchanged** (builds metadata, secrets, voices, memory, knowledge, settings).
- A V2 `%LOCALAPPDATA%/HELIX/data` migrates into V3 with zero steps: it just loads.
- Security invariants that must not regress (verified by the existing test suite):
  `call_api` redirect refusal · BUILD_TOOLS fence for autonomous agents · WebFetch off agent runs ·
  PROTECTED_FILES / SHELL_PREFIX / EDITABLE_PREFIXES · knowledge/file/api-body fencing · CSPRNG
  nonces · subscription workdir off the data tree · single-instance mutex · voice-identity gate.
- Version: `3.0.0`. The exe stays `HELIX.exe`; built with `python build.py --with-voice`.
