"""Data safety — legacy-data migration, corrupt-manifest resilience, and the crash watchdog."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta

from helix.adapters import watchdog
from helix.config import AppPaths, migrate_legacy_data
from helix.domain.models import App
from helix.services.builds import BuildService


class _NoRepo:
    def init(self, _ws) -> None: ...
    def commit_all(self, _ws, _msg) -> None: ...


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 1, 12, 0, 0)


def _svc(tmp_path) -> BuildService:
    return BuildService(tmp_path, _NoRepo(), _FixedClock())


def _make(svc: BuildService, name: str) -> App:
    app = App.from_request(name, "x")
    svc.create_workspace(app)
    (svc.workspace(app.slug) / "index.html").write_text("<html></html>", encoding="utf-8")
    return svc.finalize(app)


# ---------- migration ----------

def test_migrate_moves_legacy_data_once(tmp_path):
    old = tmp_path / "app" / "data"
    old.mkdir(parents=True)
    (old / "helix_settings.json").write_text('{"k": 1}', encoding="utf-8")
    new = tmp_path / "localappdata" / "HELIX" / "data"
    migrate_legacy_data(old, new)
    assert (new / "helix_settings.json").read_text(encoding="utf-8") == '{"k": 1}'
    assert not old.exists()  # same-volume rename moved it


def test_migrate_never_overwrites_existing_new_data(tmp_path):
    old = tmp_path / "old-data"
    old.mkdir()
    (old / "helix_settings.json").write_text("old", encoding="utf-8")
    new = tmp_path / "new-data"
    new.mkdir()
    (new / "helix_settings.json").write_text("new", encoding="utf-8")
    migrate_legacy_data(old, new)
    assert (new / "helix_settings.json").read_text(encoding="utf-8") == "new"
    assert old.exists()  # the legacy copy is left alone


def test_migrate_noop_when_no_legacy_data(tmp_path):
    new = tmp_path / "new-data"
    migrate_legacy_data(tmp_path / "missing", new)
    assert not new.exists()


def test_migration_retries_after_a_failed_attempt_left_an_empty_scaffold(tmp_path):
    # A prior failed migration + ensure() leaves an EMPTY builds/ scaffold under new (no marker). The
    # retry must still run and pull the real legacy data in — not treat the scaffold as 'already done'.
    old = tmp_path / "old"
    old.mkdir()
    (old / "helix_settings.json").write_text("real key", encoding="utf-8")
    new = tmp_path / "new"
    (new / "builds").mkdir(parents=True)  # empty scaffold, no marker
    migrate_legacy_data(old, new)
    assert (new / "helix_settings.json").read_text(encoding="utf-8") == "real key"
    assert (new / ".helix-data-migrated").exists()


def test_migration_marker_stops_reruns(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "helix_settings.json").write_text("old", encoding="utf-8")
    new = tmp_path / "new"
    new.mkdir()
    (new / ".helix-data-migrated").write_text("1", encoding="utf-8")
    (new / "helix_settings.json").write_text("current", encoding="utf-8")
    migrate_legacy_data(old, new)
    assert (new / "helix_settings.json").read_text(encoding="utf-8") == "current"  # never clobbered


def test_migration_never_overwrites_an_established_new_dir_without_marker(tmp_path):
    # Even with no marker (an older successful migration), a new dir that already holds real data must be
    # left alone — _has_real_data recognizes it and just records the marker.
    old = tmp_path / "old"
    old.mkdir()
    (old / "helix.db").write_text("OLD DB", encoding="utf-8")
    new = tmp_path / "new"
    new.mkdir()
    (new / "helix.db").write_text("LIVE DB", encoding="utf-8")
    migrate_legacy_data(old, new)
    assert (new / "helix.db").read_text(encoding="utf-8") == "LIVE DB"
    assert (new / ".helix-data-migrated").exists()


def test_dev_paths_keep_repo_local_data():
    paths = AppPaths.resolve()  # tests never run frozen
    assert paths.data == paths.root / "data"


# ---------- corrupt manifests ----------

def test_one_corrupt_manifest_does_not_empty_the_menu(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Good App")
    bad = tmp_path / "bad-app"
    bad.mkdir()
    (bad / ".helixbuild.json").write_text('{"slug": "bad-app", "na', encoding="utf-8")  # truncated
    names = [a.name for a in svc.list()]
    assert names == ["Good App"]


def test_manifest_missing_required_keys_is_skipped(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Good App")
    bad = tmp_path / "half-app"
    bad.mkdir()
    (bad / ".helixbuild.json").write_text(json.dumps({"kind": "html"}), encoding="utf-8")
    assert [a.name for a in svc.list()] == ["Good App"]


def test_rename_of_corrupt_build_fails_honestly(tmp_path):
    svc = _svc(tmp_path)
    bad = tmp_path / "bad-app"
    bad.mkdir()
    (bad / ".helixbuild.json").write_text("not json", encoding="utf-8")
    assert svc.rename("bad-app", "Better Name") is None


def test_manifest_writes_leave_no_tmp_behind(tmp_path):
    svc = _svc(tmp_path)
    app = _make(svc, "Tidy App")
    ws = svc.workspace(app.slug)
    assert not list(ws.glob("*.tmp"))
    data = json.loads((ws / ".helixbuild.json").read_text(encoding="utf-8"))
    assert data["name"] == "Tidy App"


# ---------- watchdog ----------

def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_watchdog_stands_down_after_clean_exit(tmp_path, monkeypatch):
    pid = _dead_pid()  # before the patch — the helper uses the same subprocess module
    spawned = []
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    watchdog.mark_clean_exit(tmp_path)
    assert watchdog.watchdog_main(pid, tmp_path, tmp_path / "main.py", tmp_path) == 0
    assert spawned == []


def test_watchdog_relaunches_after_a_crash(tmp_path, monkeypatch):
    pid = _dead_pid()
    spawned = []
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *a, **k: spawned.append(a[0]))
    watchdog.clear_clean_exit(tmp_path)
    assert watchdog.watchdog_main(pid, tmp_path, tmp_path / "main.py", tmp_path) == 0
    assert len(spawned) == 1
    assert str(tmp_path / "main.py") in spawned[0]


def test_watchdog_caps_rapid_crash_loops(tmp_path, monkeypatch):
    pid = _dead_pid()
    spawned = []
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *a, **k: spawned.append(a[0]))
    now = datetime.now()
    stamps = [(now - timedelta(seconds=s)).isoformat() for s in (30, 60, 90)]
    (tmp_path / watchdog.RELAUNCH_JOURNAL).write_text(json.dumps(stamps), encoding="utf-8")
    assert watchdog.watchdog_main(pid, tmp_path, tmp_path / "main.py", tmp_path) == 1
    assert spawned == []


def test_watchdog_journal_prunes_old_entries(tmp_path):
    old = (datetime.now() - timedelta(hours=2)).isoformat()
    journal = tmp_path / watchdog.RELAUNCH_JOURNAL
    journal.write_text(json.dumps([old, old, old]), encoding="utf-8")
    assert watchdog._too_many_recent_relaunches(journal) is False
    kept = json.loads(journal.read_text(encoding="utf-8"))
    assert len(kept) == 1  # the three stale stamps pruned; this relaunch recorded


def test_clean_exit_sentinel_roundtrip(tmp_path):
    watchdog.mark_clean_exit(tmp_path)
    assert (tmp_path / watchdog.CLEAN_EXIT_SENTINEL).exists()
    watchdog.clear_clean_exit(tmp_path)
    assert not (tmp_path / watchdog.CLEAN_EXIT_SENTINEL).exists()
