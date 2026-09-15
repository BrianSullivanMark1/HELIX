"""CatalogStore (helix/adapters/sap_catalog.py): the three layers — shipped tables read as package
data, extensions that shadow them, the wide sqlite index inflated once on first use — the index
and its search, and the leanx.eu fetcher with its throttle, its remembered misses and its off
switch. No network anywhere: `urlopen` is a fake serving inline pages, the parser is a fake
except in the one round-trip test that runs the real one when it exists."""
from __future__ import annotations

import email.message
import gzip
import json
import logging
import re
import sqlite3
import urllib.error
from datetime import date
from pathlib import Path

import pytest

import helix
from helix.adapters import sap_catalog
from helix.adapters.sap_catalog import FETCH_MAX_BYTES, USER_AGENT, CatalogStore, shipped_dir
from helix.domain.sap import leanx_parse
from helix.domain.sap.model import FieldDef, FkRef, TableDef

LOGGER = "helix.sap.catalog"


# ----- fixtures: tables, a shipped folder, a wide index, a fake web -----

def _fd(name, description, key=False, data_type="CHAR", length=10, check_table="", position=0):
    return FieldDef(name=name, description=description, key=key, data_type=data_type,
                    length=length, check_table=check_table, position=position)


def _table(name, description, fields=(), fks=(), *, module="PP", source="leanx",
           fetched_at="2026-09-01") -> TableDef:
    return TableDef(name=name, description=description, module=module, fields=tuple(fields),
                    foreign_keys=tuple(fks), source=source,
                    source_url=f"https://leanx.eu/sap/table/{name.lower()}/", fetched_at=fetched_at)


AFRU = _table("AFRU", "Order Confirmations", (
    _fd("MANDT", "Client", True, "CLNT", 3, "T000", 1),
    _fd("RUECK", "Completion confirmation number", True, "NUMC", 10, "", 2),
    _fd("RMZHL", "Confirmation counter", True, "NUMC", 8, "", 3),
    _fd("AUFPL", "Routing number of operations", False, "NUMC", 10, "", 4),
    _fd("APLZL", "General counter for order", False, "NUMC", 8, "", 5),
), (FkRef(field="APLZL", check_table="AFVC", kind="KEY",
          columns=(("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL"))),))
AUFK = _table("AUFK", "Order master data", (
    _fd("MANDT", "Client", True, "CLNT", 3), _fd("AUFNR", "Order number", True, "CHAR", 12),
    _fd("OBJNR", "Object number", False, "CHAR", 22),
))
AFKO = _table("AFKO", "Order header data PP orders", (
    _fd("MANDT", "Client", True), _fd("AUFNR", "Order number", True),
    _fd("AUFPL", "Routing number"),
))
JEST = _table("JEST", "Individual Object Status", (
    _fd("MANDT", "Client", True), _fd("OBJNR", "Object number", True),
    _fd("STAT", "Object status", True), _fd("INACT", "Indicator: status is inactive"),
), module="CA")


def _write_gz(path: Path, table: TableDef) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(table.to_dict(), fh)


def _shipped(tmp_path: Path, *tables: TableDef, index: bool = True) -> Path:
    root = tmp_path / "shipped"
    (root / "tables").mkdir(parents=True, exist_ok=True)
    for t in tables:
        _write_gz(root / "tables" / f"{t.name}.json.gz", t)
    if index:
        rows = [{"name": t.name, "description": t.description, "module": t.module,
                 "component": t.component, "keys": list(t.primary_key()),
                 "fields": len(t.fields), "source": t.source} for t in tables]
        (root / "index.json").write_text(
            json.dumps({"v": 1, "built_at": "2026-09-14", "tables": rows}), "utf-8")
    return root


def _store(tmp_path: Path, *tables: TableDef, index: bool = True) -> CatalogStore:
    return CatalogStore(tmp_path / "data", shipped=_shipped(tmp_path, *tables, index=index))


_WIDE_SCHEMA = """
CREATE TABLE tables(name TEXT PRIMARY KEY, description TEXT, category TEXT, delivery_class TEXT,
                    module TEXT, component TEXT);
CREATE TABLE fields(table_name TEXT, position INTEGER, name TEXT, description TEXT,
                    data_element TEXT, domain TEXT, data_type TEXT, length INTEGER,
                    decimals INTEGER, check_table TEXT);
CREATE TABLE fk_hints(table_name TEXT, field TEXT, check_table TEXT, check_field TEXT, kind TEXT,
                      cardinality TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""
# No indexes here on purpose: the shipped gz carries none (a third of its size), and the store
# is expected to create them itself on first use.
_WIDE_TABLES = [
    ("CRHD", "Work Center Header", "TRANSP", "A", "PP", "PP-BD-WKC"),
    ("CRTX", "Text for the Work Center or Production Resource/Tool", "TRANSP", "A", "PP", ""),
    ("MARA", "General Material Data", "TRANSP", "A", "LO", "LO-MD-MM"),
    ("T001W", "Plants/Branches", "TRANSP", "C", "LO", ""),
    ("KAKO", "Capacity Header Segment", "TRANSP", "A", "PP", ""),
    ("AFVGD", "Order operation (structure)", "STRUCT", "", "PP", ""),
    ("PLPO", "Task list - operation/activity", "TRANSP", "A", "PP", ""),
    ("ZZT", "Decoy", "TRANSP", "A", "", ""),
]
_WIDE_FIELDS = [
    ("CRHD", 1, "MANDT", "Client", "MANDT", "MANDT", "CLNT", 3, 0, "T000"),
    ("CRHD", 2, "OBJTY", "Object types of the CIM resource", "CR_OBJTY", "CR_OBJTY", "CHAR", 2, 0,
     ""),
    ("CRHD", 3, "OBJID", "Object ID of the resource", "CR_OBJID", "CR_OBJID", "NUMC", 8, 0, ""),
    ("CRHD", 4, "ARBPL", "Work center", "ARBPL", "ARBPL", "CHAR", 8, 0, ""),
    ("CRHD", 5, "WERKS", "Plant", "WERKS_D", "WERKS", "CHAR", 4, 0, "T001W"),
    ("CRTX", 1, "MANDT", "Client", "MANDT", "MANDT", "CLNT", 3, 0, "T000"),
    ("CRTX", 2, "OBJTY", "Object types of the CIM resource", "CR_OBJTY", "CR_OBJTY", "CHAR", 2, 0,
     ""),
    ("CRTX", 3, "OBJID", "Object ID of the resource", "CR_OBJID", "CR_OBJID", "NUMC", 8, 0, ""),
    ("CRTX", 4, "SPRAS", "Language Key", "SPRAS", "SPRAS", "LANG", 1, 0, "T002"),
    ("CRTX", 5, "KTEXT", "Description", "CR_KTEXT", "TEXT40", "CHAR", 40, 0, ""),
    ("MARA", 1, "MANDT", "Client", "MANDT", "MANDT", "CLNT", 3, 0, "T000"),
    ("MARA", 2, "MATNR", "Material Number", "MATNR", "MATNR", "CHAR", 18, 0, ""),
    ("KAKO", 3, "ARBPL_K", "Work center key of the capacity", "ARBPL", "ARBPL", "CHAR", 8, 0, ""),
    ("AFVGD", 7, "XARBPL", "Work center (external)", "ARBPL", "ARBPL", "CHAR", 8, 0, ""),
    ("PLPO", 5, "ARBID", "Work center object id (see ARBPL)", "CR_OBJID", "CR_OBJID", "NUMC", 8, 0,
     ""),
    ("ZZT", 1, "ARBPLXK", "an underscore is not a wildcard", "", "", "CHAR", 8, 0, ""),
]
_WIDE_FKS = [
    ("CRHD", "WERKS", "T001W", "WERKS", "REF", "1:CN"),
    ("CRHD", "MANDT", "T000", "MANDT", "KEY", ""),
]


def _wide_gz(shipped: Path, *, extra_table: tuple | None = None) -> Path:
    """Build the agreed wide schema in a scratch sqlite file, gzip it to shipped/wide.sqlite.gz."""
    db = shipped / "wide.build.sqlite"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_WIDE_SCHEMA)
        rows = _WIDE_TABLES + ([extra_table] if extra_table else [])
        conn.executemany("INSERT INTO tables VALUES (?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO fields VALUES (?,?,?,?,?,?,?,?,?,?)", _WIDE_FIELDS)
        conn.executemany("INSERT INTO fk_hints VALUES (?,?,?,?,?,?)", _WIDE_FKS)
        conn.executemany("INSERT INTO meta VALUES (?,?)",
                         [("source", "svn11x"), ("built_at", "2026-09-14")])
        conn.commit()
    finally:
        conn.close()
    gz = shipped / "wide.sqlite.gz"
    with open(db, "rb") as src, gzip.open(gz, "wb") as out:
        out.write(src.read())
    db.unlink()
    return gz


class _Resp:
    """The slice of an http.client.HTTPResponse the fetcher touches."""

    def __init__(self, body: bytes, status: int = 200, charset: str = "utf-8") -> None:
        self._body = body
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Web:
    """A fake leanx: `pages` maps a URL to HTML bytes (or an exception to raise); any other URL is
    a 404, as leanx answers for a table it does not know."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.requests: list[tuple[str, str | None, float | None]] = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_header("User-agent"), timeout))
        body = self.pages.get(req.full_url)
        if body is None:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        if isinstance(body, Exception):
            raise body
        return _Resp(body)


def _url(name: str) -> str:
    return f"https://leanx.eu/sap/table/{name.lower()}/"


TABLE_PAGE = "<h1>SAP Table {name}</h1><h2>{desc}</h2><h3>{name} table fields</h3>"
SEARCH_PAGE = "<h1>Search SAP tables</h1><form>...</form>"


@pytest.fixture
def web(monkeypatch):
    fake = _Web({_url("AFRU"): TABLE_PAGE.format(name="AFRU", desc="Order Confirmations").encode()})
    monkeypatch.setattr(sap_catalog, "urlopen", fake)
    return fake


@pytest.fixture
def fake_parser(monkeypatch):
    """A stand-in for domain.sap.leanx_parse so the store's plumbing is tested on its own."""
    def parse_table(html, *, fetched_at="", source_url=""):
        m = re.search(r"SAP Table (\w+)", html)
        if not m:
            raise ValueError("not a table page")
        desc = re.search(r"<h2>(.*?)</h2>", html)
        return TableDef(name=m.group(1), description=desc.group(1) if desc else "",
                        fields=(_fd("MANDT", "Client", True), _fd("RUECK", "Number", True)),
                        source="leanx", source_url=source_url, fetched_at=fetched_at)

    monkeypatch.setattr(leanx_parse, "table_url", _url)
    monkeypatch.setattr(leanx_parse, "is_table_page", lambda html: "SAP Table" in html)
    monkeypatch.setattr(leanx_parse, "parse_table", parse_table)


@pytest.fixture
def clock(monkeypatch):
    """A frozen monotonic clock and a sleep that only advances it — so the throttle is observable
    and the suite never actually waits."""
    state = {"t": 100.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        state["t"] += s

    monkeypatch.setattr(sap_catalog, "_monotonic", lambda: state["t"])
    monkeypatch.setattr(sap_catalog, "_sleep", sleep)
    monkeypatch.setattr(sap_catalog._THROTTLE, "last", None)
    return state


# ----- package data -----

def test_shipped_dir_is_package_relative_and_holds_the_tables_folder():
    assert shipped_dir() == Path(helix.__file__).resolve().parent / "sapcatalog"
    assert (shipped_dir() / "tables").is_dir()
    assert (shipped_dir() / "README.md").is_file()


# ----- layers 1 + 2 -----

def test_a_shipped_table_is_read_from_package_data_and_cached(tmp_path):
    store = _store(tmp_path, AFRU, AUFK)
    table = store.get("afru")
    assert table == AFRU and table.primary_key() == ("MANDT", "RUECK", "RMZHL")
    assert store.get("TV_AFRU") is table                   # the EDW view prefix is dropped
    assert store.has("AUFK") and not store.has("NOPE") and store.get("") is None
    (tmp_path / "shipped" / "tables" / "AFRU.json.gz").unlink()
    assert store.get("AFRU") is table                      # served from memory after the first read
    assert store.built_at() == "2026-09-14"


def test_an_extension_shadows_the_shipped_table_and_survives_a_restart(tmp_path):
    store = _store(tmp_path, AFRU, AUFK)
    assert store.get("AFRU").description == "Order Confirmations"
    newer = _table("AFRU", "Order Confirmations (refetched)", AFRU.fields, AFRU.foreign_keys,
                   fetched_at="2026-09-15")
    store.put(newer)
    assert store.get("AFRU") is newer
    assert (tmp_path / "data" / "sap" / "tables" / "AFRU.json.gz").is_file()
    assert store.extension_names() == {"AFRU"} and store.shipped_names() == {"AFRU", "AUFK"}
    row = next(r for r in store.index() if r["name"] == "AFRU")
    assert row["description"] == "Order Confirmations (refetched)" and row["layer"] == "extension"

    reopened = CatalogStore(tmp_path / "data", shipped=tmp_path / "shipped")
    assert reopened.get("AFRU") == newer                   # layer 1 wins on disk too
    assert reopened.get("AUFK") == AUFK


def test_names_and_index_cover_both_layers_sorted(tmp_path):
    store = _store(tmp_path, AFRU, JEST)
    store.put(_table("ZZNEW", "A custom table", (_fd("MANDT", "Client", True),), module=""))
    assert store.names() == ("AFRU", "JEST", "ZZNEW")
    rows = store.index()
    assert [r["name"] for r in rows] == ["AFRU", "JEST", "ZZNEW"]
    afru = rows[0]
    assert afru == {"name": "AFRU", "description": "Order Confirmations", "module": "PP",
                    "component": "", "keys": ["MANDT", "RUECK", "RMZHL"], "fields": 5,
                    "source": "leanx", "layer": "shipped"}
    assert rows[2]["keys"] == ["MANDT"] and rows[2]["fields"] == 1
    assert rows[2]["layer"] == "extension"


def test_search_index_ranks_exact_then_prefix_then_contains_then_description(tmp_path):
    store = _store(tmp_path, AFRU, AUFK, AFKO, JEST)
    assert [r["name"] for r in store.search_index("AFRU")] == ["AFRU"]
    assert [r["name"] for r in store.search_index("af")] == ["AFKO", "AFRU"]
    assert [r["name"] for r in store.search_index("tv_afko")] == ["AFKO"]
    # 'order' is description-only for all three, so they come alphabetically; 'fk' sits INSIDE
    # two names (rank 'contains'), still ahead of any description-only hit.
    assert [r["name"] for r in store.search_index("order")] == ["AFKO", "AFRU", "AUFK"]
    assert [r["name"] for r in store.search_index("fk")] == ["AFKO", "AUFK"]
    assert [r["name"] for r in store.search_index("object status")] == ["JEST"]
    assert [r["name"] for r in store.search_index("order confirmations")] == ["AFRU"]
    assert len(store.search_index("order", limit=2)) == 2
    assert store.search_index("") == () and store.search_index("nothing here") == ()


def test_a_missing_or_corrupt_index_is_rebuilt_from_the_files_and_junk_rows_skipped(
        tmp_path, caplog):
    store = _store(tmp_path, AFRU, AUFK, index=False)
    assert [r["name"] for r in store.index()] == ["AFRU", "AUFK"]
    assert store.names() == ("AFRU", "AUFK") and store.built_at() == ""

    shipped = _shipped(tmp_path / "second", AFRU, AUFK)
    (shipped / "index.json").write_text("{not json", "utf-8")
    (shipped / "tables" / "BROKEN.json.gz").write_bytes(b"not gzip at all")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store2 = CatalogStore(tmp_path / "second" / "data", shipped=shipped)
        assert store2.get("BROKEN") is None
        assert [r["name"] for r in store2.index()] == ["AFRU", "AUFK"]
    assert any("unreadable" in m for m in caplog.messages)

    shipped3 = _shipped(tmp_path / "third", AFRU)
    (shipped3 / "index.json").write_text(json.dumps(
        {"v": 1, "tables": [42, {"description": "no name"}, {"name": "aufk", "keys": "MANDT",
                                                              "fields": "many"}]}), "utf-8")
    rows = CatalogStore(tmp_path / "third" / "data", shipped=shipped3).index()
    assert [(r["name"], r["keys"], r["fields"]) for r in rows] == [
        ("AFRU", ["MANDT", "RUECK", "RMZHL"], 5),   # the file backstops the row the index lacks
        ("AUFK", [], 0),                             # junk shapes read as empty, not as a crash
    ]


def test_namespaced_table_names_are_safe_file_names(tmp_path):
    store = _store(tmp_path, AFRU)
    store.put(_table("/BI0/TCUST", "Namespaced", (_fd("MANDT", "Client", True),), module=""))
    folder = tmp_path / "data" / "sap" / "tables"
    assert (folder / "%2FBI0%2FTCUST.json.gz").is_file()
    assert [p.name for p in folder.iterdir()] == ["%2FBI0%2FTCUST.json.gz"]   # no subfolders
    assert store.names() == ("/BI0/TCUST", "AFRU")
    reopened = CatalogStore(tmp_path / "data", shipped=tmp_path / "shipped")
    assert reopened.get("/bi0/tcust") is not None


def test_put_keeps_serving_from_memory_when_the_disk_write_fails(tmp_path, caplog):
    store = _store(tmp_path, AFRU)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sap").write_text("a file where the folder should be", "utf-8")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store.put(AUFK)
    assert store.get("AUFK") == AUFK and any("persist" in m for m in caplog.messages)
    with pytest.raises(ValueError):
        store.put(_table("", "nameless"))


# ----- the wide index -----

def test_the_wide_index_is_inflated_once_and_describes_a_table_without_keys(tmp_path):
    store = _store(tmp_path, AFRU)
    _wide_gz(tmp_path / "shipped")
    assert store.wide_available() is True
    db = tmp_path / "data" / "sap" / "wide.sqlite"
    assert db.is_file()
    conn = sqlite3.connect(str(db))
    try:  # the shipped file has no indexes; the store built them on inflate
        made = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        conn.close()
    assert {"ix_tables_name", "ix_fields_name", "ix_fields_table"} <= made
    crhd = store.describe_wide("tv_crhd")
    assert crhd is not None and crhd.source == "svn11x" and crhd.description == "Work Center Header"
    assert crhd.category == "TRANSP" and crhd.module == "PP" and crhd.component == "PP-BD-WKC"
    assert crhd.field_names() == ("MANDT", "OBJTY", "OBJID", "ARBPL", "WERKS")
    assert crhd.primary_key() == () and not any(f.key for f in crhd.fields)
    werks = crhd.field("WERKS")
    assert werks.check_table == "T001W" and werks.type_label() == "CHAR(4)" and werks.position == 5
    assert crhd.foreign_keys == (
        FkRef(field="MANDT", check_table="T000", columns=(("MANDT", "MANDT"),), partial=True,
              kind="KEY"),
        FkRef(field="WERKS", check_table="T001W", columns=(("WERKS", "WERKS"),), partial=True,
              kind="REF", cardinality="1:CN"),
    )
    assert store.describe_wide("NOPE") is None and store.describe_wide("") is None
    assert store.get("CRHD") is None                       # layer 3 never leaks into get()


def test_the_wide_index_is_not_reinflated_unless_the_shipped_file_changes(tmp_path, monkeypatch):
    calls = []
    real = sap_catalog._inflate
    monkeypatch.setattr(sap_catalog, "_inflate", lambda gz, dst: (calls.append(gz), real(gz, dst)))
    shipped = _shipped(tmp_path, AFRU)
    _wide_gz(shipped)
    data = tmp_path / "data"
    assert CatalogStore(data, shipped=shipped).wide_available() and len(calls) == 1
    again = CatalogStore(data, shipped=shipped)
    assert again.wide_available() and again.wide_available() and len(calls) == 1
    _wide_gz(shipped, extra_table=("ZNEW", "A table a newer build added", "TRANSP", "A", "", ""))
    fresh = CatalogStore(data, shipped=shipped)
    assert fresh.wide_available() and len(calls) == 2
    assert fresh.describe_wide("ZNEW").description == "A table a newer build added"


def test_wide_field_search_ranks_exact_prefix_contains_then_description(tmp_path):
    store = _store(tmp_path, AFRU)
    _wide_gz(tmp_path / "shipped")
    hits = store.search_wide_fields(["arbpl"])
    # exact; the two prefixes in table order; the name that merely contains it; description only
    assert [(h["table"], h["field"]) for h in hits] == [
        ("CRHD", "ARBPL"), ("KAKO", "ARBPL_K"), ("ZZT", "ARBPLXK"), ("AFVGD", "XARBPL"),
        ("PLPO", "ARBID"),
    ]
    assert hits[0] == {"table": "CRHD", "table_description": "Work Center Header",
                       "field": "ARBPL", "description": "Work center", "data_type": "CHAR",
                       "check_table": ""}
    # '_' is a letter in a SAP name, not a LIKE wildcard: ARBPLXK must not match
    assert [h["field"] for h in store.search_wide_fields("ARBPL_K")] == ["ARBPL_K"]
    both = store.search_wide_fields(["work", "center"])
    assert {h["field"] for h in both} == {"ARBPL", "ARBPL_K", "XARBPL", "ARBID"}
    assert len(store.search_wide_fields(["work", "center"], limit=2)) == 2
    assert store.search_wide_fields(["work", "center", "unicorn"]) == ()
    assert store.search_wide_fields([]) == () and store.search_wide_fields(["x"], limit=0) == ()


def test_wide_table_search_ranks_like_the_field_search(tmp_path):
    store = _store(tmp_path, AFRU)
    _wide_gz(tmp_path / "shipped")
    assert [t["name"] for t in store.search_wide_tables(["mara"])] == ["MARA"]
    assert [t["name"] for t in store.search_wide_tables(["cr"])] == ["CRHD", "CRTX"]
    assert [t["name"] for t in store.search_wide_tables("work center")] == ["CRHD", "CRTX"]
    assert store.search_wide_tables(["plants"])[0] == {
        "name": "T001W", "description": "Plants/Branches", "category": "TRANSP",
        "delivery_class": "C", "module": "LO", "component": ""}
    assert store.search_wide_tables(["nothing", "matches"]) == ()


def test_without_a_wide_index_everything_else_still_works(tmp_path, caplog):
    store = _store(tmp_path, AFRU)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert store.wide_available() is False and store.wide_available() is False
    assert store.describe_wide("CRHD") is None
    assert store.search_wide_fields(["ARBPL"]) == () and store.search_wide_tables(["CRHD"]) == ()
    assert store.get("AFRU") == AFRU
    assert sum("no wide SAP index" in m for m in caplog.messages) == 1


def test_a_broken_wide_index_is_turned_off_and_logged_once(tmp_path, caplog):
    store = _store(tmp_path, AFRU)
    (tmp_path / "shipped" / "wide.sqlite.gz").write_bytes(b"this is not a gzip stream")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.wide_available() is False
        assert store.describe_wide("CRHD") is None
        assert store.search_wide_fields(["ARBPL"]) == ()
    warnings = [m for m in caplog.messages if "wide SAP index is unavailable" in m]
    assert len(warnings) == 1

    # A gz that inflates to a database without the agreed tables is just as unavailable.
    db = tmp_path / "other.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nope(x)")
    conn.commit()
    conn.close()
    shipped2 = _shipped(tmp_path / "two", AFRU)
    with gzip.open(shipped2 / "wide.sqlite.gz", "wb") as out:
        out.write(db.read_bytes())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert CatalogStore(tmp_path / "two" / "data", shipped=shipped2).wide_available() is False
    assert any("lacks" in m for m in caplog.messages)


# ----- fetching (fake web, fake parser) -----

def test_fetch_parses_stores_and_serves_the_table(tmp_path, web, fake_parser, clock):
    store = _store(tmp_path, AUFK)
    table = store.fetch_table("afru", today="2026-09-15")
    assert table is not None and table.name == "AFRU" and table.description == "Order Confirmations"
    assert table.source == "leanx" and table.fetched_at == "2026-09-15"
    assert table.source_url == _url("AFRU")
    assert table.module == "PP"                            # curated.SCOPE files AFRU under PP
    assert web.requests == [(_url("AFRU"), USER_AGENT, sap_catalog.FETCH_TIMEOUT_S)]
    assert store.get("AFRU") is table                      # in the cache…
    assert (tmp_path / "data" / "sap" / "tables" / "AFRU.json.gz").is_file()   # …and in layer 1
    assert CatalogStore(tmp_path / "data", shipped=tmp_path / "shipped").get("AFRU") == table
    row = next(r for r in store.index() if r["name"] == "AFRU")
    assert row["layer"] == "extension" and row["keys"] == ["MANDT", "RUECK"]


def test_fetch_stamps_today_when_no_date_is_given(tmp_path, web, fake_parser, clock):
    table = _store(tmp_path).fetch_table("AFRU")
    assert table is not None and table.fetched_at == date.today().isoformat()


def test_a_404_or_a_search_page_is_a_miss_remembered_for_the_session(tmp_path, web, fake_parser,
                                                                     clock, caplog):
    store = _store(tmp_path)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert store.fetch_table("NOPE") is None
        assert store.fetch_table("nope") is None
    assert len(web.requests) == 1                          # the second call never left the store
    assert sum("no table page for NOPE" in m for m in caplog.messages) == 1

    web.pages[_url("AFVGD")] = SEARCH_PAGE.encode()        # 200, but not a table page
    assert store.fetch_table("AFVGD") is None and store.fetch_table("AFVGD") is None
    assert len(web.requests) == 2

    # A fresh store has no memory of misses — they are session-level, not persisted.
    assert CatalogStore(tmp_path / "data", shipped=tmp_path / "shipped").fetch_table("NOPE") is None
    assert len(web.requests) == 3


def test_a_network_error_or_server_error_is_not_remembered(tmp_path, web, fake_parser, clock,
                                                           caplog):
    store = _store(tmp_path)
    web.pages[_url("AFKO")] = urllib.error.URLError("name resolution failed")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.fetch_table("AFKO") is None
        assert store.fetch_table("AFKO") is None
    assert len(web.requests) == 2 and any("could not reach" in m for m in caplog.messages)

    web.pages[_url("AFKO")] = urllib.error.HTTPError(_url("AFKO"), 503, "Busy", None, None)
    assert store.fetch_table("AFKO") is None and len(web.requests) == 3
    web.pages[_url("AFKO")] = TimeoutError("timed out")
    assert store.fetch_table("AFKO") is None and len(web.requests) == 4

    web.pages[_url("AFKO")] = TABLE_PAGE.format(name="AFKO", desc="Order header").encode()
    assert store.fetch_table("AFKO").description == "Order header"   # once the site is back


def test_fetching_can_be_switched_off(tmp_path, web, fake_parser, clock):
    store = _store(tmp_path)
    assert store.fetch_allowed() is True
    store.set_fetch_allowed(False)
    assert store.fetch_allowed() is False
    assert store.fetch_table("AFRU") is None and web.requests == []
    store.set_fetch_allowed(True)
    assert store.fetch_table("AFRU") is not None and len(web.requests) == 1


def test_fetches_are_spaced_a_second_apart(tmp_path, web, fake_parser, clock):
    store = _store(tmp_path)
    web.pages[_url("AFKO")] = TABLE_PAGE.format(name="AFKO", desc="Order header").encode()
    web.pages[_url("AFVC")] = TABLE_PAGE.format(name="AFVC", desc="Operation").encode()
    store.fetch_table("AFRU")
    assert clock["sleeps"] == []                           # the first request goes straight out
    clock["t"] += 0.25
    store.fetch_table("AFKO")
    assert clock["sleeps"] == [pytest.approx(0.75)]        # topped up to one second
    clock["t"] += 2.0
    store.fetch_table("AFVC")
    assert len(clock["sleeps"]) == 1                       # two seconds later: no wait
    assert len(web.requests) == 3


def test_an_oversized_page_is_ignored_and_not_remembered(tmp_path, web, fake_parser, clock,
                                                         caplog):
    store = _store(tmp_path)
    web.pages[_url("BIG")] = b"<h1>SAP Table BIG</h1>" + b"x" * FETCH_MAX_BYTES
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.fetch_table("BIG") is None
        assert store.fetch_table("BIG") is None
    assert len(web.requests) == 2 and any("exceeds" in m for m in caplog.messages)


def test_fetch_never_raises_when_the_parser_does(tmp_path, web, fake_parser, clock, monkeypatch,
                                                 caplog):
    store = _store(tmp_path)

    def broken(html, **_kw):
        raise ValueError("bad page")

    monkeypatch.setattr(leanx_parse, "parse_table", broken)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.fetch_table("AFRU") is None
    assert any("fetching the SAP table AFRU failed" in m for m in caplog.messages)
    assert store.get("AFRU") is None


def test_only_leanx_over_https_is_ever_contacted(tmp_path, web, fake_parser, clock, monkeypatch,
                                                 caplog):
    store = _store(tmp_path)
    for bad in ("http://leanx.eu/sap/table/afru/", "https://evil.example/sap/table/afru/",
                "https://leanx.eu.evil.example/x/"):
        monkeypatch.setattr(leanx_parse, "table_url", lambda name, bad=bad: bad)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert store.fetch_table("AFRU") is None
    assert web.requests == []
    assert sum("refusing to fetch" in m for m in caplog.messages) == 3


def test_a_page_in_another_charset_is_decoded_by_its_header(tmp_path, fake_parser, clock,
                                                            monkeypatch):
    body = TABLE_PAGE.format(name="AFRU", desc="Rückmeldungen").encode("latin-1")

    class _Latin(_Web):
        def __call__(self, req, timeout=None):
            self.requests.append((req.full_url, None, timeout))
            return _Resp(body, charset="iso-8859-1")

    monkeypatch.setattr(sap_catalog, "urlopen", _Latin({}))
    assert _store(tmp_path).fetch_table("AFRU").description == "Rückmeldungen"


# ----- the real parser, when it exists -----

LEANX_PAGE = """<!DOCTYPE html><html><head><title>SAP Table AFRU</title></head><body>
<h1 class="text-3xl font-bold">SAP Table AFRU</h1>
<h2 class="text-xl">Order Confirmations</h2>
<h3>AFRU table fields</h3>
<table class="w-full">
<thead><tr><th>Field</th><th>Data element</th><th>Checktable</th><th>Datatype</th><th>Length</th>
<th>Decimals</th><th>Possible values</th></tr></thead>
<tbody>
<tr class="bg-blue-50"><td><div class="font-medium text-gray-900">MANDT</div>
<div class="text-sm text-gray-500">Client</div></td><td>MANDT</td>
<td><a href="/sap/table/t000/">T000</a></td><td><div>CLNT</div><div></div></td><td>3</td><td>0</td>
<td></td></tr>
<tr class="bg-blue-50"><td><div class="font-medium text-gray-900">RUECK</div>
<div class="text-sm text-gray-500">Completion confirmation number for the operation</div></td>
<td>CO_RUECK</td><td></td><td><div>NUMC</div><div></div></td><td>10</td><td>0</td><td></td></tr>
<tr class="bg-blue-50"><td><div class="font-medium text-gray-900">RMZHL</div>
<div class="text-sm text-gray-500">Confirmation counter</div></td>
<td>CO_RMZHL</td><td></td><td><div>NUMC</div><div></div></td><td>8</td><td>0</td><td></td></tr>
<tr class="hover:bg-gray-50"><td><div class="font-medium text-gray-900">AUFPL</div>
<div class="text-sm text-gray-500">Routing number of operations in the order</div></td>
<td>CO_AUFPL</td><td></td><td><div>NUMC</div><div></div></td><td>10</td><td>0</td><td></td></tr>
<tr class="hover:bg-gray-50"><td><div class="font-medium text-gray-900">APLZL</div>
<div class="text-sm text-gray-500">General counter for order</div></td>
<td>CO_APLZL</td><td></td><td><div>NUMC</div><div></div></td><td>8</td><td>0</td><td></td></tr>
<tr class="hover:bg-gray-50"><td><div class="font-medium text-gray-900">STOKZ</div>
<div class="text-sm text-gray-500">Confirmation: Document has been reversed</div></td>
<td>CO_STOKZ</td><td></td><td><div>CHAR</div><div></div></td><td>1</td><td>0</td>
<td><button onclick="toggleCollapse('collapse-6')">values</button></td></tr>
<tr id="collapse-6" class="hidden"><td colspan="7"><table>
<thead><tr><th>Value</th><th>Description</th></tr></thead>
<tbody><tr><td>X</td><td>Flag set</td></tr></tbody></table></td></tr>
</tbody></table>
<h3>AFRU foreign key
 relationships</h3>
<table class="w-full">
<thead><tr><th>Table</th><th>Field</th><th>Foreign key table</th><th>Foreign key field</th>
<th>Check table</th><th>Check field</th></tr></thead>
<tbody>
<tr><td>AFRU</td><td>APLZL</td><td>AFRU</td><td>MANDT</td><td>AFVC</td><td>MANDT</td></tr>
<tr><td>AFRU</td><td>APLZL</td><td>AFRU</td><td>AUFPL</td><td>AFVC</td><td>AUFPL</td></tr>
<tr><td>AFRU</td><td>APLZL</td><td>AFRU</td><td>APLZL</td><td>AFVC</td><td>APLZL</td></tr>
<tr><td>AFRU</td><td>MANDT</td><td>AFRU</td><td>MANDT</td><td>T000</td><td>MANDT</td></tr>
</tbody></table>
</body></html>"""


def _real_parser_present() -> bool:
    try:
        leanx_parse.is_table_page(LEANX_PAGE)
        leanx_parse.table_url("AFRU")
    except NotImplementedError:
        return False
    return True


@pytest.mark.skipif(not _real_parser_present(),
                    reason="domain.sap.leanx_parse is not implemented yet")
def test_a_real_leanx_page_round_trips_through_fetch_parse_put_and_get(tmp_path, clock,
                                                                        monkeypatch):
    web = _Web({leanx_parse.table_url("AFRU"): LEANX_PAGE.encode(),
                leanx_parse.table_url("NOPE"): SEARCH_PAGE.encode()})
    monkeypatch.setattr(sap_catalog, "urlopen", web)
    store = _store(tmp_path)
    table = store.fetch_table("AFRU", today="2026-09-15")
    assert table is not None and table.name == "AFRU" and table.description == "Order Confirmations"
    assert table.source == "leanx" and table.fetched_at == "2026-09-15"
    assert table.source_url == leanx_parse.table_url("AFRU") and table.module == "PP"
    assert table.primary_key() == ("MANDT", "RUECK", "RMZHL")
    assert table.field("MANDT").check_table == "T000"
    assert table.field("AUFPL").type_label() == "NUMC(10)"
    assert ("X", "Flag set") in table.field("STOKZ").values
    fk = table.fks_to("AFVC")
    assert len(fk) == 1 and fk[0].joinable
    assert fk[0].columns == (("MANDT", "MANDT"), ("AUFPL", "AUFPL"), ("APLZL", "APLZL"))
    assert CatalogStore(tmp_path / "data", shipped=tmp_path / "shipped").get("AFRU") == table
    # leanx's 404 body is its search page: not a table page, remembered as a miss
    assert store.fetch_table("NOPE") is None and store.fetch_table("NOPE") is None
    assert [u for u, _ua, _t in web.requests] == [leanx_parse.table_url("AFRU"),
                                                  leanx_parse.table_url("NOPE")]
