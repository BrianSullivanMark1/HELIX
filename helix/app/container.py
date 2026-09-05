"""Container — the ONE place adapters are constructed and wired into services.

Nothing else in the codebase builds an adapter. Swapping an implementation (e.g. the coder) is a
one-line change here. This module is a PROTECTED_PATH: the self-coder may never edit the wiring.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable

from helix.adapters.agent_sdk_chat import PreferredChat, SubscriptionBrain, raise_no_rail
from helix.adapters.anthropic_chat import AnthropicChat
from helix.adapters.api_coder import ApiCoder
from helix.adapters.claude_code_cli import ClaudeCodeCli
from helix.adapters.coder_select import FallbackCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.model_select import GrowthModelResolver
from helix.adapters.build123d_cad import Build123dCad
from helix.adapters.rebuild import Rebuilder
from helix.adapters.restart import Restarter
from helix.adapters.signal_bus import SignalBus
from helix.adapters.speech import EdgeSpeechOut, OsSpeechOut, WhisperSpeechIn, active_model
from helix.adapters.sqlite_store import SqliteStore
from helix.adapters.system_clock import SystemClock
from helix.adapters.voyage_embed import VoyageEmbedder
from helix.config import AppPaths
from helix.domain.errors import MissingApiKey
from helix.domain.models import Role
from helix.logging_setup import get_logger, setup_logging
from helix.ports.llm import Text, Turn
from helix.services.agents import AgentService
from helix.services.build_queue import BuildQueue
from helix.services.builds import BuildService
from helix.services.calendar import CalendarService
from helix.services.connections import ConnectionsService
from helix.services.conversation import ConversationService
from helix.services.files import FilesService
from helix.services.forge import ForgeService
from helix.services.gmail import GmailService
from helix.services.images import load_image_block
from helix.services.knowledge import KnowledgeService
from helix.services.desktop import DesktopService
from helix.services.dream import DreamService
from helix.services.evolve import EvolveService
from helix.services.reflexes import ReflexService
from helix.services.lessons import LessonsService
from helix.services.location import LocationService
from helix.services.memory import MemoryService
from helix.services.profile import ProfileService
from helix.services.recommend import RecommendService
from helix.services.remote import RemoteService
from helix.services.suggestions import SuggestionService
from helix.services.reminders import ReminderService
from helix.services.scheduler import AgentScheduler
from helix.services.tasks import TaskService
from helix.services.workflows import WorkflowService
from helix.services.prompts import CONSOLE_SYSTEM, DEEP_THINK_SYSTEM
from helix.services.selfdev import SelfDevService
from helix.services.selfdev_lane import SelfDevLane
from helix.services.components import ComponentService
from helix.services.maker import MakerService
from helix.services.parts import PartsService
from helix.services.shopping import ShoppingService
from helix.services.tools import ToolRegistry
from helix.services.voiceid import VoiceIdService

# The module logger the orphan sweep below reports through. It was referenced there without ever
# being defined, so the first launch that actually found an orphaned app server would have died in
# Container.__init__ with a NameError inside its own except clause.
_LOG = get_logger("container")


def _migrate_agents(settings: JsonSettings, agent_store: JsonSettings) -> None:
    """One-time lift of the 'agents' list from the old (guarded) settings file into its own file. Runs
    only when the new store is empty and the old key is present, then clears the old key so it can't be
    resurrected — and can't keep getting byte-reverted mid-build in its old home."""
    if agent_store.get("agents"):
        return  # already on the dedicated store
    legacy = settings.get("agents")
    if not legacy:
        return
    agent_store.set("agents", legacy)
    settings.set("agents", [])  # leave a tombstone (not delete) — JsonSettings has no delete


# THE SENTINEL: default background watchers, shipped as ordinary scheduled agents (data, not shell —
# rename/pause/delete them like anything else). Each goal ends with the QUIET convention: the shell
# only speaks a scheduled report when something actually crossed a threshold, so an unconnected or
# uneventful watcher makes no sound. Lookback windows match each cadence, so a persisting condition
# is flagged when it CROSSES its threshold rather than re-announced forever (agent runs are
# stateless by design). Seeded once per version; deleting one is honored forever.
_WATCHERS_SEED_VERSION = 1
# Every goal carries the same operating rules: a six-tool-call budget per run (batch independent
# call_api requests into ONE round and keep the last round for the reply — running out mid-gather
# must still end in a spoken summary or QUIET, never a stall), small payloads (limit/per_page kept
# low — responses are re-sent to the model on every round), and the QUIET convention.
_BUDGET_RULE = (
    " You have a budget of six tool calls for this run: batch independent call_api requests together "
    "in one round, keep payloads small, and always leave the final round to speak your reply — if "
    "you run low, summarize what you have instead of fetching more."
)
_DEFAULT_WATCHERS: tuple[tuple[str, str, str], ...] = (
    ("Morning Brief",
     "Deliver Brian's spoken morning brief: ONE plain-prose summary under 100 words of what happened "
     "overnight, no markdown, no lists read aloud. Batch these in one round: GitHub — call_api "
     "https://api.github.com/user/repos?per_page=40&sort=pushed (note overnight pushes to repos named "
     "like BRMS, MRP, WMS, or APS, and whether Brendan, Alex, Kate, or Thoa look quiet for two or "
     "more days); Alpaca — https://api.alpaca.markets/v2/positions for drift away from commodities, "
     "industrials, and hard assets, or any tilt toward financials or insurance; SAM.gov — "
     "https://sam.gov/api/prod/sgs/v1/search/?index=opp&q=manufacturing&page=0&size=10"
     "&sort=-modifiedDate&mode=search&is_active=true, counting only notices whose publishDate is "
     "overnight against the current date in context. With a spare call, "
     "check open PRs on the most active of those repos "
     "(https://api.github.com/repos/OWNER/REPO/pulls?state=open&per_page=20) for any older than 24 "
     "hours. Skip any service call_api says isn't connected — never mention missing keys. If "
     "genuinely nothing happened, say so in one short sentence." + _BUDGET_RULE,
     "every morning at 8"),
    ("GitHub Watcher",
     "Watch GitHub for Brian's team. Call call_api https://api.github.com/user/repos?per_page=40"
     "&sort=pushed and focus on repos named like BRMS, MRP, WMS, or APS. Flag ONLY: a push in the "
     "last two hours whose message signals a breaking change (breaking, revert, rollback, hotfix, "
     "force), or an open pull request that crossed the 24-hour mark within the last two hours — check "
     "https://api.github.com/repos/OWNER/REPO/pulls?state=open&per_page=20 and compare created_at to "
     "the current time in context, batching the PR checks in one round. Speak at most three short "
     "sentences naming the repo and person. If nothing matches, or GitHub isn't connected, reply "
     "with exactly: QUIET" + _BUDGET_RULE,
     "every 2 hours"),
    ("Slack Watcher",
     "Watch Slack for urgent messages aimed at Brian or his team. Use call_api on read endpoints: "
     "https://slack.com/api/conversations.list?limit=50&exclude_archived=true&types=public_channel,"
     "private_channel,im then, batched in one round, "
     "https://slack.com/api/conversations.history?channel=CHANNEL_ID&limit=20&oldest=UNIX_SECONDS "
     "(35 minutes ago, from the current time in context) for the three or four busiest channels. "
     "Flag ONLY messages from the last 35 minutes that read urgent: mentions of Brian, or words like "
     "urgent, ASAP, down, broken, blocked, or a customer escalation. One short sentence per flag, "
     "three at most. If nothing urgent, or Slack isn't connected, reply with exactly: QUIET"
     + _BUDGET_RULE,
     "every 30 minutes"),
    ("Portfolio Watcher",
     "Watch Brian's Alpaca portfolio for allocation drift. Call call_api "
     "https://api.alpaca.markets/v2/positions and reason from the tickers you know. Brian's strategy "
     "favors commodities, industrials, and hard assets (Kiyosaki, Dalio, Jiang); financial and "
     "insurance exposure should stay minimal. Flag ONLY a clear tilt: financials or insurance above "
     "roughly ten percent of portfolio value, or any single position drifting past a third of the "
     "portfolio. Two short sentences at most. If allocation looks fine, or Alpaca isn't connected, "
     "reply with exactly: QUIET" + _BUDGET_RULE,
     "every 3 hours"),
    ("Procurement Watcher",
     "Watch SAM.gov for new federal solicitations relevant to Brian's business. Call call_api "
     "https://sam.gov/api/prod/sgs/v1/search/?index=opp&q=manufacturing+software&page=0&size=10"
     "&sort=-modifiedDate&mode=search&is_active=true, and batch one or two more q keywords in the "
     "same round: MES, MRP. Only notices whose publishDate falls within the last day, against the "
     "current date in context, count as new. Flag solicitations matching manufacturing software, "
     "MES, MRP, WMS, or compliance tooling — title, agency, and response deadline, one line each, "
     "three at most. If nothing new matches, or SAM.gov isn't connected, "
     "reply with exactly: QUIET" + _BUDGET_RULE,
     "every morning at 9"),
)


def _seed_watchers(agent_store: JsonSettings, agents) -> None:
    """Seed the default sentinel watchers ONCE per seed version. The store-level version key (not a
    field on the agents — _save would drop it) makes this idempotent AND makes deletion stick: a
    watcher the user removed never comes back on the next launch. Never raises — a seeding hiccup
    must not stop HELIX from starting."""
    try:
        if int(agent_store.get("watchers_seed_version") or 0) >= _WATCHERS_SEED_VERSION:
            return
        for name, goal, hint in _DEFAULT_WATCHERS:
            if not agents.exists(name):  # never clobber a user's same-named agent
                agents.add(name, goal, schedule_hint=hint)
        agent_store.set("watchers_seed_version", _WATCHERS_SEED_VERSION)
    except Exception:  # noqa: BLE001
        pass


class _LazyBaker:
    """Wiring only: defers ModelBaker's construction until a MODEL build actually reaches its check.

    It used to stand between launch and a ~955 ms mesh stack (trimesh + networkx + scipy); the baker
    now compiles through the CadEngine and imports nothing heavy, but the seam stays: the Forge only
    ever reaches for the baker from a build worker, and nothing a launch draws needs it, so the viewer's
    page templates are parsed the first time a hologram is built rather than before the first frame.
    Same object shape Forge expects — prepare() / check() / bake() / engine_missing() — it neither
    knows nor cares. The shape is the whole contract here, and it is the easy thing to break: when
    prepare() was added to ModelBaker and the Forge started calling it before every hologram's coder
    run, this proxy still forwarded only the older three, so every hologram build in the RUNNING app
    would have died with an AttributeError while the suite — which hands the Forge a real ModelBaker —
    stayed green. tests/test_container_wiring.py now asserts the proxy forwards every public method the
    real class has, so a new baker method cannot ship half-wired again."""

    __slots__ = ("_make", "_real")

    def __init__(self, make: Callable[[], object]) -> None:
        self._make = make
        self._real: object | None = None

    def _get(self):
        if self._real is None:
            self._real = self._make()
        return self._real

    def prepare(self, workspace):
        return self._get().prepare(workspace)

    def check(self, workspace):
        return self._get().check(workspace)

    def bake(self, workspace):
        return self._get().bake(workspace)

    def engine_missing(self) -> bool:
        return self._get().engine_missing()


# The vision critic's instruction. It judges the RENDERED preview against the coder's own brief: a
# contradiction (a floating part, a blind hole, a missing feature) is what the repair pass can fix; style
# is not, and a picky critic would spend the ONE repair pass on taste. "exactly OK" gives the parser a
# fixed token to look for, so a good design is never sent back for a sentence of praise.
_CRITIC_SYSTEM = (
    "You are checking a 3D design HELIX just compiled for 3D printing on a Bambu Lab P1S. Here is the "
    "design brief and parameters, measurements HELIX took off the compiled model (overall size, volume, "
    "printability analysis), and the rendered preview. Reply with ONE short sentence naming the single "
    "most important problem that contradicts the brief (a floating/disconnected part, a hole that does "
    "not go through, a feature in the brief that is missing, grossly wrong proportion, a measured size "
    "that cannot fit what the brief says it holds) — or exactly OK if it looks right. Be strict about "
    "contradictions and lenient about style."
)
_CRITIC_MAX_CHARS = 200


def _critic_verdict(reply_text: str) -> str | None:
    """The critic's words → None (it looks right) or ONE problem sentence for the repair prompt.

    The parse is lenient about HOW the model says OK ("OK", "OK.", "OK — it matches the brief",
    "**OK**", '"OK"', "Okay") because a false problem is the expensive mistake: it burns the build's
    only repair pass on a design that was right. Leading punctuation is skipped before the first word is
    read — a model that bolds or quotes its verdict used to be parsed as a PROBLEM, and the repair prompt
    then read "Looking at the rendered preview…: **OK**. Fix the model…" — exactly the waste this
    leniency exists to prevent. A real problem sentence never opens with the word OK. Anything else is
    clipped to its first sentence, capped so a rambling verdict cannot swell the repair prompt."""
    text = " ".join((reply_text or "").split())
    if not text:
        return None
    first_word = re.split(r"[^A-Za-z0-9]+", re.sub(r"^[^A-Za-z0-9]+", "", text), maxsplit=1)[0]
    if first_word.casefold() in ("ok", "okay"):
        return None
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    if len(sentence) > _CRITIC_MAX_CHARS:
        sentence = sentence[: _CRITIC_MAX_CHARS - 1].rstrip() + "…"
    return sentence or None


def make_hologram_critic(chat, rail_usable: Callable[[], bool], record_usage=None):
    """The baker's `critic(preview_png, brief_text) -> problem | None`, on the fenced rail-preferring
    chat.

    `chat` is the unattended PreferredChat (without_web()): the critic looks at a picture HELIX rendered
    and must never be able to search or fetch. It carries the preview as an Image block in a user Turn,
    and BOTH rails can see it — PreferredChat hands the pictures to the subscription's run_hermetic as
    vision, and falls back to AnthropicChat.chat otherwise. That matters because the one machine this
    redesign is for runs subscription-only: wired to the API chat behind an API-key gate, every hologram
    there compiled and rendered while the "vision critique feeds the repair loop" step silently never
    happened. `rail_usable` says whether ANY rail can serve (subscription active or an API key set); when
    neither can, the critic abstains without loading the picture — it still compiles, it just skips the
    look. The closure never raises: a critic outage (no rail, a network blip, an unreadable PNG) must
    never fail a build, so every failure reads as "looks right" and the build goes on."""
    log = get_logger("container")

    def _critic(preview_png, brief_text: str) -> str | None:
        try:
            if not rail_usable():
                return None
            image = load_image_block(preview_png)
            if image is None:
                return None
            blocks = (
                image,
                Text("Design brief and parameters:\n" + (brief_text or "(no brief)")),
            )
            reply = chat.chat([Turn(Role.USER, blocks)], system=_CRITIC_SYSTEM)
            if record_usage is not None:
                try:
                    u = reply.usage
                    record_usage(u.input_tokens, u.output_tokens, u.cost_usd)
                except Exception:  # noqa: BLE001 — the ledger is a nicety, the verdict is the job
                    pass
            return _critic_verdict(reply.text)
        except Exception:  # noqa: BLE001
            log.warning("hologram critic unavailable; skipping the look", exc_info=True)
            return None

    return _critic


def _deep_think_on_api(deep_chat, subscription, question: str):
    """think_harder's API leg — the escalation the deep reasoner falls back to when the subscription
    rail didn't serve it.

    Its own function so the fallback can't lose the one thing it kept getting wrong: this chat is a
    BARE AnthropicChat (it has to be — PreferredChat would fire the expensive hermetic run a SECOND
    time, past the cancel guard, and would drop on_progress, cancel and the web access a user-asked
    reasoner is deliberately granted), and a bare adapter can only say "check your subscription token
    or add an API key". So the user who watched HELIX answer, then asked it to think harder, was told
    to re-issue a credential that had just worked. raise_no_rail says what actually happened instead —
    the same sentence PreferredChat gives the orb, from the same helper."""
    try:
        return deep_chat.chat([Turn(Role.USER, (Text(question),))], system=DEEP_THINK_SYSTEM)
    except MissingApiKey as exc:
        raise_no_rail(subscription, exc)


class Container:
    def __init__(self) -> None:
        # Foundation
        self.paths = AppPaths.resolve().ensure()
        setup_logging(self.paths.log_file)

        # Adapters
        self.settings = JsonSettings(self.paths.settings_file)
        self.store = SqliteStore(self.paths.db_file)  # MemoryStore + ConversationStore
        self.repo = GitRepo()
        self.clock = SystemClock()
        self.bus = SignalBus()
        def _key() -> str | None:
            return self.settings.get("claude_api_key")

        def _oauth() -> str | None:
            return self.settings.get("claude_code_oauth_token")

        # Model tiering (READ_ME/BRAIN.md): everyday conversation runs on a fast model (Sonnet —
        # thinking off, low effort) for snappy routing/confirming/chat. GROWTH reasoning — the deep
        # reasoner (think_harder) and the nightly Evolve loop, where HELIX rewrites itself — runs on
        # the STRONGEST model available. The resolver queries the live model list and auto-upscales to
        # a future Fable 6 / higher Opus; the pinned floor is Fable 5. Builds use the most capable coder.
        self.growth_model = GrowthModelResolver(_key, clock=self.clock)
        api_chat = AnthropicChat(
            _key, model="claude-sonnet-4-6", web_search=True, thinking="disabled", effort="low",
        )
        deep_chat = AnthropicChat(
            _key, model=self.growth_model.resolve(), web_search=True, thinking="adaptive",
            effort="high",
        )
        # THE SUBSCRIPTION BRAIN: when a Claude Code token is connected (Settings → `claude
        # setup-token`), conversation/agents/distillers run on the user's Claude PLAN — the same
        # usage pool as Claude Desktop — through the official Agent SDK + the local claude.exe,
        # instead of pay-per-token API billing. Tools are late-bound below (registry ctor cycle);
        # without a token everything stays on the API key exactly as before.
        # The subscription brain's claude.exe runs in a NEUTRAL, empty scratch dir OUTSIDE the data tree
        # (secrets/db/builds) and outside the repo — a cwd on top of secrets is needless blast radius (a
        # relative read, an up-walked CLAUDE.md); an isolated empty dir has nothing to find.
        _sub_workdir = Path(tempfile.gettempdir()) / "helix-subscription-cwd"
        try:
            _sub_workdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _sub_workdir = self.paths.data  # last resort — still better than failing to start
        self.subscription = SubscriptionBrain(_oauth, CONSOLE_SYSTEM, workdir=str(_sub_workdir))
        # Say plainly, once per launch, which rail the brain bills to — the subscription (flat, the
        # enterprise plan) or the metered API key. Silent metering is how a surprise bill happens.
        _log = get_logger("container")

        def _announce_rail() -> None:
            """Say which rail actually works — off the startup path. Deciding this means probing that a
            claude.exe will LAUNCH (a saved token proves nothing about the CLI), which costs a
            subprocess, so it must never sit between launch and the first frame. Running it here also
            warms the resolver cache, so the first real turn doesn't pay for the probe."""
            try:
                reason = self.subscription.why_inactive()
                if reason is None:
                    _log.info("brain: SUBSCRIPTION rail live — all HELIX reasoning bills to the plan")
                elif (_key() or "").strip():
                    _log.warning("brain: subscription rail unavailable (%s) — running on the METERED "
                                 "API key", reason)
                else:
                    _log.error("brain: NO usable Claude rail — %s", reason)
            except Exception:  # noqa: BLE001 — a diagnostic must never take the app down
                _log.warning("brain: could not determine which Claude rail is live", exc_info=True)

        threading.Thread(target=_announce_rail, daemon=True, name="helix-rail-check").start()
        # Plain no-tool chat (the orb's own turns, via ConversationService): subscription first.
        self.chat = PreferredChat(self.subscription, api_chat)
        # THE UNATTENDED CHAT — the same rail-preferring chat with the API rail's server-side
        # web_search/web_fetch shed (PreferredChat.without_web forwards the shed to its API leg; the
        # subscription leg has always been fenced by run_hermetic's web=False default).
        #
        # Everything wired to THIS thinks with nobody watching, over content HELIX did not write: the
        # profile/lessons/long-term-memory distillers are fed the raw transcript, which contains
        # whatever an email, a Slack thread or a SAM.gov notice dragged in; the voice-identity notes
        # distill spoken answers; Evolve reads the day's lessons and the log tail. A model-authored
        # search or fetch from any of them is an outbound channel that walks straight around
        # call_api's host allowlist, redirect refusal and secret scrubbing — with no human at the orb
        # to notice. ConversationService keeps the WEB-ENABLED self.chat because it sheds per turn
        # itself (a human at the orb may search; its agent/watcher turns may not); nothing else here
        # has that distinction to make, so it takes the fenced twin once, at wiring time.
        unattended_chat = self.chat.without_web()
        # The GROWTH chat: plain (no-tool) reasoning pinned to the strongest available model + high
        # effort, subscription-first like self.chat. Evolve's nightly self-improvement pass runs on
        # this so HELIX always grows on its best brain (Fable 5 → a future Fable 6, resolved live).
        # Fenced like the distillers: Evolve runs at night, unattended, on mined text — and it drafts
        # a change to HELIX's OWN code, so it is the last place to hand the model a free egress path.
        # (The deep reasoner keeps its web: think_harder is a human ASKING for a search, and it takes
        # the bare `deep_chat` below, not this.)
        growth_chat = PreferredChat(
            self.subscription, deep_chat, model=self.growth_model.resolve(), effort="high",
        ).without_web()

        def _deep_think(question: str, on_progress=None, cancel=None) -> str:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return ""  # the user already stopped — don't fire the (expensive) Opus escalation
            if self.subscription.active():
                try:
                    return self.subscription.run_hermetic(
                        f"{DEEP_THINK_SYSTEM}\n\n---\n\n{question}",
                        model=self.growth_model.resolve(), effort="high",
                        on_progress=on_progress, cancel=cancel, web=True,  # a user-asked reasoner may search
                    ) or "I couldn't reason that through just now — try rephrasing?"
                except Exception:  # noqa: BLE001 — fall back to the API escalation below
                    pass
            # The subscription attempt may have run for a while; if the user stopped in the meantime,
            # don't now fire the priciest call on the API meter.
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return ""
            reply = _deep_think_on_api(deep_chat, self.subscription, question)
            # Meter the Opus escalation like the main loop does — it's the most expensive call path, and
            # was previously invisible to the usage ledger.
            u = reply.usage
            self.store.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)
            return reply.text or "I couldn't reason that through just now — try rephrasing?"

        coder_chat = AnthropicChat(_key, max_tokens=8000)  # roomier for code generation (Opus default)
        # Prefer the Claude Code CLI (most capable); fall back to the API coder (key-only, no CLI).
        self.coder = FallbackCoder(ClaudeCodeCli(_key, _oauth), ApiCoder(coder_chat, _key))
        # THE GROWTH CODER: when HELIX edits its OWN code (self-improvement), it drafts on the growth
        # model by DEFAULT (Fable 5 today, auto-upscaling) — but Evolve's proposal can size it PER TASK
        # via the EFFORT tier, dropping to the Opus 4.8 work floor for a small mechanical change or
        # holding at Fable 5 for a deep one (see GrowthModelResolver.work_model). Same CLI/subscription
        # path (the OAuth token is preferred over the API key inside ClaudeCodeCli). App and task builds
        # keep using self.coder; self-dev AND holograms reach for the top brain — the Forge routes MODEL
        # builds here (model_coder below) because CAD authoring is the hardest coding HELIX commissions.
        _growth_model = self.growth_model.resolve()
        growth_coder_chat = AnthropicChat(_key, model=_growth_model, max_tokens=8000)
        self.growth_coder = FallbackCoder(
            ClaudeCodeCli(_key, _oauth, model=_growth_model), ApiCoder(growth_coder_chat, _key)
        )

        # Services
        self.builds = BuildService(self.paths.builds, self.repo, self.clock)
        guard_files = [self.paths.settings_file]  # reverted if a coder writes into them
        # The secrets store lives OUTSIDE the guarded settings file (the build guard byte-reverts
        # settings mid-build; a key pasted while a build runs must survive). Constructed early so every
        # engine-key getter below can prefer it — the V3 just-in-time connect panel writes here.
        self.secrets = JsonSettings(self.paths.data / "helix_secrets.json")
        # The hologram baker compiles a built model.scad through the CadEngine (below) into a mesh + the
        # technical-illustration viewer, in-process. If a Tripo key is present (secrets, settings, or
        # env), it also gets the neural backend — the demoted REFERENCE path ("show me what a real X
        # looks like"), never the design itself. Opt-in: no key → the reference is simply declined.
        def _tripo_key() -> str | None:
            # Secrets first (the JIT connect panel, guard-safe), then Settings (legacy), then the env var.
            return (
                (self.secrets.get("TRIPO_API_KEY") or self.settings.get("tripo_api_key")
                 or os.environ.get("TRIPO_API_KEY") or "").strip()
                or None
            )

        def _neural(prompt, image):
            # The backend is ALWAYS wired and the key is read PER build — so connecting Tripo (the
            # JIT panel writes the secrets store; a legacy Settings key or env var still counts)
            # takes effect on the next hologram with no restart. Detail is likewise live.
            from helix.adapters.tripo3d import Tripo3D, TripoError
            if not _tripo_key():
                raise TripoError(
                    "High-detail holograms need Tripo — just ask HELIX to connect Tripo and a "
                    "secure key panel opens."
                )
            high = (self.settings.get("model_detail") or "balanced").lower() == "high"
            return Tripo3D(
                _tripo_key,
                face_limit=0 if high else 100000,
                texture_quality="detailed" if high else "standard",
            ).generate(prompt, image)

        # Environment/scene path (Blockade Labs Skybox) — a whole 360° PLACE ("a backyard", "a forest
        # clearing") the user looks around inside, where Tripo makes single objects. Key read per build,
        # like Tripo; no key → the baker shows an honest banner and routes objects to Tripo/parametric.
        def _blockade_key() -> str | None:
            return (
                (self.secrets.get("BLOCKADE_API_KEY") or self.settings.get("blockade_api_key")
                 or os.environ.get("BLOCKADE_API_KEY") or "").strip()
                or None
            )

        def _skybox(prompt):
            from helix.adapters.blockade_skybox import BlockadeSkybox
            return BlockadeSkybox(
                _blockade_key, style_provider=lambda: self.settings.get("skybox_style_id")
            ).generate(prompt)

        # THE HOLOGRAM ENGINE: a hologram is a build123d program (model.py) the coder writes and HELIX
        # compiles, and this is the ONE CadEngine instance — the B-rep kernel behind a worker
        # subprocess (helix.cad.runner), so the heavy OCCT import never touches the app process. One
        # worker run writes the whole artifact set (STL + STEP + 3MF + preview + meta). Constructed
        # once and shared by the baker, which compiles with it on the build worker, and by the tool
        # registry, which pre-flights build_3d_model with it and offers the pip install just in time —
        # two instances would let the registry's "installed" and the baker's "missing" disagree.
        # Importing the adapter costs nothing heavy; nothing is spawned until a hologram is built.
        cad = Build123dCad(app_root=self.paths.root)
        # The critic looks at the rendered preview on the FENCED rail-preferring chat — the same
        # unattended twin the distillers use, so a subscription-only machine gets the look too (see
        # make_hologram_critic). It abstains only when NO rail can serve: no live subscription and no key.
        critic = make_hologram_critic(
            unattended_chat,
            lambda: self.subscription.active() or bool((_key() or "").strip()),
            self.store.record_usage,
        )

        # neural_available reflects a LIVE Tripo key (the backend is always wired), so the reference
        # path keys off the real thing, not merely "is a backend object present".
        def _build_baker():
            from helix.services.model_baker import ModelBaker
            # The viewer's three.js is the vendored r128 UMD build under helix/ui/assets (the same file
            # the orb's shader page reads; build.py ships that folder with --add-data). Resolved from
            # the package path — the one place that is right both in dev and frozen — and handed over
            # as a plain Path because a service must not import ui. None if it is somehow missing: the
            # baker then says so in the page instead of rendering nothing.
            import helix.ui
            three_js = Path(helix.ui.__file__).resolve().parent / "assets" / "three.min.js"
            return ModelBaker(
                cad=cad, three_js=three_js if three_js.is_file() else None, critic=critic,
                neural_backend=_neural, neural_available=lambda: bool(_tripo_key()),
                skybox_backend=_skybox, skybox_available=lambda: bool(_blockade_key()),
            )

        self.model_baker = _LazyBaker(_build_baker)
        self.cad = cad
        self.forge = ForgeService(
            self.builds, self.coder, self.bus, self.repo, self.paths.root, guard_files,
            model_baker=self.model_baker, data_dir=self.paths.data,
            model_coder=self.growth_coder,  # holograms draft on the growth model (Fable 5 today)
        )
        # Builds run as background jobs so the orb keeps talking while it works — a small pool runs a few
        # at once (the Forge's escape guard skips all build workspaces, so concurrent builds don't trip
        # each other; same-name builds still serialize so two edits can't clobber one workspace).
        self.build_queue = BuildQueue(self.forge, self.bus, max_workers=2)
        # Self-dev worktrees live OUTSIDE the app tree (a temp dir) so a concurrent background build's
        # escape-scan never mistakes an in-progress self-change draft for an escaped write.
        # THE SOURCE ROOT (READ_ME/DREAM.md §3): in a FROZEN build paths.root is dist/HELIX — the
        # install folder next to the exe, no git repository at all — so every self-change used to die
        # at its first git call. The gate now works on the SOURCE repository the build came from
        # (paths.source_root: the setting, else the build stamp; None when neither is usable, in which
        # case root is kept and the dream session says so instead of pretending). dev_python is the
        # interpreter that compiles, tests and rebuilds — sys.executable in dev, HELIX.exe never.
        self.selfdev = SelfDevService(
            self.growth_coder, self.repo, self.settings, self.clock,
            self.paths.source_root or self.paths.root,
            worktrees_dir=Path(tempfile.gettempdir()) / "helix-worktrees", guard_files=guard_files,
            data_dir=self.paths.data, python=self.paths.dev_python,
        )
        # Background lane so drafting a self-change doesn't freeze the orb.
        self.selfdev_lane = SelfDevLane(self.selfdev, self.bus)
        # Connections: the user's saved API keys for builds that need them. (self.secrets — the dedicated
        # guard-safe file — was constructed early, before the model baker's key getters.)
        # HELIX-managed keys a built app can reuse without the user re-pasting them: the Claude key
        # powers any AI feature (so builds default to Anthropic, never OpenAI); the engine keys
        # resolve secrets-first (the V3 JIT panel writes there) with legacy Settings + env fallback.
        _managed_keys = {
            "ANTHROPIC_API_KEY": lambda: (self.settings.get("claude_api_key") or "").strip(),
            "CLAUDE_API_KEY": lambda: (self.settings.get("claude_api_key") or "").strip(),
            "TRIPO_API_KEY": lambda: (_tripo_key() or ""),
            "VOYAGE_API_KEY": lambda: (_voyage_key() or ""),
            "BLOCKADE_API_KEY": lambda: (_blockade_key() or ""),
        }
        self.connections = ConnectionsService(self.builds, self.secrets, managed=_managed_keys)
        # Gmail: read-only inbox access (an address + a Google App Password in the secrets store). Like
        # Connections, the credential is local-only; HELIX only ever READS the inbox.
        self.gmail = GmailService(self.secrets)
        # Calendar: read-only iCal access (the private feed URL is the secret, same posture as Gmail).
        self.calendar = CalendarService(self.secrets, clock=self.clock)
        # Reminders/timers the orb keeps itself ("set a 10-minute timer") — spoken by the heartbeat.
        # A DEDICATED file (like secrets), NOT the guarded settings file: a reminder set WHILE a build
        # runs must survive the build guard's byte-revert of settings, or the timer silently vanishes.
        self.reminders = ReminderService(
            JsonSettings(self.paths.data / "helix_reminders.json"), self.clock
        )
        # Optional SEMANTIC knowledge search: enabled only when a Voyage key is set (Settings or the
        # VOYAGE_API_KEY env var). The key is read PER search, so adding it takes effect with no restart;
        # without it, knowledge search is keyword-only. Failures fall back to keyword automatically.
        def _voyage_key() -> str | None:
            return (
                (self.secrets.get("VOYAGE_API_KEY") or self.settings.get("voyage_api_key")
                 or os.environ.get("VOYAGE_API_KEY") or "").strip()
                or None
            )

        self.embedder = VoyageEmbedder(_voyage_key)
        # Knowledge: the user's own searchable notes/documents. A workspace build like any other (so it
        # inherits git, the rebuild-surviving guard skip, and voice rename/delete), but ingested directly
        # here — never by the coder — so there is no build sandbox in the loop. Built before tasks so a
        # finishing task can harvest its output into a knowledge base.
        self.knowledge = KnowledgeService(
            self.builds, self.repo, self.clock, bus=self.bus, embedder=self.embedder
        )
        self.tasks = TaskService(self.builds, connections=self.connections, knowledge=self.knowledge)
        # Servers a previous HELIX life launched and never stopped (a quit, a crash, a rebuild) hold
        # their build folders open — the "can't remove the music player" trap. Sweep them by pid file.
        try:
            _orphans = self.tasks.reap_orphans()
            if _orphans:
                _LOG.info("stopped orphaned app servers left by an earlier HELIX: %s", ", ".join(_orphans))
        except Exception:  # noqa: BLE001
            _LOG.warning("orphan sweep failed", exc_info=True)
        # Files: the user's own disk. Reads are always on; writes exist only while the Settings
        # toggle (file_write_access) is on — read live per turn, so flipping it needs no restart.
        # HELIX's program folder and data stores stay off-limits in every mode.
        self.files = FilesService(self.settings, root=self.paths.root, data=self.paths.data)
        # Long-term memory (durable per-speaker FACTS about the user) + Location (named places, so local
        # questions ground via web search). Both mirror the profile/lessons pattern and inject a context
        # block each turn; dedicated JSON files, guard-safe like reminders/agents.
        self.user_memory = MemoryService(
            unattended_chat, self.store, JsonSettings(self.paths.data / "helix_memory.json"),
            self.clock,
        )
        self.location = LocationService(JsonSettings(self.paths.data / "helix_locations.json"))
        # Recommend: a local usage ledger (opens/runs per build) that resurfaces the user's most-used and
        # neglected builds as a "Suggested" strip in the Menu. Privacy-local; a dedicated JSON file.
        self.recommend = RecommendService(
            JsonSettings(self.paths.data / "helix_usage.json"), self.clock
        )
        # Anticipate: quiet, deterministic nudges (a neglected build, a drafted change) surfaced as one
        # dismissible chip over the orb by the heartbeat. No LLM, no network — a calm ambient presence.
        self.suggestions = SuggestionService(
            recommend=self.recommend, builds=self.builds, selfdev=self.selfdev
        )
        # Desktop control (V3): open installed programs, media keys, one machine-status line. All
        # user-driven — the BUILD_TOOLS fence keeps open_program/media_control off autonomous runs.
        self.desktop = DesktopService()
        # Shopping (V3): the Amazon faculty. HELIX searches amazon.com with its OWN reads (live
        # prices, stars, ASINs), verifies every id against the listing before staging, keeps the
        # staged list on disk, and on the user's go drives its OWN Chrome window (a dedicated profile
        # under data/, never the user's everyday browser) to press Add-to-Cart per item and read the
        # cart back — never purchased. Parts lists (a project's BOM) and the handoff ledger live in
        # their own store. User-driven only: the BUILD_TOOLS fence keeps every mutation and window
        # launch off autonomous runs. All three stores are on the guard skip list (config).
        from helix.adapters.amazon_web import AmazonWeb
        from helix.adapters.chrome_cart import ChromeCart

        def _stamp() -> str:
            return self.clock.now().isoformat(timespec="minutes")

        self.parts = PartsService(JsonSettings(self.paths.data / "helix_parts.json"), clock=_stamp)
        self.shopping = ShoppingService(
            web=AmazonWeb(), driver=ChromeCart(self.paths.data / "amazon-chrome"),
            store=JsonSettings(self.paths.data / "helix_cart.json"), parts=self.parts, bus=self.bus,
            clock=_stamp,
        )
        # THE MAKER FLOW (READ_ME/MAKER_FLOW.md): the component library in service (suggest the parts
        # of a device; resolve a parts-list row to a library part, a LiPo code, or a measured size)
        # and the maker brain that designs an enclosure from the list DETERMINISTICALLY — no coder
        # run: the generator emits model.py + assets/layout.json, the same baker/engine/repo the
        # Forge uses bake and version it, and the shell hears BuildCreated/BuildIterated like any
        # hologram. It also projects a design over the camera with ghost pockets (check_fit), parks
        # the ruler on the panel (camera_measure), and writes the print sheet print_hologram and
        # the studio show. Pure Python until a design is asked for; the kernel runs in its worker.
        self.components = ComponentService(self.parts)
        self.maker = MakerService(
            self.components, self.parts, self.builds, self.model_baker, self.repo, self.bus, cad=cad,
        )
        # The Bambu printer's LAN details, read PER CALL (secrets → legacy settings → env) like the
        # Tripo key — so connecting the printer mid-conversation works on the very next tool call.
        def _bambu_key(key: str) -> str | None:
            return (
                (self.secrets.get(key) or self.settings.get(key.lower())
                 or os.environ.get(key) or "").strip() or None
            )

        self.tools = ToolRegistry(
            self.forge, self.builds, self.selfdev, deep_think=_deep_think, queue=self.build_queue,
            tasks=self.tasks, bus=self.bus, selfdev_lane=self.selfdev_lane, connections=self.connections,
            knowledge=self.knowledge, gmail=self.gmail, reminders=self.reminders, calendar=self.calendar,
            files=self.files, user_memory=self.user_memory, location=self.location,
            desktop=self.desktop, shopping=self.shopping, parts=self.parts, cad=cad, bambu=_bambu_key,
            maker=self.maker,
        )
        # The orb quietly learns who the user is: a background distiller (same fast chat model) keeps a
        # compact profile in the DB, injected into each Console turn like the time anchor. No knobs.
        self.profile = ProfileService(unattended_chat, self.store, self.clock)
        # The learning flywheel: standing behavioral preferences distilled from the user's own
        # corrections/confirmations ("keep it shorter", "yes, that's right"), injected each turn like the
        # profile. A DEDICATED JSON file (guard-safe like reminders/agents), so a correction made while a
        # build runs isn't byte-reverted with the settings file.
        self.lessons = LessonsService(
            unattended_chat, self.store, JsonSettings(self.paths.data / "helix_lessons.json"),
            self.clock,
        )
        # Reflexes: the growth layer's consolidation store (READ_ME/BRAIN.md). A sleep phrase the
        # cortex judged genuine becomes a fast brainstem reflex next time — dedicated guard-safe JSON
        # (like reminders/agents), so a reflex learned mid-build isn't byte-reverted with settings.
        self.reflexes = ReflexService(JsonSettings(self.paths.data / "helix_reflexes.json"))
        # Evolve: the overnight self-improvement pass (V3). Mines the day's lessons + the log tail and
        # DRAFTS one small change through the same selfdev lane improve_helix uses — approval-gated,
        # never self-applying. The shell heartbeat calls tick(); the Settings toggle governs it.
        self.evolve = EvolveService(
            growth_chat, self.lessons, self.selfdev_lane, self.selfdev, self.settings, self.clock,
            growth_model=self.growth_model,  # maps the proposal's EFFORT tier → coder model
            data_dir=self.paths.data,        # the backlog + journal live beside the other data files
        )
        self.tools.attach_evolve(self.evolve)  # late-bind: the registry is built before Evolve is
        # THE DREAM SESSION (READ_ME/DREAM.md): the nightly long-form of Evolve — a user-set window in
        # which HELIX plans a whole night on the growth model (Fable), drafts through the same lane,
        # merges only what the FULL suite proves green (when the user allows), and — frozen — rebuilds
        # and relaunches itself at dawn through the Rebuilder. The shell beats its heart (tick), hands
        # it the user's presence (dream.activity = seconds since the last turn) and tells the morning
        # report; Evolve defers to it for any night it covers, so the two never both draft.
        self.rebuilder = Rebuilder(self.paths, self.settings, clock=self.clock)
        self.dream = DreamService(
            growth_chat, self.selfdev_lane, self.selfdev, self.evolve, self.settings, self.clock,
            self.bus, paths=self.paths, suite_runner=self.selfdev.verify, rebuilder=self.rebuilder,
            growth_model=self.growth_model,
        )
        self.evolve.set_dream(self.dream)
        _attach_dream = getattr(self.tools, "attach_dream", None)  # the registry's dream tools
        if callable(_attach_dream):
            _attach_dream(self.dream)
        # The research faculty (READ_ME/DREAM_MIND.md §10): HELIX's own reads of documentation
        # hosts on an allowlist, and the record of what it verified there. Late-bound like Evolve.
        from helix.services.research import ResearchService
        from helix.services.verified import VerifiedStore

        self.verified = VerifiedStore(JsonSettings(self.paths.data / "helix_verified.json"), self.clock)
        self.research = ResearchService(settings=self.settings)
        _attach_research = getattr(self.tools, "attach_research", None)
        if callable(_attach_research):
            _attach_research(self.research, self.verified)
        self.subscription._tools = self.tools  # late-bind (tools → services ctor cycle, like agents)
        self.conversation = ConversationService(
            self.chat, self.tools, self.store, self.store, self.clock, CONSOLE_SYSTEM,
            knowledge=self.knowledge, profile=self.profile, subscription=self.subscription,
            lessons=self.lessons, user_memory=self.user_memory, location=self.location,
            # AUTO-DEEP: hard-looking turns quietly escalate to the growth model (Fable 5) on the
            # subscription rail — the automatic sibling of think_harder (settings: auto_deep_turns).
            growth_model=self.growth_model, settings=self.settings,
        )
        # Agents persist in a DEDICATED file (not the guarded settings file): scheduled agents write
        # last_run mid-build via the heartbeat, and the orb can create/pause an agent while a build runs
        # — both would be byte-reverted by the Forge guard if they lived in settings (silent agent loss
        # + duplicate scheduled fires). One-time migration lifts any agents from the old settings key.
        self.agent_store = JsonSettings(self.paths.data / "helix_agents.json")
        _migrate_agents(self.settings, self.agent_store)
        self.agents = AgentService(self.agent_store, self.conversation, bus=self.bus, clock=self.clock)
        self.tools.bind_agents(self.agents)  # late-bind: agents → conversation → tools, so it can't be ctor-passed
        _seed_watchers(self.agent_store, self.agents)  # the sentinel: default watchers, once per version
        # Which scheduled agents are due — the shell's heartbeat (main_window) asks this every tick.
        self.scheduler = AgentScheduler(self.agents, self.clock)
        # Workflows chain agents into ordered pipelines. A dedicated JSON store, and — because Workflow
        # mirrors Agent's shape — the SAME AgentScheduler drives their schedules from the heartbeat.
        self.workflows = WorkflowService(
            JsonSettings(self.paths.data / "helix_workflows.json"), self.agents, bus=self.bus,
            clock=self.clock,
        )
        self.tools.bind_workflows(self.workflows)
        self.workflow_scheduler = AgentScheduler(self.workflows, self.clock)
        # Remote companion (check in / trigger from a phone). POLICY only here; the listener is started
        # by the main window and is OFF until the user enables it in Settings. Read/ask + run-agent only
        # (allow_builds=False, persist=False) — it inherits the BUILD_TOOLS fence, so nothing can build,
        # delete, self-change, or write to disk remotely.
        self.remote = RemoteService(
            self.settings, self.secrets, conversation=self.conversation, agents=self.agents,
            queue=self.build_queue,
        )
        self.restart = Restarter(self.paths.root / "main.py", self.paths.root).restart

        # Voice (optional; both degrade to text-only / silent if unavailable). TTS uses the chosen
        # neural accent (edge-tts), falling back to the local OS voice when offline/unavailable.
        self.speech_in = WhisperSpeechIn(  # use whatever prewarm loaded (preferred/fallback)
            active_model(), wake_word=lambda: self.settings.get("wake_word") or ""  # ui.voice.WAKE_WORD_SETTING
        )
        self.speech_out = EdgeSpeechOut(
            lambda: self.settings.get("tts_voice"),
            lambda: self.settings.get("tts_rate"),
            fallback=OsSpeechOut(),
        )
        # Voice identity: registered voice profiles + the per-utterance speaker decision. A DEDICATED
        # file (like agents/reminders): profiles sharpen passively while builds may be running, and the
        # Forge guard byte-reverts helix_settings.json. Embeddings only — never audio.
        self.voice_id = VoiceIdService(self.paths.data / "helix_voices.json", chat=unattended_chat)
