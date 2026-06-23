"""ToolRegistry — the model's hands. Maps tool calls to service methods."""
from __future__ import annotations

from helix.ports.coder import ProgressFn
from helix.ports.llm import ToolSpec
from helix.services.builds import BuildService
from helix.services.forge import ForgeService
from helix.services.selfdev import SelfDevService


class ToolRegistry:
    def __init__(
        self, forge: ForgeService, builds: BuildService, selfdev: SelfDevService | None = None
    ) -> None:
        self._forge = forge
        self._builds = builds
        self._selfdev = selfdev

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
                name="list_apps",
                description="List the apps the user has already built.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]
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
        return tools

    def dispatch(self, name: str, args: dict, *, on_progress: ProgressFn | None = None) -> str:
        if name == "build_app":
            app = self._forge.build(args["name"], args["request"], on_progress=on_progress)
            return f"Built '{app.name}'. It's in the menu now."
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
        return f"Unknown tool: {name}"
