"""The radio's bucket, through the user's own gcloud login (DESKTOP). Same trust as the fleet read:
no keys, no secrets in HELIX; whatever your gcloud can do to the bucket, the radio can.

Reads the catalog, writes it back, copies a file up, copies a file down into the local cache.
Every call returns a plain sentence on failure and never raises. ASCII in everything it prints.
The API for the apps (`helix-radio-api`, signed links, Google sign-in) is a later piece; this is
the radio inside HELIX today.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from helix.adapters.gcloud_fleet import Runner, _real_runner, classify, NOT_INSTALLED
from helix.ports.radio import BAD_CATALOG, NO_BUCKET, NO_CATALOG, Outcome


class GcsRadio:
    def __init__(self, bucket: Callable[[], str | None], *, runner: Runner | None = None,
                 gcloud: str = "gcloud", cache_dir: Path | None = None) -> None:
        self._bucket = bucket
        self._run = runner or _real_runner
        self._gcloud = shutil.which(gcloud) or gcloud
        self._cache = cache_dir or Path(tempfile.gettempdir()) / "helix_radio"

    # ---------------------------------------------------------------- plumbing
    def bucket(self) -> str | None:
        b = (self._bucket() or "").strip().removeprefix("gs://").strip("/")
        return b or None

    def available(self) -> tuple[bool, str | None]:
        if not self.bucket():
            return False, NO_BUCKET
        ran = self._run([self._gcloud, "storage", "ls", f"gs://{self.bucket()}/"], 25.0)
        bad = classify(ran)
        return (True, None) if bad is None else (False, bad[0])

    def _uri(self, key: str) -> str:
        return f"gs://{self.bucket()}/{key.lstrip('/')}"

    # ---------------------------------------------------------------- the catalog
    def read_catalog(self) -> tuple[dict | None, Outcome]:
        if not self.bucket():
            return None, Outcome(False, NO_BUCKET)
        ran = self._run([self._gcloud, "storage", "cat", self._uri("catalog.json")], 30.0)
        if ran.rc != 0:
            low = ((ran.err or "") + (ran.out or "")).lower()
            if "not found" in low or "no such object" in low or "404" in low or "matched no objects" in low:
                return None, Outcome(False, NO_CATALOG)
            bad = classify(ran) or (NOT_INSTALLED, "")
            return None, Outcome(False, bad[0], bad[1])
        try:
            doc = json.loads(ran.out or "{}")
        except ValueError:
            return None, Outcome(False, BAD_CATALOG, (ran.out or "")[:200])
        if not isinstance(doc, dict) or not isinstance(doc.get("tracks", []), list):
            return None, Outcome(False, BAD_CATALOG)
        return doc, Outcome(True)

    def write_catalog(self, doc: dict) -> Outcome:
        if not self.bucket():
            return Outcome(False, NO_BUCKET)
        self._cache.mkdir(parents=True, exist_ok=True)
        tmp = self._cache / "catalog.upload.json"
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")   # ASCII-safe: json escapes
        ran = self._run([self._gcloud, "storage", "cp", str(tmp), self._uri("catalog.json")], 40.0)
        bad = classify(ran)
        return Outcome(True) if bad is None else Outcome(False, bad[0], bad[1])

    # ---------------------------------------------------------------- files
    def upload(self, local: Path, key: str) -> Outcome:
        if not self.bucket():
            return Outcome(False, NO_BUCKET)
        ran = self._run([self._gcloud, "storage", "cp", str(local), self._uri(key)], 600.0)
        bad = classify(ran)
        return Outcome(True) if bad is None else Outcome(False, bad[0], bad[1])

    def fetch(self, key: str) -> tuple[Path | None, Outcome]:
        """The object as a local file in the cache (downloaded once; the cache is by key)."""
        if not self.bucket():
            return None, Outcome(False, NO_BUCKET)
        self._cache.mkdir(parents=True, exist_ok=True)
        local = self._cache / key.replace("/", "__")
        if local.exists() and local.stat().st_size > 0:
            return local, Outcome(True)
        ran = self._run([self._gcloud, "storage", "cp", self._uri(key), str(local)], 600.0)
        bad = classify(ran)
        if bad is not None:
            return None, Outcome(False, bad[0], bad[1])
        if not local.exists():
            return None, Outcome(False, "gcloud said it copied the file, but it is not on disk.")
        return local, Outcome(True)
