"""ForgeService — the core loop: describe → build → register. The product, in one method."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from helix.config import volatile_data_paths
from helix.domain.errors import BuildCancelled, BuildError
from helix.domain.events import BuildCreated, BuildDeleted, BuildIterated, BuildStarted
from helix.domain.models import App, BuildKind, slugify
from helix.domain.vocabulary import KIND_SYNONYMS, kind_label
from helix.logging_setup import get_logger
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.events import EventBus
from helix.ports.repo import VersionedRepo
from helix.services.builds import BuildService
from helix.services.cancel import BuildHandle, CancelToken
from helix.services.prompts import (
    build_3d_model_prompt,
    build_app_prompt,
    build_task_prompt,
    edit_app_prompt,
    edit_task_prompt,
    repair_prompt,
)
from helix.services.sandbox import restore_if_changed, scan_tree, snapshot_files, tree_changed

if TYPE_CHECKING:
    from helix.services.model_baker import ModelBaker

_LOG = get_logger("forge")

# Filler words dropped before a fuzzy build-name match, so "update my garden hologram" still resolves
# to the build named "Garden Walkthrough". Conservative on purpose — only obvious connective words
# plus every creation word the user may say, old or new, sourced from the ONE vocabulary synonym
# table so the two never drift apart.
_NAME_FILLER = frozenset({
    "the", "a", "an", "my", "your", "our", "this", "that", "please",
    "agents", "build", "3d",
    *KIND_SYNONYMS,
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
        data_dir: Path | None = None,
    ) -> None:
        self._builds = builds
        self._coder = coder
        self._bus = bus
        self._repo = repo
        self._app_root = app_root
        # data/ no longer necessarily lives under the app root (a frozen build keeps it in
        # %LOCALAPPDATA%), so the guard's skip set must use the REAL location, not app_root/"data".
        self._data_dir = data_dir or (app_root / "data")
        self._guard_files = list(guard_files or [])
        # The hologram baker: compiles a built model.scad (OpenSCAD, through the CadEngine port) into the
        # mesh + exports + viewer, and judges the work in _verify_workspace's MODEL branch first.
        self._baker = model_baker

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
            # never silently moves a hologram out of its tab); new builds default to a plain app.
            app.build_kind = prior.build_kind if prior else BuildKind.APP
        elif prior is not None and prior.build_kind != kind:
            # The name is taken by a DIFFERENT kind — refuse instead of silently flipping a model into a
            # task (or vice versa) and overwriting its workspace. Ask the user to pick another name.
            raise BuildError(
                f"There's already a {kind_label(prior.build_kind.value)} called '{prior.name}'. "
                f"Choose a different name for the new {kind_label(kind.value)}."
            )
        else:
            app.build_kind = kind
        if prior is not None:
            # Iterating: keep the creation date and the original blueprint, so the menu subtitle isn't
            # degraded to the change fragment and the card doesn't jump to the top as if brand new.
            app.created_at = prior.created_at
            app.request = prior.request
        workspace = self._builds.create_workspace(app)
        # A hologram's bake cycle is OWNED HERE: prepare() runs for every MODEL build — new or iterating,
        # it is idempotent — once the workspace exists and BEFORE the coder runs. It seeds helix.scad so a
        # coder that lists the folder finds the one library its prompt names (it used to be written first
        # by check(), after the coder had already gone looking), and it opens a fresh cycle so the
        # critic's one look lands on THIS build's first check even when the previous build of the same
        # hologram never reached bake() (a repair pass that was cancelled, failed, or escaped).
        if app.is_model and self._baker is not None:
            self._baker.prepare(workspace)
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
        # Skip: git's own churn, the whole builds tree (concurrent builds write their own workspaces),
        # and every VOLATILE store the live app rewrites WHILE a build runs (helix.db checkpoints, the
        # heartbeat stamping agents, background distillers, a key connected mid-build, a reflex
        # consolidated). Without these, HELIX's own mid-build writes read as the BUILD escaping and a
        # good build fails. The volatile list is shared with the self-dev guard (config), so they
        # can never drift apart.
        skip = (
            self._app_root / ".git",
            self._builds.dir,
            *volatile_data_paths(self._data_dir),
            *self._guard_files,
        )
        tree_sig = scan_tree(self._app_root, skip=skip)
        hooks = self._repo.hooks_dir(self._app_root)
        hooks_sig = scan_tree(hooks)

        # The coder runs at most twice: the build itself, and ONE automatic repair pass if the result
        # fails the pre-finalize check (a syntax error, a missing entry point, a hologram whose model.scad
        # won't compile or whose rendered preview contradicts its brief). Every pass gets the full
        # guard treatment — escapes are scanned and reverted UNCONDITIONALLY, before the cancel/failure
        # exits, because a cancelled or failed run drove the same coder with the same hands.
        prompt_text = prompt or self._default_prompt(app, request, iterating)
        problem: str | None = None
        for attempt in range(2):
            result = self._coder.run_task(
                workspace, prompt_text, on_progress=on_progress, cancel=cancel,
            )
            restore_if_changed(guard)
            escaped = tree_changed(self._app_root, tree_sig, skip=skip) + tree_changed(hooks, hooks_sig)
            if escaped:
                self._revert_escapes(escaped)
                _LOG.warning("build %r wrote outside its workspace: %s", app.name, escaped)
            if cancel is not None and cancel.is_set():
                # Stopped mid-build: don't finalize. The token carries the handle so the UI can offer
                # cleanup. The in-progress marker deliberately STAYS until the user answers that offer —
                # a cancel alone does not settle the build. Clearing it here would strand the routine
                # case: closing HELIX mid-build cancels every active job, and BuildQueue skips the
                # cleanup announcement while shutting down (there is no UI left to answer it), so nobody
                # is ever asked. Without the marker, the next launch's recover_interrupted skips the
                # build entirely and a half-edited app stays live and broken. keep_build() clears it
                # when the user says keep; discard_build() removes the work outright.
                raise BuildCancelled(app.slug, app.name, iterating, bool(app.is_model))
            if not result.ok:
                # The coder gave up mid-edit. Same treatment as a failed check: a half-applied change must
                # not stay live (an iteration goes back to its last working version, a never-finalized new
                # build is removed) — otherwise a broken app sits in the menu looking ready.
                self._rollback_failed(app, workspace, iterating)
                raise BuildError(result.error or result.summary or "the build failed")
            if escaped:
                # Name WHAT it tried to touch — the opaque "wrote outside its workspace" told the user
                # (and us) nothing; now the announcement and the log pinpoint the offending file(s).
                # The workspace goes back too: reverting only the escaped files would leave the rest of a
                # build we just refused to trust live and openable, contradicting "rolled it back".
                self._rollback_failed(app, workspace, iterating)
                raise BuildError(
                    f"the build tried to change files outside its own folder "
                    f"({self._escape_names(escaped)}), so I blocked it and rolled it back."
                )
            problem = self._verify_workspace(workspace, app.build_kind)
            if problem is None:
                break
            if attempt == 0:
                _LOG.warning("build %r failed its check (%s) — one repair pass", app.name, problem)
                if on_progress:
                    on_progress("Fixing a problem I caught checking the work…")
                prompt_text = repair_prompt(app.name, problem)
        if problem is not None:
            # Both passes failed the checks. Do NOT leave the broken result on disk (the app is opened
            # straight from its workspace): an ITERATION rolls back to its last good, still-working
            # version; a never-finalized NEW build's broken scaffold is removed so it can't linger in the
            # menu. Without this, a working app stays silently broken until the next restart's recovery.
            self._rollback_failed(app, workspace, iterating)
            raise BuildError(f"the finished build didn't pass its checks ({problem})")

        # A hologram is delivered as a PROGRAM the coder wrote (model.scad); HELIX itself compiles it here
        # through the CadEngine port — an in-process subprocess to the OpenSCAD CLI on THIS worker thread,
        # never a shell handed to the coder — and wraps the mesh in its own viewer. The baker's three
        # calls are one cycle, in order: prepare() above before the coder ran, check() after each coder
        # pass in _verify_workspace, and bake() here once the check is happy — bake() closes the cycle.
        # Runs AFTER the escape check (the coder's output is validated first) and only writes inside the
        # workspace, which the guard skips. The compile itself usually already happened in check() (the
        # baker keys its record by the source's hash, so the same text is never compiled twice); bake()
        # still never raises — a missing engine or a compile hiccup becomes a friendly in-viewer page, not
        # a failed build that rolls back work the coder did right.
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

    def keep_build(self, handle: BuildHandle) -> None:
        """The user answered KEEP to a stopped build's cleanup offer. That answer — not the stop itself —
        is what settles the build, so this is where the in-progress marker goes. Leaving it would let the
        next launch's recover_interrupted read it as a crash and roll back (or delete) the very work the
        user just chose to keep. The counterpart to discard_build."""
        self._builds.clear_building(handle.slug)

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

    def _rollback_failed(self, app: App, workspace: Path, iterating: bool) -> None:
        """Undo a build that settled badly — a coder failure, a sandbox escape, or a failed pre-finalize
        check — so a broken half-edited result never sits on disk. An iteration is restored to its last
        committed version; a never-finalized new build is deleted. (Cancellation is NOT a bad settle: the
        user is offered the choice, so it only clears the marker and leaves the work for discard_build.)"""
        try:
            if iterating:
                self._repo.discard_changes(workspace)  # back to the last good, working version
                self._builds.clear_building(app.slug)   # settled — don't let startup recovery re-touch it
            elif self._builds.delete(app.slug):
                self._bus.publish(BuildDeleted(app.slug))
        except Exception:  # noqa: BLE001 — a rollback hiccup must not mask the original BuildError
            _LOG.warning("could not roll back failed build %s", app.slug, exc_info=True)

    def _default_prompt(self, app: App, request: str, iterating: bool) -> str:
        """The coder instruction for this build — kind- AND iteration-aware. An edit of an existing
        build gets an edit prompt (smallest change, keep everything else), never the from-scratch one
        with only the change fragment. The 3D prompt is already edit-aware (it reuses the workspace)."""
        if app.build_kind == BuildKind.MODEL:
            return build_3d_model_prompt(app.name, request)
        if app.build_kind == BuildKind.TASK:
            return edit_task_prompt(app.name, request) if iterating else build_task_prompt(app.name, request)
        return edit_app_prompt(app.name, request) if iterating else build_app_prompt(app.name, request)

    def _verify_workspace(self, ws: Path, kind: BuildKind) -> str | None:
        """The pre-finalize gate: every .py must compile and the build must have a real entry point; a
        hologram's model.scad must compile and survive one look from the vision critic (the baker does
        that part). Cheap for apps (no execution); a hologram pays for its compile here, on the worker.
        Returns a one-line problem description, or None when it passes — so a build that LOOKS finished
        but can't even parse never lands in the menu as 'ready'."""
        if kind == BuildKind.KNOWLEDGE:
            return None  # ingested data, never a runnable artifact
        vendor = {".git", ".venv", "venv", "site-packages", "node_modules", "__pycache__"}
        problems: list[str] = []
        for py in ws.rglob("*.py"):
            if any(part in vendor for part in py.parts):
                continue  # only gate the coder's own code, not an installed dependency tree
            try:
                compile(py.read_text(encoding="utf-8", errors="replace"), str(py), "exec")
            except SyntaxError as exc:
                problems.append(f"{py.name} line {exc.lineno}: {exc.msg}")
                if len(problems) >= 3:
                    break
            except OSError:
                continue
        if problems:
            return "Python syntax errors — " + "; ".join(problems)
        if kind == BuildKind.MODEL:
            # A hologram is judged by the BAKER: it lints and compiles model.scad through the CadEngine,
            # renders the preview and asks the vision critic for one look, and hands back a problem in
            # exactly the shape repair_prompt expects (the compiler's file:line words, or "Looking at the
            # rendered preview (assets/preview.png): …"). Delegating the WHOLE branch — not just the
            # model.scad case — is what lets the baker ask for model.scad when it finds a model.json from
            # the retired primitive engine, so "make it wider" on an old hologram migrates the design in
            # the same build's repair pass instead of leaving it stranded. check() never raises and reads
            # a missing engine as "not the coder's fault" (None); bake() then shows the install page.
            if self._baker is not None:
                return self._baker.check(ws)
            # No baker wired (a bare Forge in a test): the design file is enough. model.py is THE
            # deliverable; model.scad is the retired engine's legacy shape, and model.json (an
            # environment or a reference) and a hand-authored animated index.html are the other
            # shapes a hologram may take.
            if not any((ws / f).exists() for f in ("model.py", "model.scad", "model.json", "index.html")):
                return "no model.py was produced"
            return None
        if kind == BuildKind.TASK:
            return None if (ws / "main.py").exists() else "the entry point main.py is missing"
        has_entry = (
            (ws / "main.py").exists()
            or (ws / "index.html").exists()
            or next(iter(ws.glob("*.html")), None) is not None
            or next(iter(ws.glob("*.py")), None) is not None
        )
        return None if has_entry else "no runnable entry point (index.html or main.py) was produced"

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
        # NOTE: `escaped` is computed by scan_tree/tree_changed with a `skip` set that includes the whole
        # builds tree (self._builds.dir), so a write into ANOTHER build's folder is deliberately NOT an
        # escape (it enables parallel builds — see test_build_into_a_sibling_workspace_is_allowed). There
        # is therefore no builds-tree path to handle here; the protections that matter — source, settings,
        # .git, hooks — are all outside that skip and are handled below.
        root = self._app_root.resolve()
        data_root = self._data_dir.resolve()
        source_rels: list[str] = []
        for ap in escaped:
            rp = Path(ap).resolve()
            if data_root == rp or data_root in rp.parents:
                continue  # db/log/settings: detected + refused (settings already byte-reverted)
            try:
                rel = str(rp.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue  # outside the app tree entirely — nothing of ours to restore
            if ".git" in rp.parts:
                try:  # a planted hook — remove it
                    if rp.is_file() and not rp.name.endswith(".sample"):
                        rp.unlink()
                except OSError:
                    _LOG.critical("could not remove planted git hook: %s", rp, exc_info=True)
            else:
                source_rels.append(rel)
        if source_rels:
            try:
                self._repo.restore_paths(self._app_root, source_rels)
            except Exception:
                _LOG.critical("could not revert escaped writes to source: %s", source_rels, exc_info=True)
