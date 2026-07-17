# THE HELIX BRAIN — a biomimetic mind

This is the gem: HELIX doesn't just answer, it *thinks like a situated entity*, with a mind organized
the way a real brain is — fast fixed reflexes low in the stack, slow flexible reasoning high in it, a
sense of its own state, and the ability to **grow**. A baby is born with reflexes and grows a cortex;
so does HELIX. This document is the durable anchor for that architecture — read it before touching any
`helix/**/brain*`, the voice reflexes, the self-situation block, or the growth loop.

The neuroscience below is real (structure names preserved for reference); the clean layer boundaries
are engineering abstractions over systems that are, in the brain, more distributed and parallel.

## The stack — fast/fixed low, slow/flexible high

| Layer | Brain analog | HELIX organ | Job | Latency |
|-------|-------------|-------------|-----|---------|
| **Brainstem** | Reticular activating system; reflex arcs | Voice regex reflexes (`voice.py`), the heartbeat | Arousal (awake/asleep), sleep-wake switching, fixed reflex responses to known patterns | Instant, no model call |
| **Thalamus** | Thalamic relay + reticular nucleus (attention gating) | Addressing / salience gate (`domain/brain.py`) | Decide what actually reaches the cortex: is this addressed to me, or ambient? | Cheap, pre-reasoning |
| **Limbic** | Amygdala (salience), hippocampus (memory), hypothalamus + insula (interoception/self-state) | Self-situation block + memory/lessons/profile | Tag input with significance; model HELIX's own internal state (awake, in session, who's speaking, building, time, last slept) | Per-turn context |
| **Cortex** | Neocortex + prefrontal executive | The conversation model (fast) → deep reasoner (`think_harder`) | Reasoning, language, planning, deliberate judgment — including *use-vs-mention* ("go to sleep" meant vs. described) | Slow, flexible, the model turn |
| **Growth** | Sleep consolidation, myelination, Hebbian wiring, developmental pruning | Evolve loop + learned-reflex store (`helix_reflexes.json`) | Overnight: promote repeated cortical judgments into fast reflexes; over-generate then prune; run on the strongest reasoning model | Nightly |

### The organizing principle (from real reflex arcs)
A reflex resolves in the spinal cord/brainstem in ~25 ms and never troubles the cortex — but a *copy*
of the signal still travels up for after-the-fact appraisal. HELIX mirrors this: a crisp "go to sleep"
is caught by a brainstem regex and acted on instantly; anything the reflexes don't recognize routes up
to the cortex (the model) to be reasoned about. **Every input is handled at the lowest sufficient layer.**

## Thalamic gating — "is this addressed to me?"

The **cocktail-party effect**: your own name breaks through an unattended channel because high-salience
tokens have a *permanently lowered recognition threshold* (Treisman's attenuation model). HELIX's
addressing gate (`domain/brain.py:is_addressed`) encodes this:

- The wake word **leading** an utterance = directed at HELIX, even in a long sentence: *"good morning
  HELIX, how you doing"* is addressed (salutation + name up front), and wakes it.
- The name **buried mid-sentence** as a topic = someone talking *about* HELIX: *"the wake word is
  HELIX"*, *"you wake it by saying HELIX"* — ambient, does not wake.
- A short utterance that *is* the address (≤3 words containing the name) = addressed.

Salience tokens (the name, salutations like good morning / hey / are you there) get the lowered
threshold; everything else stays attenuated.

## Limbic self-state — interoception, a situated self

The **insula** integrates the body's internal signals into a *subjective sense of being a self*. HELIX
gets a proprioceptive analog: each turn carries a compact **self-situation block** describing its own
condition — awake or resting, whether a conversation session is live, who it's speaking with, whether a
build is running, the time of day, how long since it last slept. The cortex reasons *from* this, so
"where am I in this conversation?" is answerable. This is what makes the difference between a stateless
responder and an entity that knows where it is.

## Growth — the baby grows up

Two real mechanisms, both in the Growth layer:

1. **Consolidation / myelination (Hebbian).** A judgment the cortex makes *repeatedly and the same way*
   should migrate into a fast pathway — "neurons that fire together wire together," then the pathway
   myelinates and runs faster next time. In HELIX: when the model repeatedly judges a phrase to be a
   genuine sleep request (via `go_to_sleep`), that phrase consolidates into a **learned reflex**
   (`helix_reflexes.json`) — next time it fires instantly in the brainstem, no model call. The cortex
   taught the brainstem.
2. **Developmental plasticity (over-generate → prune).** A baby brain overproduces synapses then prunes
   the ones experience doesn't validate. HELIX's Evolve loop over-generates candidate improvements and
   prunes learned reflexes that go unused or were wrong, keeping the mind lean and experience-fitted —
   not a static config.

### How a self-change is drafted (the growth pipeline)
When Evolve (or the user) asks HELIX to improve itself, `SelfDevLane` runs `SelfDevService.propose()`
on a background thread:
1. Refuse unless the working tree is clean, the constitution fingerprint matches, and no git hooks are
   planted.
2. Create an **isolated git worktree** in a temp dir (never the live tree) on a `selfdev/` branch.
3. The **growth coder** (the Claude Code CLI on the subscription token, pinned to Fable 5) edits code
   in that worktree, streaming plain-language progress.
4. Guards, all fail-closed: a **source-escape** scan (the coder must not write the live source outside
   its worktree) and a **data-guard** (it must not write into `data/`). The data-guard SKIPS the app's
   own volatile stores (`config.volatile_data_paths` — helix.db, agents/memory/reflexes, the log,
   secrets), because the live app keeps writing those during the minutes-long draft and they are not
   the coder. **This shared skip list is the single source of truth for both the Forge and self-dev
   guards, so they can never drift** (a drift is how a build once failed on a mid-run memory write).
5. The staged diff is scanned against the **constitution** (only `services/`+`adapters/` `.py`, never
   the shell/domain/ports/safety files), committed on the branch, and surfaced as a pending change.
6. Nothing merges without a human "apply it": approve re-scans the branch tip, smoke-checks it
   (non-executing byte-compile) in a fresh worktree, then does a revertible `--no-ff` merge.

Self-improvement is **protected work**: while a draft runs the mic is deaf and neither speech nor a
"stop" cancels it (only closing the app, or the internal timeout). Its high-level steps are spoken
aloud — even when the mic is asleep — and the orb wears the working hue with an "Improving myself"
pill, so it's always visible that HELIX is growing.

The constitution fingerprint (a hash over the safety code) is a tamper tripwire. In a **frozen build**
the safety code is read-only bundled `.pyc` — impossible to edit out of band — so a fingerprint change
there can only mean a new version shipped, and the app **re-stamps automatically** (a genuine upgrade
never strands the user in the paused state). In dev mode, where the source IS editable, the strict
compare-and-pause stands.

### Growth runs on the strongest mind available — Fable 5, auto-upscaling
Growth is where HELIX rewrites itself, so it must reason with the best model it can reach. The deep
reasoner and the Evolve loop are pinned to **Fable 5** (`claude-fable-5`) — and the model resolver
(`adapters/model_select.py`) queries the live model list so that when a stronger model in the same line
appears (a future **Fable 6**, or a higher Opus), HELIX **automatically upscales** its growth reasoning
to it. It always grows on the most capable brain Anthropic offers, without a code change. The everyday
conversation stays on a fast model; only the deliberate, self-modifying reasoning reaches for the top.

## Invariants (do not regress)
- Reflexes never call the model; the model never does a reflex's job. Lowest sufficient layer.
- The addressing gate is the ONLY thing that wakes a sleeping mic (plus explicit wake phrases) — a
  mentioned name never wakes.
- Learned reflexes are whole-utterance, addressed-only, capped, and prunable — a consolidated reflex
  can never fire from ambient speech or grow unbounded.
- Growth (`go_to_sleep` consolidation, Evolve) stays fenced from autonomous agent runs (BUILD_TOOLS)
  and behind the human-approval gate; the constitution is unchanged.
- Growth reasoning resolves to the top available model (Fable 5 → newer); everyday turns do not.
- `model_select.py` (adapters/) sends the API key to the fixed `api.anthropic.com/v1/models` host, GET
  only, with a no-redirect opener so the key can't leak via a 3xx — same posture as `call_api`. It is
  in the editable self-improvement surface deliberately: it holds no gate logic, only a model-ranking
  read, and every self-change is still behind the human-approval lock. `reflexes.py` (services/) is
  likewise editable but can only ever *rest* the mic (never build/spend/wake), and `go_to_sleep` is
  agent-fenced — so neither needs the constitution's protected status. The safety-critical pieces (the
  addressing gate in `domain/brain.py`, the voice wiring in `ui/`) ARE immutable (domain + shell).
