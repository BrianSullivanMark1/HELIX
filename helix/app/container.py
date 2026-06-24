"""Container — the ONE place adapters are constructed and wired into services.

Nothing else in the codebase builds an adapter. Swapping an implementation (e.g. the coder) is a
one-line change here. This module is a PROTECTED_PATH: the self-coder may never edit the wiring.
"""
from __future__ import annotations

from helix.adapters.anthropic_chat import AnthropicChat
from helix.adapters.api_coder import ApiCoder
from helix.adapters.claude_code_cli import ClaudeCodeCli
from helix.adapters.coder_select import FallbackCoder
from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.adapters.signal_bus import SignalBus
from helix.adapters.speech import OsSpeechOut, WhisperSpeechIn
from helix.adapters.sqlite_store import SqliteStore
from helix.adapters.system_clock import SystemClock
from helix.config import AppPaths
from helix.logging_setup import setup_logging
from helix.services.agents import AgentService
from helix.services.archive import ArchiveService
from helix.services.builds import BuildService
from helix.services.conversation import ConversationService
from helix.services.forge import ForgeService
from helix.services.tasks import TaskService
from helix.services.prompts import CONSOLE_SYSTEM
from helix.services.selfdev import SelfDevService
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

        self.chat = AnthropicChat(_key)
        coder_chat = AnthropicChat(_key, max_tokens=8000)  # roomier for code generation
        # Prefer the Claude Code CLI (most capable); fall back to the API coder (key-only, no CLI).
        self.coder = FallbackCoder(ClaudeCodeCli(_key, _oauth), ApiCoder(coder_chat, _key))

        # Services
        self.builds = BuildService(self.paths.builds, self.repo, self.clock)
        guard_files = [self.paths.settings_file]  # reverted if a coder writes into them
        self.forge = ForgeService(
            self.builds, self.coder, self.bus, self.repo, self.paths.root, guard_files
        )
        self.selfdev = SelfDevService(
            self.coder, self.repo, self.settings, self.clock, self.paths.root,
            worktrees_dir=self.paths.data / "worktrees", guard_files=guard_files,
            data_dir=self.paths.data,
        )
        self.tools = ToolRegistry(self.forge, self.builds, self.selfdev)
        self.conversation = ConversationService(
            self.chat, self.tools, self.store, self.store, self.clock, CONSOLE_SYSTEM
        )
        self.archive = ArchiveService(self.repo, self.store, self.paths.root)
        self.agents = AgentService(self.settings, self.conversation)
        self.tasks = TaskService(self.builds)
        self.restart = Restarter(self.paths.root / "main.py", self.paths.root).restart

        # Voice (optional; both degrade to text-only / silent if unavailable)
        self.speech_in = WhisperSpeechIn()
        self.speech_out = OsSpeechOut()
