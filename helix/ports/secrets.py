"""THE VAULT's port: a store of named secrets with versions, in the company's cloud.

THE FORGE kept Databricks secret scopes; HELIX keeps Google Cloud Secret Manager, which the apps
already read at container start (rule 6: a change means a bounce). The port is what a store must
do; the adapter says how. Values go IN through this port and never come back out - there is no
read-a-value method on purpose (plan section 17.6, rule 9: the console never shows a value, ever).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SecretVersion:
    number: int
    state: str          # enabled | disabled | destroyed
    created: str        # ISO stamp from the store, "" when unknown


@dataclass(frozen=True)
class Secret:
    name: str
    created: str
    labels: dict[str, str] = field(default_factory=dict)
    latest: SecretVersion | None = None
    versions_n: int = 0


class SecretStore(Protocol):
    def available(self) -> tuple[bool, str | None]: ...
    def list(self) -> list[Secret]: ...
    def versions(self, name: str) -> list[SecretVersion]: ...
    def create(self, name: str, value: str, labels: dict[str, str]) -> None: ...
    def add_version(self, name: str, value: str) -> int: ...
    def set_version_state(self, name: str, number: int, enabled: bool) -> None: ...
    def destroy(self, name: str) -> None: ...


class StoreError(Exception):
    """One sentence about what the store said, in plain words. Never carries a value."""
