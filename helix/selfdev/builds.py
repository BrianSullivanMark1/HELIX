"""Per-Build workspaces — each invented app lives in its own git repo under `data/builds/<slug>/`.

This is the load-bearing generalization of the Forge (the self-dev coder): instead of HELIX editing
its OWN source, the coder targets one of these isolated workspaces, so a user-invented app ("a Build")
is fully contained, versioned, and reversible on its own git history — and a Build can never reach back
into the product's own code. `helix/selfdev/coder.py` does the editing; this module owns the workspace
lifecycle (create / list / locate) and the build instruction handed to the coder.

Pure-ish edge module: filesystem + git, no Qt. Settings/UI-free so it stays unit-testable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from helix.core.config import load_config
from helix.selfdev import gitops

# Marker file written into every workspace so a Build is self-describing on disk (and gives git an
# initial commit to branch from). Holds the human name + originating request (the "Blueprint").
MANIFEST_NAME = ".helixbuild.json"


def builds_root() -> Path:
    """The directory that holds every Build's workspace (created on demand)."""
    root = load_config().data_dir / "builds"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(name: str) -> str:
    """A filesystem- and branch-safe slug for a Build name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return slug or "app"


def workspace_dir(slug: str) -> Path:
    return builds_root() / slug


def _unique_slug(name: str) -> str:
    """A slug that does not collide with an existing workspace (appends -2, -3, … if needed)."""
    base = slugify(name)
    slug, n = base, 2
    while workspace_dir(slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_workspace(name: str, *, request: str = "") -> Path:
    """Create a fresh git-backed workspace for a Build and return its path.

    Lays down a manifest (the Build's name + originating request), `git init`s the folder with `main`
    as the default branch, and makes an initial commit so the tree is clean and the coder has a base
    branch to work from. The actual app code is written later by `coder.run_coding_task`."""
    slug = _unique_slug(name)
    ws = workspace_dir(slug)
    ws.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "slug": slug,
        "request": request,            # the Blueprint: what the human asked for
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    (ws / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    gitops.init(str(ws))
    gitops.commit_all(str(ws), f"build: scaffold {slug}")
    return ws


def delete_build(slug: str) -> bool:
    """Permanently delete a Build's workspace (its code + git history). Returns True if removed."""
    import shutil
    ws = workspace_dir(slug)
    if not ws.exists():
        return False
    shutil.rmtree(ws, ignore_errors=True)
    return not ws.exists()


def entry_point(ws: Path) -> dict:
    """Best-effort guess at how to run a Build, for the in-app runner.

    Returns {"kind": "html"|"python"|"none", "path": <file or "">}. Prefers a single HTML file, then a
    conventional Python entry (main.py / app.py / run.py / <slug>.py), then the only .py file present."""
    ws = Path(ws)
    htmls = sorted(p for p in ws.glob("*.html"))
    if htmls:
        preferred = next((p for p in htmls if p.name.lower() in ("index.html", "app.html")), htmls[0])
        return {"kind": "html", "path": str(preferred)}
    pys = [p for p in ws.glob("*.py")]
    if pys:
        slug = read_manifest(ws).get("slug", "")
        names = ["main.py", "app.py", "run.py", f"{slug}.py"]
        preferred = next((ws / n for n in names if (ws / n).exists()), None)
        if preferred is None and len(pys) == 1:
            preferred = pys[0]
        if preferred is not None:
            return {"kind": "python", "path": str(preferred)}
    return {"kind": "none", "path": ""}


def read_manifest(ws: Path) -> dict:
    try:
        return json.loads((Path(ws) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def list_builds() -> list[dict]:
    """Every Build workspace on disk, newest first: [{slug, name, request, created_at, path}]."""
    out: list[dict] = []
    for child in builds_root().iterdir():
        if not child.is_dir():
            continue
        man = read_manifest(child)
        out.append({
            "slug": man.get("slug", child.name),
            "name": man.get("name", child.name),
            "request": man.get("request", ""),
            "created_at": man.get("created_at", ""),
            "path": str(child),
        })
    out.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return out


def build_app(name: str, request: str, *, on_step=None, merge: bool = True):
    """Invent a standalone app end-to-end: make its workspace, have the coder write it, land the code.

    Returns (workspace_path, CoderResult). The code is written on a work branch inside the workspace;
    when `merge` is True it is merged to the workspace's `main` (a --no-ff, revertible commit) so the
    Build's History has a version to show and roll back to."""
    from helix.selfdev import coder  # lazy: keep this module import-light

    ws = create_workspace(name, request=request)
    result = coder.run_coding_task(
        task=f"Build app: {name}",
        repo_dir=str(ws),
        prompt=build_app_prompt(name, request),
        on_step=on_step,
    )
    if result.ok and merge and result.branch:
        try:
            gitops.merge_to(str(ws), result.branch, into=result.base or "main", message=f"build: {name}")
        except gitops.GitError:
            pass
    return ws, result


def build_app_prompt(name: str, request: str) -> str:
    """The instruction handed to the headless coder to BUILD A STANDALONE APP in its own workspace.

    Unlike the self-improvement prompt (which edits HELIX itself), this targets the empty Build
    workspace as the current directory, so the model is free to create whatever files the app needs."""
    return (
        "You are building a brand-new, standalone application for a user, from scratch. The current "
        "working directory is an empty git repository dedicated to THIS app — create whatever files it "
        "needs here. Do not assume any framework or prior code exists.\n\n"
        f"APP NAME: {name.strip()}\n"
        f"WHAT THE USER WANTS:\n{request.strip()}\n\n"
        "Rules:\n"
        "- Build the simplest thing that genuinely satisfies the request, and make it actually run.\n"
        "- Prefer a single self-contained program with no third-party dependencies when reasonable "
        "(Python stdlib, or a single HTML file). If you must add dependencies, list them in a README.\n"
        "- Stay INSIDE this directory. Never write outside it, never use absolute or parent (..) paths, "
        "and never touch the user's wider system.\n"
        "- Do NOT run `git commit`, `git push`, or any git command — the workspace handles version "
        "control. Just create and edit files.\n"
        "- Add a short README.md explaining what the app does and exactly how to run it.\n"
        "- When done, summarize in a few sentences what you built and how to run it."
    )
