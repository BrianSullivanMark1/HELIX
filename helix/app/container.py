"""Container — the ONE place adapters are constructed and wired into services.

Nothing else in the codebase builds an adapter. Swapping an implementation (e.g. the coder) is a
one-line change here. This module is a PROTECTED_PATH: the self-coder may never edit the wiring.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from helix.adapters.agent_sdk_chat import PreferredChat, SubscriptionBrain
from helix.adapters.anthropic_chat import AnthropicChat
from helix.adapters.api_coder import ApiCoder
from helix.adapters.claude_code_cli import ClaudeCodeCli
from helix.adapters.coder_select import FallbackCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.adapters.signal_bus import SignalBus
from helix.adapters.speech import EdgeSpeechOut, OsSpeechOut, WhisperSpeechIn, active_model
from helix.adapters.sqlite_store import SqliteStore
from helix.adapters.system_clock import SystemClock
from helix.adapters.voyage_embed import VoyageEmbedder
from helix.config import AppPaths
from helix.domain.models import Role
from helix.logging_setup import setup_logging
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
from helix.services.knowledge import KnowledgeService
from helix.services.lessons import LessonsService
from helix.services.location import LocationService
from helix.services.memory import MemoryService
from helix.services.model_baker import ModelBaker
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
from helix.services.tools import ToolRegistry
from helix.services.voiceid import VoiceIdService


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
     "https://api.sam.gov/opportunities/v2/search?postedFrom=YESTERDAY&postedTo=TODAY&limit=10"
     "&title=manufacturing with MM/dd/yyyy dates from the current date in context. With a spare call, "
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
     "https://api.sam.gov/opportunities/v2/search?postedFrom=YESTERDAY&postedTo=TODAY&limit=10"
     "&title=manufacturing using MM/dd/yyyy dates from the current date in context, and batch one or "
     "two more title keywords in the same round: MES, compliance. Flag solicitations matching "
     "manufacturing software, MES, MRP, WMS, or compliance tooling — title, agency, and response "
     "deadline, one line each, three at most. If nothing new matches, or SAM.gov isn't connected, "
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

        # Model tiering: the conversation runs on Sonnet (fast — thinking off, low effort) for snappy
        # routing/confirming/chat; a hard question escalates to Opus with deep thinking via think_harder;
        # builds use the most capable coder. All can research the web.
        api_chat = AnthropicChat(
            _key, model="claude-sonnet-4-6", web_search=True, thinking="disabled", effort="low",
        )
        deep_chat = AnthropicChat(
            _key, model="claude-opus-4-8", web_search=True, thinking="adaptive", effort="high",
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
        # Plain no-tool chat (profile distiller, voice-identity notes, …): subscription first.
        self.chat = PreferredChat(self.subscription, api_chat)

        def _deep_think(question: str, on_progress=None, cancel=None) -> str:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return ""  # the user already stopped — don't fire the (expensive) Opus escalation
            if self.subscription.active():
                try:
                    return self.subscription.run_hermetic(
                        f"{DEEP_THINK_SYSTEM}\n\n---\n\n{question}",
                        model="claude-opus-4-8", effort="high",
                        on_progress=on_progress, cancel=cancel, web=True,  # a user-asked reasoner may search
                    ) or "I couldn't reason that through just now — try rephrasing?"
                except Exception:  # noqa: BLE001 — fall back to the API escalation below
                    pass
            # The subscription attempt may have run for a while; if the user stopped in the meantime,
            # don't now fire the priciest call on the API meter.
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return ""
            reply = deep_chat.chat([Turn(Role.USER, (Text(question),))], system=DEEP_THINK_SYSTEM)
            # Meter the Opus escalation like the main loop does — it's the most expensive call path, and
            # was previously invisible to the usage ledger.
            u = reply.usage
            self.store.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)
            return reply.text or "I couldn't reason that through just now — try rephrasing?"

        coder_chat = AnthropicChat(_key, max_tokens=8000)  # roomier for code generation (Opus default)
        # Prefer the Claude Code CLI (most capable); fall back to the API coder (key-only, no CLI).
        self.coder = FallbackCoder(ClaudeCodeCli(_key, _oauth), ApiCoder(coder_chat, _key))

        # Services
        self.builds = BuildService(self.paths.builds, self.repo, self.clock)
        guard_files = [self.paths.settings_file]  # reverted if a coder writes into them
        # The model baker turns a built model.json into a real polygon mesh (assets/model.glb) + viewer,
        # in-process. If a Tripo key is present (env var or settings), it also gets a neural backend —
        # the high-detail "turbo" path for organic/character subjects. Opt-in: no key → local-only.
        def _tripo_key() -> str | None:
            # Settings first (set in-app), then the env var. Either enables the high-detail neural path.
            return (
                (self.settings.get("tripo_api_key") or os.environ.get("TRIPO_API_KEY") or "").strip()
                or None
            )

        def _neural(prompt, image):
            # The backend is ALWAYS wired and the key is read PER build — so setting the key in Settings
            # (or fixing the env var) takes effect on the next model with no restart needed. Detail is
            # likewise live: "high" = native polygon count + detailed textures.
            from helix.adapters.tripo3d import Tripo3D, TripoError
            if not _tripo_key():
                raise TripoError("Add your Tripo API key in Settings to build high-detail 3D models.")
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
                (self.settings.get("blockade_api_key") or os.environ.get("BLOCKADE_API_KEY") or "").strip()
                or None
            )

        def _skybox(prompt):
            from helix.adapters.blockade_skybox import BlockadeSkybox
            return BlockadeSkybox(
                _blockade_key, style_provider=lambda: self.settings.get("skybox_style_id")
            ).generate(prompt)

        # neural_available reflects a LIVE Tripo key (the backend is always wired), so auto-routing and the
        # no-key preview banner key off the real thing, not merely "is a backend object present".
        self.model_baker = ModelBaker(
            neural_backend=_neural, neural_available=lambda: bool(_tripo_key()),
            skybox_backend=_skybox, skybox_available=lambda: bool(_blockade_key()),
        )
        self.forge = ForgeService(
            self.builds, self.coder, self.bus, self.repo, self.paths.root, guard_files,
            model_baker=self.model_baker, data_dir=self.paths.data,
        )
        # Builds run as background jobs so the orb keeps talking while it works — a small pool runs a few
        # at once (the Forge's escape guard skips all build workspaces, so concurrent builds don't trip
        # each other; same-name builds still serialize so two edits can't clobber one workspace).
        self.build_queue = BuildQueue(self.forge, self.bus, max_workers=2)
        # Self-dev worktrees live OUTSIDE the app tree (a temp dir) so a concurrent background build's
        # escape-scan never mistakes an in-progress self-change draft for an escaped write.
        self.selfdev = SelfDevService(
            self.coder, self.repo, self.settings, self.clock, self.paths.root,
            worktrees_dir=Path(tempfile.gettempdir()) / "helix-worktrees", guard_files=guard_files,
            data_dir=self.paths.data,
        )
        # Background lane so drafting a self-change doesn't freeze the orb.
        self.selfdev_lane = SelfDevLane(self.selfdev, self.bus)
        # Connections: the user's saved API keys for builds that need them. A DEDICATED secrets file (not
        # the settings file, so the build guard never byte-reverts it), kept on this machine only and never
        # written into a build's folder, git, or the browser.
        self.secrets = JsonSettings(self.paths.data / "helix_secrets.json")
        # HELIX-managed keys a built app can reuse without the user re-pasting them: the Claude key powers
        # any AI feature (so builds default to Anthropic, never OpenAI); Tripo/Voyage are here too. These
        # live in Settings (not the secrets store), so the connections layer resolves them via these getters.
        _managed_keys = {
            "ANTHROPIC_API_KEY": lambda: (self.settings.get("claude_api_key") or "").strip(),
            "CLAUDE_API_KEY": lambda: (self.settings.get("claude_api_key") or "").strip(),
            "TRIPO_API_KEY": lambda: (
                self.settings.get("tripo_api_key") or os.environ.get("TRIPO_API_KEY") or ""
            ).strip(),
            "VOYAGE_API_KEY": lambda: (
                self.settings.get("voyage_api_key") or os.environ.get("VOYAGE_API_KEY") or ""
            ).strip(),
            "BLOCKADE_API_KEY": lambda: (
                self.settings.get("blockade_api_key") or os.environ.get("BLOCKADE_API_KEY") or ""
            ).strip(),
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
                (self.settings.get("voyage_api_key") or os.environ.get("VOYAGE_API_KEY") or "").strip()
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
        # Files: the user's own disk. Reads are always on; writes exist only while the Settings
        # toggle (file_write_access) is on — read live per turn, so flipping it needs no restart.
        # HELIX's program folder and data stores stay off-limits in every mode.
        self.files = FilesService(self.settings, root=self.paths.root, data=self.paths.data)
        # Long-term memory (durable per-speaker FACTS about the user) + Location (named places, so local
        # questions ground via web search). Both mirror the profile/lessons pattern and inject a context
        # block each turn; dedicated JSON files, guard-safe like reminders/agents.
        self.user_memory = MemoryService(
            self.chat, self.store, JsonSettings(self.paths.data / "helix_memory.json"), self.clock
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
        self.tools = ToolRegistry(
            self.forge, self.builds, self.selfdev, deep_think=_deep_think, queue=self.build_queue,
            tasks=self.tasks, bus=self.bus, selfdev_lane=self.selfdev_lane, connections=self.connections,
            knowledge=self.knowledge, gmail=self.gmail, reminders=self.reminders, calendar=self.calendar,
            files=self.files, user_memory=self.user_memory, location=self.location,
        )
        # The orb quietly learns who the user is: a background distiller (same fast chat model) keeps a
        # compact profile in the DB, injected into each Console turn like the time anchor. No knobs.
        self.profile = ProfileService(self.chat, self.store, self.clock)
        # The learning flywheel: standing behavioral preferences distilled from the user's own
        # corrections/confirmations ("keep it shorter", "yes, that's right"), injected each turn like the
        # profile. A DEDICATED JSON file (guard-safe like reminders/agents), so a correction made while a
        # build runs isn't byte-reverted with the settings file.
        self.lessons = LessonsService(
            self.chat, self.store, JsonSettings(self.paths.data / "helix_lessons.json"), self.clock
        )
        self.subscription._tools = self.tools  # late-bind (tools → services ctor cycle, like agents)
        self.conversation = ConversationService(
            self.chat, self.tools, self.store, self.store, self.clock, CONSOLE_SYSTEM,
            knowledge=self.knowledge, profile=self.profile, subscription=self.subscription,
            lessons=self.lessons, user_memory=self.user_memory, location=self.location,
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
        self.voice_id = VoiceIdService(self.paths.data / "helix_voices.json", chat=self.chat)
