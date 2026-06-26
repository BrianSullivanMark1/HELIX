"""ForgeService — the core loop: describe → build → register. The product, in one method."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from helix.domain.errors import BuildCancelled, BuildError
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated
from helix.domain.models import App
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.events import EventBus
from helix.ports.repo import VersionedRepo
from helix.services.builds import BuildService
from helix.services.cancel import BuildHandle, CancelToken
from helix.services.prompts import build_app_prompt
from helix.services.selfdev import restore_if_changed, scan_tree, snapshot_files, tree_changed

if TYPE_CHECKING:
    from helix.services.model_baker import ModelBaker


class ForgeService:
    def __init__(
        self,
        builds: BuildService,
        coder: CoderAgent,
        bus: EventBus,
        repo: VersionedRepo,
        app_root: Path,
        guard_files: list[Path] | None = None,
        model_baker: "ModelBaker | None" = None,
    ) -> None:
        self._builds = builds
        self._coder = coder
        self._bus = bus
        self._repo = repo
        self._app_root = app_root
        self._guard_files = list(guard_files or [])
        self._baker = model_baker  # turns a built model.json into assets/model.glb + a viewer

    def build(
        self,
        name: str,
        request: str,
        *,
        prompt: str | None = None,
        is_model: bool | None = None,
        on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None,
    ) -> App:
        # `prompt` overrides the default app-builder instruction so the same sandboxed loop can also
        # drive a different kind of build (e.g. a 3D model). `is_model` tags the result so it lands in
        # the Models tab instead of Apps. The sandbox/guard is identical either way.
        app = App.from_request(name, request)
        iterating = self._builds.exists(app.slug)
        if is_model is None:
            # Caller didn't classify: keep an existing build's class on iteration (so a plain rebuild
            # never silently moves a model out of the Models tab); new builds default to a plain app.
            prior = next((a for a in self._builds.list() if a.slug == app.slug), None) if iterating else None
            app.is_model = bool(prior and prior.is_model)
        else:
            app.is_model = is_model
        workspace = self._builds.create_workspace(app)
        # Record what's being built so a mid-run 'stop' can offer to remove/roll back this exact work.
        if cancel is not None:
            cancel.build = BuildHandle(app.slug, app.name, iterating, bool(app.is_model))

        # A build may ONLY write inside its own workspace. Snapshot everything else — source, data/
        # (db, settings, sibling apps), and the git hooks dir — and verify it's untouched afterward.
        guard = snapshot_files(self._guard_files)  # byte-revert settings if tampered
        skip = (self._app_root / ".git", workspace)
        tree_sig = scan_tree(self._app_root, skip=skip)
        hooks = self._repo.hooks_dir(self._app_root)
        hooks_sig = scan_tree(hooks)

        result = self._coder.run_task(
            workspace, prompt or build_app_prompt(app.name, request),
            on_progress=on_progress, cancel=cancel,
        )
        restore_if_changed(guard)
        if cancel is not None and cancel.is_set():
            # Stopped mid-build: don't finalize. The token carries the handle so the UI can offer cleanup.
            raise BuildCancelled(app.slug, app.name, iterating, bool(app.is_model))
        if not result.ok:
            raise BuildError(result.error or result.summary or "the build failed")

        escaped = tree_changed(self._app_root, tree_sig, skip=skip) + tree_changed(hooks, hooks_sig)
        if escaped:
            self._revert_escapes(escaped)
            raise BuildError("the build wrote outside its workspace and was blocked.")

        # A model is delivered as a small model.json the coder wrote; HELIX itself bakes it into a real
        # mesh + viewer here (in-process — the coder never gets a shell). Runs AFTER the escape check
        # (the coder's output is validated first) and only writes inside the workspace, which the guard
        # skips. bake() never raises: a bad spec becomes a friendly in-viewer message.
        if app.is_model and self._baker is not None:
            self._baker.bake(workspace)

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app

    def discard_build(self, handle: BuildHandle) -> None:
        """Remove the work a stopped build left behind: delete a brand-new build outright, or roll an
        interrupted iteration back to its last committed (good) version. Announces BuildDeleted so the
        menu refreshes."""
        ws = self._builds.workspace(handle.slug)
        if handle.iterating:
            try:
                self._repo.discard_changes(ws)  # keep the prior good version; drop the partial edit
            except Exception:
                pass
        else:
            self._builds.delete(handle.slug)  # a new build that never finished — remove it entirely
        self._bus.publish(BuildDeleted(handle.slug))

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
