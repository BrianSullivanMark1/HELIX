"""AgentService — goal-driven automations. An agent is a saved goal HELIX can run on demand.

Running an agent drives the same model↔tools loop a typed request does, but with the build/spend/
self-mod/delete tools DENIED: an agent runs autonomously (no human in the loop), so it may read, think,
search, and report — but never build or change things on its own. Settings-backed (v1 trigger: manual).
"""
from __future__ import annotations

from dataclasses import dataclass

from helix.domain.events import AgentsChanged
from helix.ports.coder import ProgressFn
from helix.ports.events import EventBus
from helix.ports.stores import SettingsStore
from helix.services.conversation import ConversationService

_KEY = "agents"


@dataclass
class Agent:
    name: str
    goal: str
    enabled: bool = True


class AgentService:
    def __init__(
        self, settings: SettingsStore, conversation: ConversationService, bus: EventBus | None = None
    ) -> None:
        self._settings = settings
        self._conversation = conversation
        self._bus = bus

    def _changed(self) -> None:
        if self._bus is not None:
            self._bus.publish(AgentsChanged())

    def list(self) -> list[Agent]:
        return [
            Agent(name=a.get("name", ""), goal=a.get("goal", ""), enabled=a.get("enabled", True))
            for a in (self._settings.get(_KEY) or [])
        ]

    def exists(self, name: str) -> bool:
        target = (name or "").strip().lower()
        return any(a.name.strip().lower() == target for a in self.list())

    def add(self, name: str, goal: str) -> Agent:
        agent = Agent(name=name.strip(), goal=goal.strip())
        self._save([a for a in self.list() if a.name != agent.name] + [agent])
        self._changed()
        return agent

    def remove(self, name: str) -> None:
        self._save([a for a in self.list() if a.name != name])
        self._changed()

    def rename(self, old_name: str, new_name: str) -> Agent | None:
        """Rename an agent in place (its goal and position are kept). Returns the updated Agent, or
        None if the name is blank, the agent is missing, or another agent already has that name."""
        new_name = (new_name or "").strip()
        if not new_name:
            return None
        agents = self.list()
        target = next((a for a in agents if a.name == old_name), None)
        if target is None:
            return None
        if new_name != old_name and any(a.name == new_name for a in agents):
            return None  # name already taken
        renamed = Agent(name=new_name, goal=target.goal, enabled=target.enabled)
        self._save([renamed if a.name == old_name else a for a in agents])
        self._changed()
        return renamed

    def run(self, name: str, *, on_progress: ProgressFn | None = None) -> str:
        agent = next((a for a in self.list() if a.name == name), None)
        if agent is None:
            return f"No agent named '{name}'."
        # An agent runs autonomously (no human in the loop), so it may NOT build, spend, self-modify,
        # delete, rename, or run — only read, think, search, and report. It is also hermetic (persist=
        # False): its goal and report never enter the shared Console transcript.
        return self._conversation.run_turn(
            agent.goal, on_progress=on_progress, allow_builds=False, persist=False
        )

    def _save(self, agents: list[Agent]) -> None:
        self._settings.set(
            _KEY, [{"name": a.name, "goal": a.goal, "enabled": a.enabled} for a in agents]
        )
