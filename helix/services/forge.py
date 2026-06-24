"""ForgeService — the core loop: describe → build → register. The product, in one method."""
from __future__ import annotations

from pathlib import Path

from helix.domain.errors import BuildError
from helix.domain.events import BuildCreated, BuildIterated
from helix.domain.models import App
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.events import EventBus
from helix.ports.repo import VersionedRepo
from helix.services.builds import BuildService
from helix.services.prompts import build_app_prompt


class ForgeService:
    def __init__(
        self,
        builds: BuildService,
        coder: CoderAgent,
        bus: EventBus,
        repo: VersionedRepo,
        app_root: Path,
    ) -> None:
        self._builds = builds
        self._coder = coder
        self._bus = bus
        self._repo = repo
        self._app_root = app_root

    def build(self, name: str, request: str, *, on_progress: ProgressFn | None = None) -> App:
        app = App.from_request(name, request)
        iterating = self._builds.exists(app.slug)
        # Snapshot the live repo's cleanliness first, so we only flag NEW escapes (dev trees are dirty).
        clean_before = self._repo.is_clean(self._app_root)
        workspace = self._builds.create_workspace(app)

        result = self._coder.run_task(
            workspace, build_app_prompt(app.name, request), on_progress=on_progress
        )
        if not result.ok:
            raise BuildError(result.error or result.summary or "the build failed")

        # A build must only ever write inside its own (gitignored) workspace. If the live HELIX repo's
        # tracked tree changed, the coder escaped into HELIX's source — revert it and refuse.
        if clean_before and not self._repo.is_clean(self._app_root):
            self._repo.discard_changes(self._app_root)
            raise BuildError("the build tried to modify HELIX itself and was blocked.")

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app
