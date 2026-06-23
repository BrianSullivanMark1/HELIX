"""ToolRegistry — the model's hands. Maps tool calls to service methods."""
from __future__ import annotations

from helix.ports.coder import ProgressFn
from helix.ports.llm import ToolSpec
from helix.services.builds import BuildService
from helix.services.forge import ForgeService


class ToolRegistry:
    def __init__(self, forge: ForgeService, builds: BuildService) -> None:
        self._forge = forge
        self._builds = builds

    def specs(self) -> list[ToolSpec]:
        return [
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

    def dispatch(self, name: str, args: dict, *, on_progress: ProgressFn | None = None) -> str:
        if name == "build_app":
            app = self._forge.build(args["name"], args["request"], on_progress=on_progress)
            return f"Built '{app.name}'. It's in the menu now."
        if name == "list_apps":
            apps = self._builds.list()
            if not apps:
                return "No apps built yet."
            return "\n".join(f"- {a.name}: {a.request}" for a in apps)
        return f"Unknown tool: {name}"
