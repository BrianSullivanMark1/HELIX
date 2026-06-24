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
from helix.services.selfdev import restore_if_changed, snapshot_files


class ForgeService:
    def __init__(
        self,
        builds: BuildService,
        coder: CoderAgent,
        bus: EventBus,
        repo: VersionedRepo,
        app_root: Path,
        guard_files: list[Path] | None = None,
    ) -> None:
        self._builds = builds
        self._coder = coder
        self._bus = bus
        self._repo = repo
        self._app_root = app_root
        self._guard_files = list(guard_files or [])

    def build(self, name: str, request: str, *, on_progress: ProgressFn | None = None) -> App:
        app = App.from_request(name, request)
        iterating = self._builds.exists(app.slug)
        # Snapshot the live repo cleanliness + guarded files first, so we only flag NEW escapes.
        clean_before = self._repo.is_clean(self._app_root)
        guard = snapshot_files(self._guard_files)
        workspace = self._builds.create_workspace(app)

        result = self._coder.run_task(
            workspace, build_app_prompt(app.name, request), on_progress=on_progress
        )

        # A build must only ever write inside its own (gitignored) workspace.
        reverted = restore_if_changed(guard)  # settings live in gitignored data/ — invisible to git
        if not result.ok:
            raise BuildError(result.error or result.summary or "the build failed")
        if reverted:
            raise BuildError("the build tried to modify HELIX's settings and was blocked.")
        if clean_before and not self._repo.is_clean(self._app_root):
            self._repo.discard_changes(self._app_root)
            raise BuildError("the build tried to modify HELIX itself and was blocked.")

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app
