"""ToolRegistry — the model's hands. Maps tool calls to service methods."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from helix.domain.models import BuildKind
from helix.ports.coder import ProgressFn
from helix.ports.llm import ToolSpec
from helix.services.builds import BuildService
from helix.services.forge import ForgeService
from helix.services.prompts import build_3d_model_prompt, build_task_prompt
from helix.services.selfdev import SelfDevService

if TYPE_CHECKING:  # AgentService -> ConversationService -> ToolRegistry would be a runtime import cycle
    from helix.services.agents import AgentService

# Escalation: hand a hard question to a deeper model and get back its spoken answer.
DeepThink = Callable[[str, ProgressFn | None], str]


class ToolRegistry:
    def __init__(
        self,
        forge: ForgeService,
        builds: BuildService,
        selfdev: SelfDevService | None = None,
        deep_think: DeepThink | None = None,
        agents: "AgentService | None" = None,
    ) -> None:
        self._forge = forge
        self._builds = builds
        self._selfdev = selfdev
        self._deep_think = deep_think
        self._agents = agents

    def bind_agents(self, agents: "AgentService") -> None:
        """Wire the agent store after construction (it depends on ConversationService, which depends on
        this registry — so it can't be passed in at build time). Enables create_agent."""
        self._agents = agents

    def specs(self) -> list[ToolSpec]:
        tools = [
            ToolSpec(
                name="build_app",
                description=(
                    "Build a new app from a plain-language description and add it to the user's menu. "
                    "Only call this AFTER the user has confirmed they want it built — building spends "
                    "Claude time."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "A short, human app name, e.g. 'Tip Calculator'.",
                        },
                        "request": {
                            "type": "string",
                            "description": "The full plain-language description of what to build.",
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="build_3d_model",
                description=(
                    "Conjure an interactive 3D model to SHOW the user what you're discussing — a device, "
                    "a part, a layout, a concept. It opens in their browser; they orbit and explore it. "
                    "Use this to visualize an idea when a picture communicates faster than words. To "
                    "CHANGE a model, call this again with the SAME name and the change (e.g. 'make it "
                    "taller', 'show the inside') and HELIX updates that model in place. Only call after "
                    "the user confirms — building spends Claude time, like build_app."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A short, human name for the model, e.g. 'Wall Camera Unit'. Reuse the "
                                "exact same name to modify an existing model."
                            ),
                        },
                        "request": {
                            "type": "string",
                            "description": (
                                "Plain-language description of what to visualize — or, when modifying an "
                                "existing model, the change to make."
                            ),
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="build_task",
                description=(
                    "Build a TASK — a small program that DOES A THING when run (a script, an automation, "
                    "a converter, a generator) instead of opening a screen. It runs in its own console "
                    "and lands in the Tasks tab; the user runs it on demand. Use this when they want an "
                    "action performed repeatably, not an interactive app. To CHANGE a task, call this "
                    "again with the SAME name and the change. Only call AFTER the user confirms — "
                    "building spends Claude time, like build_app."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "A short, human name for the task, e.g. 'Rename Downloads'.",
                        },
                        "request": {
                            "type": "string",
                            "description": (
                                "Plain-language description of what the task should do — or, when "
                                "modifying an existing task, the change to make."
                            ),
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="list_apps",
                description="List the apps the user has already built.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolSpec(
                name="delete_build",
                description=(
                    "Permanently delete something the user made — an app, a task, a 3D model, or an "
                    "agent — by its name. Use when the user clearly asks to remove or delete one of "
                    "their builds. This cannot be undone, so only call AFTER the user confirms."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the build or agent to delete.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
        ]
        if self._deep_think is not None:
            tools.append(
                ToolSpec(
                    name="think_harder",
                    description=(
                        "Escalate a genuinely hard question to a more capable, deeper-thinking model and "
                        "get back its answer. Use ONLY when the question needs real reasoning, comparison, "
                        "planning, or careful analysis — not for quick facts, chit-chat, or builds. Pass "
                        "the FULL question with any needed context; the deep model can't see this "
                        "conversation. Then relay its answer briefly in your own voice."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": (
                                    "The complete question to reason about, including all relevant "
                                    "context from the conversation."
                                ),
                            }
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._selfdev is not None:
            tools.append(
                ToolSpec(
                    name="improve_helix",
                    description=(
                        "Propose an improvement to HELIX's OWN code (how HELIX looks or works). This "
                        "DRAFTS the change on a branch for the user to review and approve in Archive — "
                        "it never applies on its own, and it can never remove HELIX's shell or safety "
                        "code. Only call after the user confirms, like build_app."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "request": {
                                "type": "string",
                                "description": "Plain-language description of the change to HELIX itself.",
                            }
                        },
                        "required": ["request"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._agents is not None:
            tools.append(
                ToolSpec(
                    name="create_agent",
                    description=(
                        "Save an AGENT — a standing goal the user can run on demand (a morning brief, a "
                        "recurring check, a routine). Use when the user describes a repeatable job they "
                        "want to keep and re-run later, not a one-off. Creating it is instant and costs "
                        "nothing (running it later does the work). Reuse the SAME name to update an "
                        "agent's goal. Confirm once first, like the other builds."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "A short name for the agent, e.g. 'Morning Brief'.",
                            },
                            "goal": {
                                "type": "string",
                                "description": "What HELIX should do each time the user runs this agent.",
                            },
                        },
                        "required": ["name", "goal"],
                        "additionalProperties": False,
                    },
                )
            )
        return tools

    def dispatch(self, name: str, args: dict, *, on_progress: ProgressFn | None = None, cancel=None) -> str:
        if name == "build_app":
            app = self._forge.build(
                args["name"], args["request"], on_progress=on_progress, cancel=cancel
            )
            return f"Built '{app.name}'. It's in the menu now."
        if name == "build_3d_model":
            app = self._forge.build(
                args["name"],
                args["request"],
                prompt=build_3d_model_prompt(args["name"], args["request"]),
                kind=BuildKind.MODEL,
                on_progress=on_progress,
                cancel=cancel,
            )
            return f"Modeled '{app.name}'. Open it from the Models tab to explore it in 3D."
        if name == "build_task":
            app = self._forge.build(
                args["name"],
                args["request"],
                prompt=build_task_prompt(args["name"], args["request"]),
                kind=BuildKind.TASK,
                on_progress=on_progress,
                cancel=cancel,
            )
            return f"Built the task '{app.name}'. Run it any time from the Tasks tab."
        if name == "think_harder" and self._deep_think is not None:
            return self._deep_think(args["question"], on_progress)
        if name == "list_apps":
            apps = self._builds.list()
            if not apps:
                return "No apps built yet."

            def clean(text: str) -> str:  # collapse the (untrusted) request to a one-line label
                return " ".join(text.split())[:140]

            return "\n".join(f"- {a.name}: {clean(a.request)}" for a in apps)
        if name == "improve_helix" and self._selfdev is not None:
            change = self._selfdev.propose(args["request"], on_progress=on_progress)
            return (
                f"Drafted a change ({change.branch}). Open Archive to review and approve it — "
                "it won't apply until you do."
            )
        if name == "create_agent" and self._agents is not None:
            agent = self._agents.add(args["name"], args["goal"])
            return f"Saved the agent '{agent.name}'. Run it any time from the Agents tab."
        if name == "delete_build":
            target = args["name"]
            if self._forge.remove_build(target):
                return f"Deleted '{target}'."
            if self._agents is not None:
                hit = next(
                    (a for a in self._agents.list()
                     if a.name.strip().lower() == target.strip().lower()),
                    None,
                )
                if hit is not None:
                    self._agents.remove(hit.name)
                    return f"Deleted the agent '{hit.name}'."
            return f"I couldn't find anything called '{target}' to delete."
        return f"Unknown tool: {name}"
