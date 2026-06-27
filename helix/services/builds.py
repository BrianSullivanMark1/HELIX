"""BuildService — the lifecycle of a built app's workspace (data/builds/<slug>/)."""
from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path

from helix.domain.models import App, AppKind, BuildKind, slugify
from helix.ports.clock import Clock
from helix.ports.repo import VersionedRepo

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
        kind, entry = self._detect_entry(ws)
        app.kind, app.entry_point = kind, entry
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
                apps.append(self._read_manifest(manifest))
        return apps

    def categorized(self) -> dict[str, list[App]]:
        """Partition every workspace build by its canonical BuildKind — the single source of truth the
        menu and the orb's list both render, so they always agree. (Agents are a separate substrate and
        are folded in by the caller.) Classification lives here, never in the view."""
        buckets: dict[str, list[App]] = {"apps": [], "tasks": [], "models": []}
        by_kind = {BuildKind.APP: "apps", BuildKind.TASK: "tasks", BuildKind.MODEL: "models"}
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
        app = self._read_manifest(manifest)
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

    # ----- helpers -----
    def _detect_entry(self, ws: Path) -> tuple[AppKind, str | None]:
        if (ws / "index.html").exists():
            return AppKind.HTML, "index.html"
        if (ws / "main.py").exists():
            return AppKind.PYTHON, "main.py"
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
        (ws / MANIFEST).write_text(json.dumps(data, indent=2), encoding="utf-8")

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
                manifest.write_text(json.dumps(d, indent=2), encoding="utf-8")
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
