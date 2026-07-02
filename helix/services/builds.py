"""BuildService — the lifecycle of a built app's workspace (data/builds/<slug>/)."""
from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path

from helix.domain.models import App, AppKind, BuildKind, slugify
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.repo import VersionedRepo

_LOG = get_logger("builds")

MANIFEST = ".helixbuild.json"
BUILDING = ".building"  # in-progress marker: present while a build runs, cleared on finalize


def _force_remove(func, path, _exc) -> None:
    """rmtree onerror: clear the read-only bit (git's loose objects) and retry the removal once."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


class BuildService:
    def __init__(self, builds_dir: Path, repo: VersionedRepo, clock: Clock) -> None:
        self._dir = builds_dir
        self._repo = repo
        self._clock = clock

    def workspace(self, slug: str) -> Path:
        return self._dir / slug

    @property
    def dir(self) -> Path:
        """The builds root (data/builds). Sidecar caches that must survive a rebuild + not trip the Forge
        escape-guard live here (the guard skips the whole builds tree); list() ignores non-build files."""
        return self._dir

    def exists(self, slug: str) -> bool:
        return (self.workspace(slug) / MANIFEST).exists()

    def create_workspace(self, app: App) -> Path:
        """Make data/builds/<slug>/, git-init it, write the manifest, commit the scaffold."""
        ws = self.workspace(app.slug)
        if self.exists(app.slug):
            return ws  # iterating an existing app
        # A folder with no manifest is a remnant of an interrupted/half-built run — never scaffold a new
        # build on top of it (the coder would inherit a corrupt git state); clear it first.
        if ws.exists():
            shutil.rmtree(ws, onerror=_force_remove)
        ws.mkdir(parents=True, exist_ok=True)
        self._repo.init(ws)
        app.created_at = app.created_at or self._clock.now()  # stamp once; finalize must not reset it
        self._write_manifest(ws, app)
        (ws / "README.md").write_text(f"# {app.name}\n\n{app.request}\n", encoding="utf-8")
        self._repo.commit_all(ws, "scaffold")
        return ws

    def mark_building(self, slug: str) -> None:
        """Drop an in-progress marker so a crash/kill mid-build is recoverable on next launch. Written
        AFTER the scaffold commit (so it never lands in a commit) and cleared by finalize."""
        try:
            (self.workspace(slug) / BUILDING).write_text("1", encoding="utf-8")
        except OSError:
            pass

    def is_building(self, slug: str) -> bool:
        return (self.workspace(slug) / BUILDING).exists()

    def clear_building(self, slug: str) -> None:
        try:
            (self.workspace(slug) / BUILDING).unlink()
        except OSError:
            pass

    def finalize(self, app: App) -> App:
        """After the coder runs: detect how the app runs, persist it, commit the result."""
        ws = self.workspace(app.slug)
        self.clear_building(app.slug)  # the build completed — clear the in-progress marker before committing
        if app.build_kind == BuildKind.KNOWLEDGE:
            # A knowledge base is data the user ingests, never a runnable artifact — it has no entry point.
            # (Knowledge is created directly by KnowledgeService, not the coder, so this is defensive: it
            # keeps _detect_entry from ever mislabelling a base as a launchable app/task.)
            app.kind, app.entry_point = AppKind.UNKNOWN, None
        elif app.build_kind == BuildKind.MODEL and (ws / "index.html").exists():
            # A model ALWAYS opens in its baked Three.js viewer. Pin it, so a stray main.py the coder might
            # have left can't make main.py-first _detect_entry turn the model into a console launch.
            app.kind, app.entry_point = AppKind.HTML, "index.html"
        else:
            app.kind, app.entry_point = self._detect_entry(ws)
        self._write_manifest(ws, app)
        self._repo.commit_all(ws, f"build: {app.name}")
        return app

    def list(self) -> list[App]:
        if not self._dir.exists():
            return []
        apps: list[App] = []
        for child in sorted(self._dir.iterdir()):
            if child.name.endswith(".helixdel"):
                continue  # a move-aside dir from an in-progress/failed delete — not a real build
            manifest = child / MANIFEST
            if manifest.exists():
                # One truncated/corrupt manifest must never empty the whole menu — skip just that build
                # (it stays on disk for repair) and keep listing the rest.
                try:
                    apps.append(self._read_manifest(manifest))
                except (OSError, ValueError, KeyError):
                    _LOG.warning("skipping build with unreadable manifest: %s", manifest)
        return apps

    def categorized(self) -> dict[str, list[App]]:
        """Partition every workspace build by its canonical BuildKind — the single source of truth the
        menu and the orb's list both render, so they always agree. (Agents are a separate substrate and
        are folded in by the caller.) Classification lives here, never in the view."""
        buckets: dict[str, list[App]] = {"apps": [], "tasks": [], "models": [], "knowledge": []}
        by_kind = {
            BuildKind.APP: "apps", BuildKind.TASK: "tasks", BuildKind.MODEL: "models",
            BuildKind.KNOWLEDGE: "knowledge",
        }
        for app in self.list():
            buckets[by_kind.get(app.build_kind, "apps")].append(app)
        return buckets

    def delete(self, slug: str) -> bool:
        """Atomic-or-honest removal. A workspace that's locked (a running task holds it as CWD, an open
        viewer, a live coder) refuses an os.rename on Windows (WinError 32) — so move it aside FIRST and
        only delete on success. A locked build returns False WITHOUT being half-gutted, so the caller can
        honestly say 'still running, close it and try again' instead of lying 'removed' and leaving a
        corrupt remnant the next same-named build would scaffold onto."""
        ws = self.workspace(slug)
        if not ws.exists():
            return False
        aside = ws.with_name(ws.name + ".helixdel")
        try:
            if aside.exists():
                shutil.rmtree(aside, onerror=_force_remove)
            ws.rename(aside)  # atomic on the same volume; refuses (OSError) if the folder is locked
        except OSError:
            return False
        # git marks loose object files read-only, so a plain rmtree silently leaves .git behind on
        # Windows. Clear the read-only bit on any file that refuses to go, then retry the unlink.
        shutil.rmtree(aside, onerror=_force_remove)
        return not ws.exists()

    def rename(self, slug: str, new_name: str) -> App | None:
        """Give a build a new display name, keeping name↔slug consistent so conversational iteration
        (which finds a build by slugify(name)) still resolves. If the new name slugs to a different
        folder, move the whole workspace (its git repo travels with it). Returns the updated App, or
        None if the build is missing, the name is blank, the target slug is already occupied, or the
        move fails.

        Safe against a concurrent build: the move happens BEFORE any manifest write, so a failed move
        leaves the build untouched; and on Windows a build in progress holds the workspace open (the
        coder runs with it as its working directory), so os.rename refuses with OSError instead of
        moving the folder out from under the coder — the caller just retries once the build finishes."""
        manifest = self.workspace(slug) / MANIFEST
        if not manifest.exists():
            return None
        new_name = (new_name or "").strip()
        if not new_name:
            return None
        try:
            app = self._read_manifest(manifest)
        except (OSError, ValueError, KeyError):
            return None  # corrupt manifest — honest failure, same as a missing build
        ws = self.workspace(slug)
        new_slug = slugify(new_name)
        if new_slug != slug:
            target = self.workspace(new_slug)
            if target.exists():
                return None  # a build (or a stray folder) already occupies that slug
            try:
                ws.rename(target)  # same-volume move; the build's .git comes along
            except OSError:
                return None  # locked (open / mid-build) or cross-volume — leave it untouched
            ws = target
        app.name = new_name
        app.slug = new_slug
        self._write_manifest(ws, app)
        return app

    def versions(self, slug: str, limit: int = 5) -> list:
        """The build's recent versions (git commits, newest first) — for the tile's version dropdown. Each
        has .sha, .summary, and .at (creation time). Empty if the build is missing or has no git history."""
        ws = self.workspace(slug)
        if not (ws / MANIFEST).exists():
            return []
        try:
            return self._repo.log(ws, limit)
        except Exception:  # noqa: BLE001 - a git hiccup must never break the menu
            return []

    def revert(self, slug: str, sha: str) -> App | None:
        """Roll a build back to an earlier version, NON-destructively (the revert becomes a new version, so
        nothing is lost and it can be reverted again). Returns the reverted App, or None if the build is
        missing or the revert failed (e.g. a locked/open workspace, or a bad sha)."""
        ws = self.workspace(slug)
        manifest = ws / MANIFEST
        if not manifest.exists():
            return None
        try:
            self._repo.revert_to(ws, sha)
        except Exception:  # noqa: BLE001 - locked/open workspace or bad ref → honest failure
            return None
        try:
            return self._read_manifest(manifest)
        except (OSError, ValueError, KeyError):
            return None

    # ----- helpers -----
    def _detect_entry(self, ws: Path) -> tuple[AppKind, str | None]:
        # A main.py means there's a program to RUN — a task, or an app with a local backend that serves
        # its page AND proxies APIs (needed for anything holding a secret key). Prefer it over a sibling
        # index.html, which for those builds is just a landing page. Pure web apps and 3D models have no
        # main.py, so they still resolve to their index.html.
        if (ws / "main.py").exists():
            return AppKind.PYTHON, "main.py"
        if (ws / "index.html").exists():
            return AppKind.HTML, "index.html"
        html = next(iter(ws.glob("*.html")), None)
        if html:
            return AppKind.HTML, html.name
        py = next((p for p in ws.glob("*.py")), None)
        if py:
            return AppKind.PYTHON, py.name
        return AppKind.UNKNOWN, None

    def _write_manifest(self, ws: Path, app: App) -> None:
        data = {
            "slug": app.slug,
            "name": app.name,
            "request": app.request,
            "kind": app.kind.value,
            "build_kind": app.build_kind.value,
            "entry_point": app.entry_point,
            "created_at": (app.created_at or self._clock.now()).isoformat(),
            "is_model": app.is_model,  # legacy mirror of build_kind == MODEL (back-compat reads)
        }
        self._atomic_write(ws / MANIFEST, json.dumps(data, indent=2))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write-then-rename so a crash/power-loss mid-write can never leave a truncated manifest (which
        would make the build vanish from the menu on the next launch)."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _read_manifest(self, manifest: Path) -> App:
        d = json.loads(manifest.read_text(encoding="utf-8"))
        changed = False
        if "is_model" not in d:  # very old build (predates the flag): classify by the viewer heuristic
            d["is_model"] = self._looks_like_model(manifest.parent, d.get("entry_point"))
            changed = True
        if "build_kind" not in d:  # derive the canonical taxonomy once from the legacy fields, then persist
            if d.get("is_model"):
                d["build_kind"] = BuildKind.MODEL.value
            elif d.get("kind") == AppKind.PYTHON.value:
                d["build_kind"] = BuildKind.TASK.value
            else:
                d["build_kind"] = BuildKind.APP.value
            changed = True
        if changed:
            try:
                self._atomic_write(manifest, json.dumps(d, indent=2))
            except OSError:
                pass  # read-only/locked — fall back to the in-memory value, retry next load
        created = d.get("created_at")
        return App(
            slug=d["slug"],
            name=d["name"],
            request=d.get("request", ""),
            kind=AppKind(d.get("kind", "unknown")),
            build_kind=BuildKind(d.get("build_kind", "app")),
            entry_point=d.get("entry_point"),
            created_at=datetime.fromisoformat(created) if created else None,
        )

    @staticmethod
    def _looks_like_model(ws: Path, entry: str | None) -> bool:
        """Heuristic for legacy builds with no is_model flag: a 3D model embeds a Three.js viewer."""
        target = ws / entry if entry else ws / "index.html"
        try:
            html = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "three.module.js" in html or "three@0" in html or "THREE." in html
