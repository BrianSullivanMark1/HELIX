"""ForgeService — the core loop: describe → build → register. The product, in one method."""
from __future__ import annotations

from helix.domain.errors import BuildError
from helix.domain.events import BuildCreated, BuildIterated
from helix.domain.models import App
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.events import EventBus
from helix.services.builds import BuildService
from helix.services.prompts import build_app_prompt


class ForgeService:
    def __init__(self, builds: BuildService, coder: CoderAgent, bus: EventBus) -> None:
        self._builds = builds
        self._coder = coder
        self._bus = bus

    def build(self, name: str, request: str, *, on_progress: ProgressFn | None = None) -> App:
        app = App.from_request(name, request)
        iterating = self._builds.exists(app.slug)
        workspace = self._builds.create_workspace(app)

        result = self._coder.run_task(
            workspace, build_app_prompt(app.name, request), on_progress=on_progress
        )
        if not result.ok:
            raise BuildError(result.error or result.summary or "the build failed")

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app
