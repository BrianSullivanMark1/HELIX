"""Build version history + NON-DESTRUCTIVE revert (roll a build back to an earlier version, keeping the
newer ones so it can be reverted again). Uses a real git repo."""
from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from helix.adapters.git_repo import GitRepo
from helix.domain.models import App
from helix.services.builds import BuildService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 1, 12, 0, 0)


def _svc(tmp_path) -> BuildService:
    return BuildService(tmp_path, GitRepo(), _Clock())


def test_versions_lists_history_and_revert_is_non_destructive(tmp_path):
    svc = _svc(tmp_path)
    app = App.from_request("Thing", "x")
    svc.create_workspace(app)                                  # "scaffold"
    ws = svc.workspace(app.slug)
    (ws / "index.html").write_text("VERSION ONE", encoding="utf-8")
    svc.finalize(app)                                          # v1: "build: Thing"
    (ws / "index.html").write_text("VERSION TWO broken", encoding="utf-8")
    svc._repo.commit_all(ws, "build: Thing")                  # v2 (the mistake)
    (ws / "index.html").write_text("VERSION THREE", encoding="utf-8")
    svc._repo.commit_all(ws, "build: Thing")                  # v3

    versions = svc.versions(app.slug, 5)
    assert len(versions) == 4                                  # v3, v2, v1, scaffold (newest first)
    assert (ws / "index.html").read_text(encoding="utf-8") == "VERSION THREE"

    v1_sha = versions[2].sha                                   # the VERSION ONE commit
    reverted = svc.revert(app.slug, v1_sha)
    assert reverted is not None
    assert (ws / "index.html").read_text(encoding="utf-8") == "VERSION ONE"  # tree rolled back

    after = svc.versions(app.slug, 10)
    assert len(after) == 5                                     # history GREW — nothing was thrown away
    assert after[0].summary.startswith("revert")              # the revert is itself the newest version
    # and it can be reverted again — v3 is still reachable
    v3_sha = after[1].sha
    assert svc.revert(app.slug, v3_sha) is not None
    assert (ws / "index.html").read_text(encoding="utf-8") == "VERSION THREE"


def test_versions_and_revert_are_safe_on_a_missing_build(tmp_path):
    svc = _svc(tmp_path)
    assert svc.versions("does-not-exist") == []
    assert svc.revert("does-not-exist", "deadbeef") is None
