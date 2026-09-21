"""THE VAULT - the company's secrets, the way THE FORGE kept its Databricks scopes, moved to
Google Cloud Secret Manager.

What the service adds over the store:
  * the board: every secret with its labels (app, env), when it was last rotated, an expiry pill
    derived from a rotation policy (90 days unless a secret says otherwise: never / N days), and
    who reads it - a grep of the linked project folders and the console checkout for the name,
    because Secret Manager records readers nowhere;
  * create and rotate as CURRENT TASKS with an audit row, and the bounce named in the same answer
    (rule 6: apps read a secret at container start, so a new value takes only after a restart);
  * delete behind FOUR conditions, like production: the live gcloud identity on the allowlist, the
    name typed back, Are you sure, and - when something still reads it - a second, explicit
    "delete anyway". The audit row is written before gcloud runs.
Values pass through in memory and are never kept, logged, pushed or echoed.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from helix.domain.fleet import may_touch_production
from helix.ports.secrets import Secret, SecretStore, StoreError

DEFAULT_DAYS = 90
WARN_DAYS = 21
VAULT_DOWN = "The vault is not available in this build."
NO_IDENTITY = "No gcloud account is signed in on this PC, so HELIX cannot say who is asking."
NOT_ALLOWED = "{who} may not delete a secret. Deleting is Brian, Brendan and Kate - the production list."
BAD_TYPED = "Type the secret's name exactly to confirm. Nothing was deleted."
NOT_SURE = "Confirm with Are you sure. Nothing was deleted."
STILL_READ = "{n} place{s} still read{s1} {name}. Deleting it breaks them at their next start. Tick 'delete it anyway' if that is what you want."
EMPTY_VALUE = "The value is empty. Nothing was saved."
BOUNCE = "Apps read a secret when their container starts: redeploy or bounce every service that reads {name} for the new value to take."
NO_STORE = "The vault has no store to talk to."

TEXT_EXT = {".py", ".ps1", ".psm1", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".env", ".toml",
            ".md", ".txt", ".cfg", ".ini", ".sh", ".bat", ".cmd", ".cs", ".html", ".xml", ".properties", ".tf"}
SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", ".idea", ".vs", "bin", "obj", ".next", "coverage"}
MAX_FILES = 4000
MAX_HITS = 40
MAX_BYTES = 1_000_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


class VaultService:
    def __init__(self, store: SecretStore | None, *, policy_path: Path, audit=None, identity: Callable[[], str | None] | None = None,
                 jobs=None, folders: Callable[[], dict[str, str]] | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._policy_path = policy_path
        self._audit = audit
        self._identity = identity or (lambda: None)
        self._jobs = jobs
        self._folders = folders or (lambda: {})
        self._now = clock or _utcnow
        self._lock = threading.Lock()
        self._readers_cache: dict[str, tuple[float, list[dict]]] = {}

    # ---------------------------------------------------------------- policy

    def _policy(self) -> dict[str, dict]:
        try:
            doc = json.loads(self._policy_path.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_policy(self, name: str, days: int | None) -> dict:
        """days=None means never expires; 0/absent means the default."""
        with self._lock:
            pol = self._policy()
            if days is None:
                pol[name] = {"days": None}
            elif int(days) <= 0:
                pol.pop(name, None)
            else:
                pol[name] = {"days": int(days)}
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            self._policy_path.write_text(json.dumps(pol, indent=1), encoding="utf-8")
        return {"ok": True, "name": name, "days": days}

    def _pill(self, s: Secret, pol: dict[str, dict]) -> dict:
        rule = pol.get(s.name) or {}
        days = rule.get("days", DEFAULT_DAYS) if rule else DEFAULT_DAYS
        explicit = bool(rule)
        since = _parse(s.latest.created if s.latest else s.created)
        age_d = int((self._now() - since).total_seconds() // 86400) if since else None
        if days is None:
            return {"age_days": age_d, "policy_days": None, "explicit": explicit, "due_days": None, "state": "never"}
        if age_d is None:
            return {"age_days": None, "policy_days": days, "explicit": explicit, "due_days": None, "state": "unknown"}
        due = int(days) - age_d
        state = "overdue" if due < 0 else "soon" if due <= WARN_DAYS else "fine"
        return {"age_days": age_d, "policy_days": int(days), "explicit": explicit, "due_days": due, "state": state}

    # ---------------------------------------------------------------- the board

    def board(self) -> dict:
        who = self._identity()
        base = {"identity": who, "may_delete": bool(who and may_touch_production(who)),
                "default_days": DEFAULT_DAYS, "warn_days": WARN_DAYS, "secrets": [], "problem": None,
                "project": getattr(self._store, "project", lambda: "")() if self._store is not None else ""}
        if self._store is None:
            return {**base, "problem": NO_STORE}
        ok, why = self._store.available()
        if not ok:
            return {**base, "problem": why}
        try:
            secrets = self._store.list()
        except StoreError as exc:
            return {**base, "problem": str(exc)}
        pol = self._policy()
        rows = []
        for s in secrets:
            rows.append({
                "name": s.name, "created": s.created, "labels": dict(s.labels),
                "app": (s.labels.get("app") or "").upper(), "env": s.labels.get("env") or "",
                "latest": ({"number": s.latest.number, "state": s.latest.state, "created": s.latest.created} if s.latest else None),
                "versions_n": s.versions_n, **self._pill(s, pol),
            })
        return {**base, "secrets": rows}

    def versions(self, name: str) -> dict:
        if self._store is None:
            return {"name": name, "versions": [], "problem": NO_STORE}
        try:
            vs = self._store.versions(name)
        except StoreError as exc:
            return {"name": name, "versions": [], "problem": str(exc)}
        return {"name": name, "versions": [{"number": v.number, "state": v.state, "created": v.created} for v in vs], "problem": None}

    # ---------------------------------------------------------------- who reads it

    def readers(self, name: str, *, fresh: bool = False) -> list[dict]:
        """Every file in a linked project folder (or the console checkout) that names the secret.
        A grep, cached five minutes; Secret Manager itself does not know who reads what."""
        hit = self._readers_cache.get(name)
        if hit and not fresh and time.time() - hit[0] < 300:
            return hit[1]
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        out: list[dict] = []
        for app, folder in self._folders().items():
            root = Path(folder)
            if not root.is_dir():
                continue
            seen = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in filenames:
                    seen += 1
                    if seen > MAX_FILES or len(out) >= MAX_HITS:
                        break
                    p = Path(dirpath) / fn
                    if p.suffix.lower() not in TEXT_EXT and fn not in {"Dockerfile", ".env"}:
                        continue
                    try:
                        if p.stat().st_size > MAX_BYTES:
                            continue
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, ln in enumerate(text.splitlines(), 1):
                        if pat.search(ln):
                            out.append({"app": app, "file": str(p.relative_to(root)).replace("\\", "/"), "line": i})
                            break
                if seen > MAX_FILES or len(out) >= MAX_HITS:
                    break
        self._readers_cache[name] = (time.time(), out)
        return out

    # ---------------------------------------------------------------- writes

    def _row(self, action: str, name: str, **more) -> str | None:
        if self._audit is None:
            return None
        return self._audit.open({"action": action, "secret": name, "who": self._identity() or "", **more})

    def _close(self, rid: str | None, **result) -> None:
        if rid and self._audit is not None:
            self._audit.close(rid, **result)

    def _task(self, kind_title: str, name: str, app: str = "", env: str = ""):
        if self._jobs is None:
            return None
        return self._jobs.start("vault", kind_title, app=app, env=env, by=(self._identity() or "").split("@")[0])

    def create(self, name: str, value: str, *, app: str = "", env: str = "", note: str = "") -> dict:
        if self._store is None:
            return {"ok": False, "error": NO_STORE}
        name = (name or "").strip()
        if not value:
            return {"ok": False, "error": EMPTY_VALUE}
        labels = {k: re.sub(r"[^a-z0-9_-]", "-", v.lower())[:63] for k, v in (("app", app), ("env", env)) if v}
        rid = self._row("secret_create", name, app=app, env=env, note=note[:200])
        job = self._task(f"Create secret {name}", name, app=app.upper(), env=env)
        try:
            self._store.create(name, value, labels)
        except StoreError as exc:
            self._close(rid, ok=False, why=str(exc))
            if job is not None:
                self._jobs.finish(job, False, note=str(exc))
            return {"ok": False, "error": str(exc)}
        self._close(rid, ok=True)
        if job is not None:
            self._jobs.finish(job, True, note="In the vault as version 1")
        self._readers_cache.pop(name, None)
        return {"ok": True, "name": name, "version": 1, "bounce": BOUNCE.format(name=name)}

    def rotate(self, name: str, value: str) -> dict:
        if self._store is None:
            return {"ok": False, "error": NO_STORE}
        if not value:
            return {"ok": False, "error": EMPTY_VALUE}
        rid = self._row("secret_rotate", name)
        job = self._task(f"Rotate secret {name}", name)
        try:
            num = self._store.add_version(name, value)
        except StoreError as exc:
            self._close(rid, ok=False, why=str(exc))
            if job is not None:
                self._jobs.finish(job, False, note=str(exc))
            return {"ok": False, "error": str(exc)}
        self._close(rid, ok=True, version=num)
        if job is not None:
            self._jobs.finish(job, True, note=f"Version {num} is the one apps read now" if num else "A new version is in")
        readers = self.readers(name)
        return {"ok": True, "name": name, "version": num, "bounce": BOUNCE.format(name=name), "readers": readers}

    def set_state(self, name: str, number: int, enabled: bool) -> dict:
        if self._store is None:
            return {"ok": False, "error": NO_STORE}
        rid = self._row("secret_version_enable" if enabled else "secret_version_disable", name, version=number)
        try:
            self._store.set_version_state(name, int(number), enabled)
        except StoreError as exc:
            self._close(rid, ok=False, why=str(exc))
            return {"ok": False, "error": str(exc)}
        self._close(rid, ok=True)
        return {"ok": True, "name": name, "version": int(number), "enabled": enabled}

    def check_delete(self, name: str, *, typed: str, sure: bool, anyway: bool) -> tuple[str | None, list[dict]]:
        """The four conditions, in order; the first one that fails is the answer."""
        who = self._identity()
        if not who:
            return NO_IDENTITY, []
        if not may_touch_production(who):
            return NOT_ALLOWED.format(who=who), []
        if (typed or "").strip() != name:
            return BAD_TYPED, []
        if not sure:
            return NOT_SURE, []
        readers = self.readers(name)
        if readers and not anyway:
            n = len(readers)
            return STILL_READ.format(n=n, s="" if n == 1 else "s", s1="s" if n == 1 else "", name=name), readers
        return None, readers

    def delete(self, name: str, *, typed: str, sure: bool, anyway: bool) -> dict:
        if self._store is None:
            return {"ok": False, "error": NO_STORE}
        why, readers = self.check_delete(name, typed=typed, sure=sure, anyway=anyway)
        if why:
            return {"ok": False, "error": why, "readers": readers}
        rid = self._row("secret_delete", name, readers=len(readers), anyway=bool(readers))
        job = self._task(f"Delete secret {name}", name)
        try:
            self._store.destroy(name)
        except StoreError as exc:
            self._close(rid, ok=False, why=str(exc))
            if job is not None:
                self._jobs.finish(job, False, note=str(exc))
            return {"ok": False, "error": str(exc)}
        self._close(rid, ok=True)
        if job is not None:
            self._jobs.finish(job, True, note="Gone from the vault, every version")
        self._readers_cache.pop(name, None)
        with self._lock:
            pol = self._policy()
            if name in pol:
                pol.pop(name)
                self._policy_path.write_text(json.dumps(pol, indent=1), encoding="utf-8")
        return {"ok": True, "name": name, "readers": readers}
