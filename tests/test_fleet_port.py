"""The fleet port - helix/ports/fleet.py - is a contract the dream cannot rewrite.

These tests pin its SHAPE: the result types never raise, they carry the human/log split, and the two
protocols are what the adapters and the service code against.
"""
from __future__ import annotations

import inspect
import pathlib

from helix.domain import constitution, fleet
from helix.domain.fleet import Env
from helix.ports import fleet as port


def test_the_port_is_protected_by_prefix():
    assert constitution.is_protected("helix/ports/fleet.py")
    assert not constitution.is_editable("helix/ports/fleet.py")


def test_results_are_frozen_and_never_raise_by_construction():
    svc = fleet.find("MES", Env.DEV)
    r = port.CellRead(service=svc, ok=False, problem="gcloud is not installed.", detail="rc=127")
    assert r.ok is False and r.api is None and r.site is None
    try:
        r.ok = True  # type: ignore[misc]
        assert False, "CellRead must be frozen"
    except Exception:
        pass


def test_problem_and_detail_are_separate_fields():
    """The human sentence and the tool's own words must never share a field, so a caller cannot
    leak one where the other belongs."""
    fields = {f.name for f in port.CellRead.__dataclass_fields__.values()}
    assert {"problem", "detail"} <= fields
    fields = {f.name for f in port.RepoRead.__dataclass_fields__.values()}
    assert {"problem", "detail"} <= fields


def test_a_lineage_event_has_no_way_to_be_edited():
    e = port.FleetEvent(id="x", at=None, company="oats-overnight", app="MES", env="dev",
                        action="deploy", by="mark1", ok=True)
    try:
        e.ok = False  # type: ignore[misc]
        assert False, "FleetEvent must be frozen"
    except Exception:
        pass


def test_reader_protocol_names_its_verbs():
    names = {n for n, _ in inspect.getmembers(port.FleetReader, inspect.isfunction)}
    assert {"available", "read_cell", "read_all"} <= names
    names = {n for n, _ in inspect.getmembers(port.RepoReader, inspect.isfunction)}
    assert {"available", "read_head", "compare"} <= names


def test_state_protocol_has_no_update_or_delete():
    """LINEAGE is append-only. The contract must not even offer a verb that could edit a row."""
    names = {n for n, _ in inspect.getmembers(port.FleetState, inspect.isfunction)}
    assert {"publish", "latest", "record", "history"} <= names
    for banned in ("update", "delete", "remove", "edit", "clear", "purge"):
        assert not any(banned in n for n in names), f"FleetState must not offer {banned!r}"


def test_the_port_imports_only_the_domain():
    src = pathlib.Path(port.__file__).read_text(encoding="utf-8")
    for banned in ("from helix.services", "from helix.adapters", "from helix.ui", "from helix.api",
                   "from helix.app", "import subprocess", "import requests", "import urllib"):
        assert banned not in src
