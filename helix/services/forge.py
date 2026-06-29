"""ForgeService — the core loop: describe → build → register. The product, in one method."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from helix.domain.errors import BuildCancelled, BuildError
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated, BuildStarted
from helix.domain.models import App, BuildKind, slugify
from helix.logging_setup import get_logger
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.events import EventBus
from helix.ports.repo import VersionedRepo
from helix.services.builds import BuildService
from helix.services.cancel import BuildHandle, CancelToken
from helix.services.prompts import build_app_prompt
from helix.services.sandbox import restore_if_changed, scan_tree, snapshot_files, tree_changed

if TYPE_CHECKING:
    from helix.services.model_baker import ModelBaker

_LOG = get_logger("forge")

# Filler words dropped before a fuzzy build-name match, so "update my garden model" still resolves to the
# build named "Garden Walkthrough". Conservative on purpose — only obvious connective/kind words.
_NAME_FILLER = frozenset({
    "the", "a", "an", "my", "your", "our", "this", "that", "please",
    "model", "models", "app", "apps", "application", "task", "tasks", "agent", "build", "3d",
})


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
        kind: BuildKind | None = None,
        on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None,
    ) -> App:
        # `prompt` overrides the default app-builder instruction so the same sandboxed loop can also
        # drive a different kind of build (a task, a 3D model). `kind` tags the result so it lands in the
        # right menu tab. The sandbox/guard is identical for every kind.
        app = App.from_request(name, request)
        existing = self._builds.list()
        # Identity resolution (defends the 'mutable by talking' vision): an exact slug or display-name
        # match iterates that build in place; failing that, an UNAMBIGUOUS same-kind fuzzy match catches a
        # paraphrase ("update my garden" → the only model called "Garden Walkthrough") so the user reliably
        # edits the build they MEAN instead of silently forking a near-duplicate.
        prior = self._resolve_prior(name, app.slug, kind, existing)
        if prior is not None:
            app.slug = prior.slug  # iterate in place, whatever name the user used this time
        iterating = prior is not None
        if kind is None:
            # Caller didn't classify: keep an existing build's kind on iteration (so a plain rebuild
            # never silently moves a model out of the Models tab); new builds default to a plain app.
            app.build_kind = prior.build_kind if prior else BuildKind.APP
        elif prior is not None and prior.build_kind != kind:
            # The name is taken by a DIFFERENT kind — refuse instead of silently flipping a model into a
            # task (or vice versa) and overwriting its workspace. Ask the user to pick another name.
            raise BuildError(
                f"There's already a {prior.build_kind.value} called '{prior.name}'. "
                f"Choose a different name for the new {kind.value}."
            )
        else:
            app.build_kind = kind
        if prior is not None:
            # Iterating: keep the creation date and the original blueprint, so the menu subtitle isn't
            # degraded to the change fragment and the card doesn't jump to the top as if brand new.
            app.created_at = prior.created_at
            app.request = prior.request
        workspace = self._builds.create_workspace(app)
        self._builds.mark_building(app.slug)  # recoverable if the process is killed mid-build
        # Record what's being built so a mid-run 'stop' can offer to remove/roll back this exact work.
        if cancel is not None:
            cancel.build = BuildHandle(app.slug, app.name, iterating, bool(app.is_model))
        # Announce the build has begun, so the menu tile, the Console legend, and the orb's hue reflect
        # in-progress work immediately (the create/iterate/finished events only land at the end).
        self._bus.publish(BuildStarted(app.name, app.slug, iterating))

        # A build may ONLY write inside its own workspace. Snapshot everything else — source code and the
        # git hooks dir — and verify it's untouched afterward. The skip set excludes:
        #   - .git (git's own churn),
        #   - the WHOLE data/builds tree, NOT just this build's folder: with CONCURRENT builds a sibling
        #     writing to its own workspace must not look like THIS build escaping (which would falsely
        #     revert the sibling's good work). Trade-off: a build deliberately writing an ABSOLUTE path
        #     into a sibling's folder is no longer detected. The API coder confines writes to its cwd
        #     (api_coder._safe_target); the CLI coder does NOT, so cross-build (data↔data) isolation now
        #     rests on coders writing within their workspace, not on this tripwire. The protections that
        #     matter — source, settings, the API key, .git, hooks — are unaffected.
        #   - helix.db and the guarded settings file: both are VOLATILE main-app state the UI thread
        #     rewrites WHILE a build runs (a sqlite checkpoint; a settings change). Without this, a write
        #     by the app — not the build — would (a) falsely fail an otherwise-good build and (b) for
        #     settings, the mtime bump from the byte-revert below would itself read as an escape. Settings
        #     are still protected: tampering is reverted byte-for-byte by snapshot_files/restore below.
        guard = snapshot_files(self._guard_files)  # byte-revert settings if tampered
        skip = (
            self._app_root / ".git",
            self._app_root / "data" / "builds",
            self._app_root / "data" / "helix.db",
            self._app_root / "data" / "helix_secrets.json",  # volatile: the user can connect a key mid-build
            *self._guard_files,
        )
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
            _LOG.warning("build %r wrote outside its workspace: %s", app.name, escaped)
            # Name WHAT it tried to touch — the opaque "wrote outside its workspace" told the user (and us)
            # nothing; now the announcement and the log both pinpoint the offending file(s).
            raise BuildError(
                f"the build tried to change files outside its own folder ({self._escape_names(escaped)}), "
                "so I blocked it and rolled it back."
            )

        # A model is delivered as a small model.json the coder wrote; HELIX itself bakes it into a real
        # mesh + viewer here (in-process — the coder never gets a shell). Runs AFTER the escape check
        # (the coder's output is validated first) and only writes inside the workspace, which the guard
        # skips. bake() never raises: a bad spec becomes a friendly in-viewer message.
        if app.is_model and self._baker is not None:
            self._baker.bake(workspace)

        app = self._builds.finalize(app)
        self._bus.publish(BuildIterated(app) if iterating else BuildCreated(app))
        return app

    def remove_build(self, name: str) -> bool:
        """Delete a build the user named in conversation (app/task/model) and refresh the menu. Matches
        by slug first, then by display name (case-insensitive). Returns True if something was removed."""
        target = name.strip().lower()
        slug = slugify(name)
        app = next(
            (a for a in self._builds.list() if a.slug == slug or a.name.strip().lower() == target),
            None,
        )
        if app is None or not self._builds.delete(app.slug):
            return False
        self._bus.publish(BuildDeleted(app.slug))
        return True

    def discard_build(self, handle: BuildHandle) -> bool:
        """Remove the work a stopped build left behind: delete a brand-new build outright, or roll an
        interrupted iteration back to its last committed (good) version. Returns True on success (False
        if a locked workspace refused removal, so the caller can be honest), and announces BuildDeleted so
        the menu refreshes."""
        ws = self._builds.workspace(handle.slug)
        if handle.iterating:
            try:
                self._repo.discard_changes(ws)  # keep the prior good version; drop the partial edit
            except Exception:
                _LOG.warning("could not roll back interrupted iteration %s", handle.slug, exc_info=True)
                return False
        elif not self._builds.delete(handle.slug):  # a new build that never finished — remove it entirely
            return False  # locked (still open / mid-write): don't claim a removal that didn't happen
        self._bus.publish(BuildDeleted(handle.slug))
        return True

    def _was_finalized(self, ws: Path) -> bool:
        """Has this workspace ever been finalized (vs. only scaffolded)? create_workspace makes ONE
        'scaffold' commit; finalize adds a 'build:' commit — so >1 commit means it has a good version to
        roll back to. Inferred from git, NOT from entry_point (which legitimately stays None for a real
        build whose entry the heuristic didn't detect — using it would DELETE a good build). On any doubt
        we report True so recovery takes the SAFE, non-deleting path."""
        try:
            return len(self._repo.log(ws, limit=2)) > 1
        except Exception:
            return True

    def recover_interrupted(self, active_slugs: set[str] | None = None) -> None:
        """On startup, clean up builds left half-written by a crash/kill/power-loss (a .building marker
        that graceful shutdown would have cleared). An interrupted ITERATION rolls back to its last good
        commit; an interrupted NEW build (never finalized) is removed entirely. Skips any build that's
        somehow still active."""
        active = active_slugs or set()
        for app in self._builds.list():
            if app.slug in active or not self._builds.is_building(app.slug):
                continue
            ws = self._builds.workspace(app.slug)
            try:
                if self._was_finalized(ws):  # has a good committed version → roll the partial edit back
                    self._repo.discard_changes(ws)
                    self._builds.clear_building(app.slug)  # don't rely on git-clean to drop the marker
                else:  # never finalized → an interrupted brand-new build: remove it
                    if self._builds.delete(app.slug):
                        self._bus.publish(BuildDeleted(app.slug))
            except Exception:
                _LOG.warning("could not recover interrupted build %s", app.slug, exc_info=True)

    @staticmethod
    def _name_tokens(name: str) -> frozenset:
        """Significant words of a build name, lowercased with punctuation and filler stripped."""
        toks = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
        return frozenset(t for t in toks if t not in _NAME_FILLER)

    def _resolve_prior(self, name: str, app_slug: str, kind, existing):
        """Find the existing build a (re)build should iterate IN PLACE: exact slug → exact display name →
        an UNAMBIGUOUS same-kind fuzzy match (one name's significant words a subset of the other's).
        Returns None when nothing matches or a fuzzy match is ambiguous — then a brand-new build is made."""
        prior = next((a for a in existing if a.slug == app_slug), None)
        if prior is not None:
            return prior
        target = (name or "").strip().lower()
        prior = next((a for a in existing if a.name.strip().lower() == target), None)
        if prior is not None:
            return prior
        if kind is None:
            return None  # an unclassified rebuild — don't fuzzy-match across kinds
        want = self._name_tokens(name)
        if not want:
            return None
        cands = []
        for a in existing:
            if a.build_kind != kind:
                continue
            have = self._name_tokens(a.name)
            if have and (want <= have or have <= want):
                cands.append(a)
        if len(cands) == 1:
            _LOG.info("resolved build name %r to existing %r by fuzzy match", name, cands[0].name)
            return cands[0]
        return None

    def _escape_names(self, escaped: list[str]) -> str:
        """A short, friendly list of WHAT a build tried to write outside its workspace, for the error
        message (the full paths go to the log). Deduped filenames, capped at a handful."""
        root = self._app_root.resolve()
        names: list[str] = []
        for ap in escaped:
            p = Path(ap)
            try:
                rel = str(p.resolve().relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = p.name
            short = rel.rsplit("/", 1)[-1] or rel
            if short and short not in names:
                names.append(short)
        if not names:
            return "a protected file"
        extra = len(names) - 4
        return ", ".join(names[:4]) + (f", and {extra} more" if extra > 0 else "")

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
                    _LOG.critical("could not remove planted git hook: %s", rp, exc_info=True)
            else:
                source_rels.append(rel)
        for sibling in siblings:
            try:
                self._repo.discard_changes(sibling)  # restore the sibling app to its last commit
            except Exception:
                _LOG.critical("could not revert escaped write to sibling build: %s", sibling, exc_info=True)
        if source_rels:
            try:
                self._repo.restore_paths(self._app_root, source_rels)
            except Exception:
                _LOG.critical("could not revert escaped writes to source: %s", source_rels, exc_info=True)
