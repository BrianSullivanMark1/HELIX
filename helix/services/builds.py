"""BuildService — the lifecycle of a built app's workspace (data/builds/<slug>/)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from helix.domain.models import App, AppKind, slugify
from helix.ports.clock import Clock
from helix.ports.repo import VersionedRepo

MANIFEST = ".helixbuild.json"


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
        ws.mkdir(parents=True, exist_ok=True)
        self._repo.init(ws)
        app.created_at = app.created_at or self._clock.now()  # stamp once; finalize must not reset it
        self._write_manifest(ws, app)
        (ws / "README.md").write_text(f"# {app.name}\n\n{app.request}\n", encoding="utf-8")
        self._repo.commit_all(ws, "scaffold")
        return ws

    def finalize(self, app: App) -> App:
        """After the coder runs: detect how the app runs, persist it, commit the result."""
        ws = self.workspace(app.slug)
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
            manifest = child / MANIFEST
            if manifest.exists():
                apps.append(self._read_manifest(manifest))
        return apps

    def delete(self, slug: str) -> bool:
        ws = self.workspace(slug)
        if not ws.exists():
            return False
        shutil.rmtree(ws, ignore_errors=True)
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
            "entry_point": app.entry_point,
            "created_at": (app.created_at or self._clock.now()).isoformat(),
            "is_model": app.is_model,
        }
        (ws / MANIFEST).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_manifest(self, manifest: Path) -> App:
        d = json.loads(manifest.read_text(encoding="utf-8"))
        if "is_model" not in d:  # legacy build (predates the flag): classify once, then persist
            d["is_model"] = self._looks_like_model(manifest.parent, d.get("entry_point"))
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
            entry_point=d.get("entry_point"),
            created_at=datetime.fromisoformat(created) if created else None,
            is_model=bool(d.get("is_model", False)),
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
