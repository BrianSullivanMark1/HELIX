"""The radio's storage port: a bucket that holds a catalog and files. One adapter today (gcloud)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

NO_BUCKET = "No radio bucket is set. Open the radio and set the bucket name."
NO_CATALOG = "The bucket has no catalog.json yet - upload the first track and one is written."
BAD_CATALOG = "catalog.json is there but is not the catalog HELIX understands."


@dataclass(frozen=True)
class Outcome:
    ok: bool
    problem: str | None = None
    detail: str | None = None


class RadioStore(Protocol):
    def bucket(self) -> str | None: ...
    def available(self) -> tuple[bool, str | None]: ...
    def read_catalog(self) -> tuple[dict | None, Outcome]: ...
    def write_catalog(self, doc: dict) -> Outcome: ...
    def upload(self, local: Path, key: str) -> Outcome: ...
    def fetch(self, key: str) -> tuple[Path | None, Outcome]: ...
