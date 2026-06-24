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
        builds_root = (self._app_root / "data" / "builds").resolve()
        source_rels: list[str] = []
        siblings: set[Path] = set()
        for ap in escaped:
            rp = Path(ap).resolve()
            try:
                rel = str(rp.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if builds_root in rp.parents:
                # a write into ANOTHER built app — revert that app's whole working tree to its commit
                try:
                    siblings.add(builds_root / rp.relative_to(builds_root).parts[0])
                except (ValueError, IndexError):
                    pass
            elif rel.startswith("data/"):
                continue  # db/log/settings: detected + refused (settings already byte-reverted)
            elif ".git" in rp.parts:
                try:  # a planted hook — remove it
                    if rp.is_file() and not rp.name.endswith(".sample"):
                        rp.unlink()
                except OSError:
                    pass
            else:
                source_rels.append(rel)
        for sibling in siblings:
            try:
                self._repo.discard_changes(sibling)  # restore the sibling app to its last commit
            except Exception:
                pass
        if source_rels:
            try:
                self._repo.restore_paths(self._app_root, source_rels)
            except Exception:
                pass
