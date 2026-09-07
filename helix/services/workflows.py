"""WorkflowService — chain saved AGENTS into an ordered pipeline.

A workflow is a named, ordered list of agent names. Running it runs each step agent in turn, handing the
PRIOR step's result to the next as untrusted DATA (see AgentService.run's `context`), and returns one
combined report. Like an agent, every step is autonomous — read/think/search/report only, never build,
spend, self-modify, delete, or run (the BUILD_TOOLS denylist is inherited from the agent run path).

It deliberately mirrors AgentService's shape (name/enabled/schedule/last_run + list/mark_ran), so the
SAME AgentScheduler drives it from the heartbeat with no new scheduling machinery. Settings-backed in a
dedicated JSON file (guard-safe like agents/reminders).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from helix.domain.events import AgentsChanged
from helix.ports.coder import ProgressFn
from helix.ports.events import EventBus
from helix.ports.stores import SettingsStore
from helix.services.agents import AgentService
from helix.services.scheduler import agent_name, infer_schedule

_KEY = "workflows"


@dataclass
class Workflow:
    name: str
    steps: list = field(default_factory=list)  # ordered agent names
    enabled: bool = True
    schedule: dict | None = None
    last_run: str | None = None


class WorkflowService:
    def __init__(
        self, settings: SettingsStore, agents: AgentService, bus: EventBus | None = None, clock=None
    ) -> None:
        self._settings = settings
        self._agents = agents
        self._bus = bus
        self._clock = clock
        self._lock = threading.RLock()  # atomic RMW over the one settings key (heartbeat + orb both write)

    def _changed(self) -> None:
        if self._bus is not None:
            self._bus.publish(AgentsChanged())  # reuse the menu-refresh signal

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now().astimezone()

    def list(self) -> list[Workflow]:
        out: list[Workflow] = []
        for w in (self._settings.get(_KEY) or []):
            # A step is an agent NAME. Normalize on read so a step ever saved as a stringified Agent
            # ("Agent(name='X', goal=...)") heals to just "X" — otherwise its lookup misses every run.
            steps = [n for s in (w.get("steps") or []) if (n := agent_name(s))]
            out.append(Workflow(
                name=w.get("name", ""), steps=steps, enabled=w.get("enabled", True),
                schedule=w.get("schedule") or None, last_run=w.get("last_run") or None,
            ))
        return out

    def exists(self, name: str) -> bool:
        t = (name or "").strip().lower()
        return any(w.name.strip().lower() == t for w in self.list())

    def find(self, name: str) -> Workflow | None:
        t = (name or "").strip().lower()
        return next((w for w in self.list() if w.name.strip().lower() == t), None)

    def add(self, name: str, steps: list[str], schedule_hint: str | None = None) -> Workflow:
        """Save (or update) a workflow. Unknown step names are kept as-is (validated at run time); the
        schedule is inferred from the hint, mirroring AgentService."""
        # Store each step by the agent's plain name — an Agent object handed in becomes its `.name`,
        # never str(Agent(...)). (See agent_name; this is where the corrupted-name bug started.)
        steps = [n for s in (steps or []) if (n := agent_name(s))]
        schedule = infer_schedule(schedule_hint or "")
        now = self._now()
        wf = Workflow(name=name.strip(), steps=steps, schedule=schedule,
                      last_run=now.isoformat() if schedule else None)
        with self._lock:
            key = wf.name.strip().lower()
            prior = next((w for w in self.list() if w.name.strip().lower() == key), None)
            if prior is not None:
                wf.enabled = prior.enabled  # a retune never silently resumes a paused workflow
            self._save([w for w in self.list() if w.name.strip().lower() != key] + [wf])
        self._changed()
        return wf

    def remove(self, name: str) -> bool:
        with self._lock:
            before = self.list()
            after = [w for w in before if w.name.strip().lower() != (name or "").strip().lower()]
            if len(after) == len(before):
                return False
            self._save(after)
        self._changed()
        return True

    def rename(self, old: str, new: str) -> Workflow | None:
        new = (new or "").strip()
        if not new:
            return None
        with self._lock:
            wfs = self.list()
            target = next((w for w in wfs if w.name.strip().lower() == (old or "").strip().lower()), None)
            if target is None:
                return None
            if new.lower() != old.strip().lower() and any(w.name.strip().lower() == new.lower() for w in wfs):
                return None
            target.name = new
            self._save(wfs)
        self._changed()
        return target

    def set_enabled(self, name: str, on: bool) -> Workflow | None:
        with self._lock:
            wfs = self.list()
            target = next((w for w in wfs if w.name.strip().lower() == (name or "").strip().lower()), None)
            if target is None:
                return None
            target.enabled = bool(on)
            self._save(wfs)
        self._changed()
        return target

    def mark_ran(self, name: str, at: datetime) -> bool:
        """Stamp a run. Returns whether a matching workflow was found, so the scheduler can tell a
        stamp that never lands (a stale name) from a normal one and stop retrying it every tick."""
        with self._lock:  # the scheduler stamps this from the heartbeat — atomic vs a concurrent edit
            wfs = self.list()
            found = False
            for w in wfs:
                if w.name == name:
                    w.last_run = at.isoformat()
                    found = True
            if found:
                self._save(wfs)
            return found

    def run(self, name: str, *, on_progress: ProgressFn | None = None) -> str:
        """Run every step in order, feeding each step the prior step's result as untrusted data. Returns
        one combined report — the final step's output, or a note if the workflow is empty/misconfigured."""
        wf = self.find(name)
        if wf is None:
            return f"No workflow named '{name}'."
        if not wf.steps:
            return f"The workflow '{wf.name}' has no steps yet."
        prior = ""
        last = ""
        for i, step in enumerate(wf.steps, 1):
            if not self._agents.exists(step):
                return (f"Workflow '{wf.name}' step {i} points at an agent called '{step}' that doesn't "
                        "exist — check the step names.")
            if on_progress is not None:
                on_progress(f"Running step {i} of {len(wf.steps)}: {step}")
            out = self._agents.run(step, on_progress=on_progress, context=prior)
            prior = out or ""
            if out and out.strip():
                last = out.strip()
        return last or f"Workflow '{wf.name}' finished with nothing to report."

    def _save(self, wfs: list[Workflow]) -> None:
        self._settings.set(
            _KEY,
            [{"name": w.name, "steps": w.steps, "enabled": w.enabled,
              "schedule": w.schedule, "last_run": w.last_run} for w in wfs],
        )
