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
from helix.services.selfdev import restore_if_changed, scan_tree, snapshot_files, tree_changed


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
        workspace = self._builds.create_workspace(app)

        # A build may ONLY write inside its own workspace. Snapshot everything else — source, data/
        # (db, settings, sibling apps), and the git hooks dir — and verify it's untouched afterward.
        guard = snapshot_files(self._guard_files)  # byte-revert settings if tampered
        skip = (self._app_root / ".git", workspace)
        tree_sig = scan_tree(self._app_root, skip=skip)
        hooks = self._repo.hooks_dir(self._app_root)
        hooks_sig = scan_tree(hooks)

        result = self._coder.run_task(
            workspace, build_app_prompt(app.name, request), on_progress=on_progress
        )
        restore_if_changed(guard)
        if not result.ok:
            raise BuildError(result.error or result.summary or "the build failed")

        escaped = tree_changed(self._app_root, tree_sig, skip=skip) + tree_changed(hooks, hooks_sig)
        if escaped:
            self._revert_escapes(escaped)
            raise BuildError("the build wrote outside its workspace and was blocked.")

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app

    def _revert_escapes(self, escaped: list[str]) -> None:
        root = self._app_root.resolve()
        source_rels: list[str] = []
        for ap in escaped:
            p = Path(ap)
            try:
                rel = str(p.resolve().relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if rel.startswith("data/"):
                continue  # data/ writes are detected + refused (not byte-reverted here)
            if rel.startswith(".git") or "/.git/" in f"/{rel}":
                try:  # a planted hook — remove it
                    if p.is_file() and not p.name.endswith(".sample"):
                        p.unlink()
                except OSError:
                    pass
                continue
            source_rels.append(rel)
        if source_rels:
            try:
                self._repo.restore_paths(self._app_root, source_rels)
            except Exception:
                pass
