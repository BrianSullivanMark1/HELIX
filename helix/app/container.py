"""Container — the ONE place adapters are constructed and wired into services.

Nothing else in the codebase builds an adapter. Swapping an implementation (e.g. the coder) is a
one-line change here. This module is a PROTECTED_PATH: the self-coder may never edit the wiring.
"""
from __future__ import annotations

from helix.adapters.anthropic_chat import AnthropicChat
from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.placeholder_coder import PlaceholderCoder
from helix.adapters.signal_bus import SignalBus
from helix.adapters.sqlite_store import SqliteStore
from helix.adapters.system_clock import SystemClock
from helix.config import AppPaths
from helix.logging_setup import setup_logging
from helix.services.archive import ArchiveService
from helix.services.builds import BuildService
from helix.services.conversation import ConversationService
from helix.services.forge import ForgeService
from helix.services.prompts import CONSOLE_SYSTEM
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
        self.chat = AnthropicChat(lambda: self.settings.get("claude_api_key"))
        self.coder = PlaceholderCoder()  # TODO(phase 6): real Claude Code CLI + API coder

        # Services
        self.builds = BuildService(self.paths.builds, self.repo, self.clock)
        self.forge = ForgeService(self.builds, self.coder, self.bus)
        self.tools = ToolRegistry(self.forge, self.builds)
        self.conversation = ConversationService(
            self.chat, self.tools, self.store, self.store, self.clock, CONSOLE_SYSTEM
        )
        self.archive = ArchiveService(self.repo, self.store, self.paths.root)
