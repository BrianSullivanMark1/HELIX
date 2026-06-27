"""Sandbox guard primitives — the filesystem containment checks the Forge and the self-dev gate share.

Pure functions, no service state: snapshot/restore guarded files, and signature-scan a tree to detect
(and locate) any write that escaped a workspace. Both ForgeService (built apps) and SelfDevService
(self-modification) rely on these to prove a coder wrote ONLY where it was allowed.

This module is safety-critical and is therefore a PROTECTED_FILE in domain/constitution.py — the
self-coder may never edit it. (It lives here, not in selfdev.py, so the Forge can use it without a
services->services import back into the approval gate.)
"""
from __future__ import annotations

from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("sandbox")


def snapshot_files(files: list[Path]) -> dict[str, bytes | None]:
    """Capture the exact bytes of guarded files (or None if absent) before running the coder."""
    snap: dict[str, bytes | None] = {}
    for f in files:
        try:
            snap[str(f)] = f.read_bytes() if f.exists() else None
        except OSError:
            snap[str(f)] = None
    return snap


def scan_tree(root: Path, skip: tuple[Path, ...] = ()) -> dict[str, tuple[int, int]]:
    """mtime+size signature of every file under root, skipping given subtrees. Fast containment check."""
    sig: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return sig
    skip_res = [s.resolve() for s in skip]
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            if (
                p.suffix in (".pyc", ".pyo", ".log")
                or "__pycache__" in p.parts
                or p.name.endswith(("-wal", "-shm", "-journal"))
            ):
                continue  # ignore legitimate churn (bytecode, logs, sqlite sidecars)
            rp = p.resolve()
            if any(rp == s or s in rp.parents for s in skip_res):
                continue
            st = p.stat()
            sig[str(rp)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return sig


def tree_changed(root: Path, before: dict[str, tuple[int, int]], skip: tuple[Path, ...] = ()) -> list[str]:
    """Absolute paths that were added/removed/modified under root since `before`."""
    after = scan_tree(root, skip)
    changed = [p for p in after if after[p] != before.get(p)]
    changed += [p for p in before if p not in after]
    return changed


def restore_if_changed(snapshot: dict[str, bytes | None]) -> list[str]:
    """Revert any guarded file the coder touched (writes into gitignored data/ are invisible to git)."""
    reverted: list[str] = []
    for path, original in snapshot.items():
        f = Path(path)
        try:
            current = f.read_bytes() if f.exists() else None
        except OSError:
            current = None
        if current != original:
            reverted.append(f.name)
            try:
                if original is None:
                    f.unlink(missing_ok=True)
                else:
                    f.write_bytes(original)
            except OSError:
                _LOG.warning("could not revert tampered file %s", f)
    return reverted
