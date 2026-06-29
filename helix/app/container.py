"""Container — the ONE place adapters are constructed and wired into services.

Nothing else in the codebase builds an adapter. Swapping an implementation (e.g. the coder) is a
one-line change here. This module is a PROTECTED_PATH: the self-coder may never edit the wiring.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from helix.adapters.anthropic_chat import AnthropicChat
from helix.adapters.api_coder import ApiCoder
from helix.adapters.claude_code_cli import ClaudeCodeCli
from helix.adapters.coder_select import FallbackCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.adapters.signal_bus import SignalBus
from helix.adapters.speech import EdgeSpeechOut, OsSpeechOut, WhisperSpeechIn
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
from helix.services.connections import ConnectionsService
from helix.services.conversation import ConversationService
from helix.services.forge import ForgeService
from helix.services.gmail import GmailService
from helix.services.knowledge import KnowledgeService
from helix.services.model_baker import ModelBaker
from helix.services.tasks import TaskService
from helix.services.prompts import CONSOLE_SYSTEM, DEEP_THINK_SYSTEM
from helix.services.selfdev import SelfDevService
from helix.services.selfdev_lane import SelfDevLane
from helix.services.tools import ToolRegistry


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
        self.chat = AnthropicChat(
            _key, model="claude-sonnet-4-6", web_search=True, thinking="disabled", effort="low",
        )
        deep_chat = AnthropicChat(
            _key, model="claude-opus-4-8", web_search=True, thinking="adaptive", effort="high",
        )

        def _deep_think(question: str, on_progress=None, cancel=None) -> str:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                return ""  # the user already stopped — don't fire the (expensive) Opus escalation
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

        # neural_available reflects a LIVE Tripo key (the backend is always wired), so auto-routing and the
        # no-key preview banner key off the real thing, not merely "is a backend object present".
        self.model_baker = ModelBaker(neural_backend=_neural, neural_available=lambda: bool(_tripo_key()))
        self.forge = ForgeService(
            self.builds, self.coder, self.bus, self.repo, self.paths.root, guard_files,
            model_baker=self.model_baker,
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
        self.connections = ConnectionsService(self.builds, self.secrets)
        # Gmail: read-only inbox access (an address + a Google App Password in the secrets store). Like
        # Connections, the credential is local-only; HELIX only ever READS the inbox.
        self.gmail = GmailService(self.secrets)
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
        self.tools = ToolRegistry(
            self.forge, self.builds, self.selfdev, deep_think=_deep_think, queue=self.build_queue,
            tasks=self.tasks, bus=self.bus, selfdev_lane=self.selfdev_lane, connections=self.connections,
            knowledge=self.knowledge, gmail=self.gmail,
        )
        self.conversation = ConversationService(
            self.chat, self.tools, self.store, self.store, self.clock, CONSOLE_SYSTEM,
            knowledge=self.knowledge,
        )
        self.agents = AgentService(self.settings, self.conversation, bus=self.bus)
        self.tools.bind_agents(self.agents)  # late-bind: agents → conversation → tools, so it can't be ctor-passed
        self.restart = Restarter(self.paths.root / "main.py", self.paths.root).restart

        # Voice (optional; both degrade to text-only / silent if unavailable). TTS uses the chosen
        # neural accent (edge-tts), falling back to the local OS voice when offline/unavailable.
        self.speech_in = WhisperSpeechIn()
        self.speech_out = EdgeSpeechOut(
            lambda: self.settings.get("tts_voice"),
            lambda: self.settings.get("tts_rate"),
            fallback=OsSpeechOut(),
        )
