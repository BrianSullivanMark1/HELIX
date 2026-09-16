"""The SAP catalog on disk — the shipped dictionary, the wide index, the user's extensions, and
the leanx.eu fetcher. The only I/O in the SAP faculty.

Three layers, read in this order:
  1. Extensions the app fetched on demand: <data>/sap/tables/<NAME>.json.gz — newest facts win.
  2. The shipped catalog: helix/sapcatalog/tables/<NAME>.json.gz (one TableDef document each, see
     model.TableDef.to_dict) with helix/sapcatalog/index.json — every shipped table's name,
     description, module, component, key fields and field count, for search without opening
     files. Shipped as package data (build.py --add-data helix/sapcatalog) and resolved
     package-relative (never from the cwd).
  3. The wide index: helix/sapcatalog/wide.sqlite.gz — every ECC table's name, description,
     category and (table, field, description, data element, check table, type) for ~110k tables,
     without key flags or foreign keys. Decompressed once into <data>/sap/wide.sqlite on first
     use (a few seconds); if absent or unreadable, everything else still works. Used to answer
     "which table has a field called …" and to describe a table not yet fetched.
     The schema the build writes (and this module reads) is documented in
     helix/sapcatalog/README.md.

Fetching: `fetch_table(name)` GETs https://leanx.eu/sap/table/<name>/ over https with a
HELIX user agent, a 20 s timeout, a 4 MB cap and a polite 1-request-per-second throttle
(module-level, shared across threads), parses it with domain.sap.leanx_parse and writes the
result to layer 1. Only that one host is ever contacted; a table leanx does not know
(`is_table_page` False) is remembered as a miss for the session so it is not re-fetched.

Threads: tools call from the turn's worker thread and routes from the uvicorn loop thread, so the
memory cache sits behind a lock and the wide index opens a fresh sqlite connection per call (a
connection is bound to the thread that made it). A fetch never holds the store lock — it is the
only slow path here and must not stall a panel read.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

import helix
from helix.domain.sap import leanx_parse
from helix.domain.sap.curated import scope_module
from helix.domain.sap.model import FieldDef, FkRef, TableDef, normalize_field, normalize_table
from helix.logging_setup import get_logger

_LOG = get_logger("sap.catalog")

LEANX_HOST = "leanx.eu"
USER_AGENT = "HELIX/1.0 (desktop assistant; SAP data-dictionary lookup)"
FETCH_MIN_INTERVAL_S = 1.0
FETCH_TIMEOUT_S = 20.0
FETCH_MAX_BYTES = 4_000_000

WIDE_SOURCE = "svn11x"           # what a wide-index TableDef says it came from
INDEX_NAME = "index.json"
WIDE_NAME = "wide.sqlite.gz"
# The indexes the wide index is searched through. NOT in the shipped gz (see _ensure_wide);
# created on first use. Keep in step with scripts/sapcatalog_build.py's schema.
WIDE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_tables_name  ON tables(name);
CREATE INDEX IF NOT EXISTS ix_fields_name  ON fields(name);
CREATE INDEX IF NOT EXISTS ix_fields_table ON fields(table_name);
"""
TABLES_DIR = "tables"
_SUFFIX = ".json.gz"
_STAMP_SUFFIX = ".src"          # beside wide.sqlite: which wide.sqlite.gz it was inflated from

# Indirections so a test can freeze the clock and skip the real sleep without touching `time`.
_monotonic = time.monotonic
_sleep = time.sleep


def shipped_dir() -> Path:
    """helix/sapcatalog, resolved package-relative (works frozen: PyInstaller keeps the folder
    beside the helix package). The bundle root is a backstop for the same reason config.py keeps
    one for build_info.json: a --add-data target can land at <_MEIPASS>/helix/… while the package
    itself is imported from somewhere else."""
    here = Path(helix.__file__).resolve().parent / "sapcatalog"
    if here.is_dir():
        return here
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        alt = Path(meipass) / "helix" / "sapcatalog"
        if alt.is_dir():
            return alt
    return here


# ----- the polite throttle: one request per second to leanx, process-wide -----
class _Throttle:
    """A context manager that spaces requests FETCH_MIN_INTERVAL_S apart across every thread in
    the process. The lock is held for the whole request so two threads fetching at once queue
    behind each other instead of both firing — leanx is a small volunteer-run site."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.lock = threading.Lock()
        self.last: float | None = None

    def __enter__(self) -> _Throttle:
        self.lock.acquire()
        if self.last is not None:
            wait = self.last + self.interval - _monotonic()
            if wait > 0:
                _sleep(wait)
        return self

    def __exit__(self, *_exc) -> None:
        self.last = _monotonic()
        self.lock.release()


_THROTTLE = _Throttle(FETCH_MIN_INTERVAL_S)


# ----- files -----
def _file_of(folder: Path, name: str) -> Path:
    """<folder>/<NAME>.json.gz. A namespaced table ('/BI0/TCUSTOMER') keeps its slashes in the
    dictionary but cannot be a file name, so the name is percent-encoded (quote with nothing
    safe): plain A-Z 0-9 _ names are unchanged, '/' becomes '%2F'. The build writes the same
    encoding (helix/sapcatalog/README.md)."""
    return folder / (quote(name, safe="") + _SUFFIX)


def _name_of(path: Path) -> str:
    return normalize_table(unquote(path.name[: -len(_SUFFIX)]))


def _list_names(folder: Path) -> set[str]:
    try:
        return {_name_of(p) for p in folder.iterdir() if p.name.endswith(_SUFFIX)}
    except OSError:
        return set()


def _load_table(path: Path) -> TableDef | None:
    """One gz JSON document → TableDef, or None (absent, truncated, not JSON, not a TableDef).
    A bad file is logged and treated as absent so one corrupt extension never takes the catalog
    down with it."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError) as e:
        _LOG.warning("unreadable catalog file %s: %s", path, e)
        return None
    if not isinstance(doc, dict) or not doc.get("name"):
        _LOG.warning("catalog file %s is not a table document", path)
        return None
    try:
        return TableDef.from_dict(doc)
    except (TypeError, ValueError, AttributeError) as e:
        _LOG.warning("catalog file %s does not parse as a TableDef: %s", path, e)
        return None


def _write_table(path: Path, table: TableDef) -> None:
    """Atomic: written beside the target and renamed in, so a reader on another thread never sees
    a half-written gz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with gzip.open(part, "wt", encoding="utf-8") as fh:
        json.dump(table.to_dict(), fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(part, path)


def _row_of(table: TableDef, layer: str) -> dict:
    return {
        "name": table.name, "description": table.description, "module": table.module,
        "component": table.component, "keys": list(table.primary_key()),
        "fields": len(table.fields), "source": table.source, "layer": layer,
    }


def _index_row(raw: object) -> dict | None:
    """One index.json row, validated and normalized; None for junk (a foreign or corrupt file)."""
    if not isinstance(raw, dict):
        return None
    name = normalize_table(raw.get("name") or "")
    if not name:
        return None
    keys = raw.get("keys")
    try:
        fields = int(raw.get("fields") or 0)
    except (TypeError, ValueError):
        fields = 0
    return {
        "name": name, "description": str(raw.get("description") or ""),
        "module": str(raw.get("module") or ""), "component": str(raw.get("component") or ""),
        "keys": [normalize_field(k) for k in keys if k] if isinstance(keys, list) else [],
        "fields": fields, "source": str(raw.get("source") or ""), "layer": "shipped",
    }


def _inflate(gz: Path, dst: Path) -> None:
    """wide.sqlite.gz → wide.sqlite, written as a .part file and renamed in."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    with gzip.open(gz, "rb") as src, open(part, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    os.replace(part, dst)


def _gz_stamp(gz: Path) -> str:
    st = gz.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


# ----- search helpers -----
def _words(words: Iterable[str] | str) -> list[str]:
    """Distinct non-empty words, order kept. A bare string is split (an Iterable[str] parameter
    handed one string would otherwise iterate its characters)."""
    raw = words.split() if isinstance(words, str) else words
    out: list[str] = []
    for w in raw:
        s = " ".join(str(w or "").split())
        if s and s not in out:
            out.append(s)
    return out


def _like(word: str) -> str:
    """A LIKE pattern for 'contains word', with the wildcards escaped: '_' is a letter in SAP names
    (TQ80_T), not a wildcard."""
    esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _passes(name_col: str, desc_col: str, words: list[str]) -> list[tuple[str, list]]:
    """The ranked WHERE clauses of a wide search: exact name, then name prefix, then the word
    somewhere in the name, then description only. Every pass also demands EVERY word appear in
    the name or the description, and excludes what the passes before it already matched, so the
    passes partition the hits and a LIMIT per pass never hides an exact match behind a fuzzy one.
    Exact and prefix are range tests (index-friendly on ~millions of field rows); only the last
    two passes scan."""
    upper = [w.upper() for w in words]
    exact = "(" + " OR ".join(f"{name_col} = ?" for _ in upper) + ")"
    exact_p: list = list(upper)
    prefix = "(" + " OR ".join(f"({name_col} >= ? AND {name_col} < ?)" for _ in upper) + ")"
    prefix_p: list = [x for w in upper for x in (w, w + "￿")]
    in_name = "(" + " OR ".join(f"{name_col} LIKE ? ESCAPE '\\'" for _ in upper) + ")"
    in_name_p: list = [_like(w) for w in upper]
    every = " AND ".join(
        f"({name_col} LIKE ? ESCAPE '\\' OR {desc_col} LIKE ? ESCAPE '\\')" for _ in words
    )
    every_p: list = [x for w in words for x in (_like(w), _like(w))]
    return [
        (f"{exact} AND {every}", exact_p + every_p),
        (f"{prefix} AND NOT {exact} AND {every}", prefix_p + exact_p + every_p),
        (f"{in_name} AND NOT {prefix} AND {every}", in_name_p + prefix_p + every_p),
        (f"NOT {in_name} AND {every}", in_name_p + every_p),
    ]


def _ranked(conn: sqlite3.Connection, select: str, order: str, passes: list[tuple[str, list]],
            limit: int, key) -> list[sqlite3.Row]:
    out: list[sqlite3.Row] = []
    seen: set = set()
    for where, params in passes:
        room = limit - len(out)
        if room <= 0:
            break
        rows = conn.execute(f"{select} WHERE {where} ORDER BY {order} LIMIT ?",
                            [*params, room]).fetchall()
        for r in rows:
            k = key(r)
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out


class CatalogStore:
    def __init__(self, data_dir: Path, *, shipped: Path | None = None) -> None:
        """`data_dir` is the app's data folder (<data>/sap is created lazily)."""
        self._data = Path(data_dir)
        self._shipped = Path(shipped) if shipped is not None else shipped_dir()
        self._ext_dir = self._data / "sap" / TABLES_DIR
        self._shipped_tables = self._shipped / TABLES_DIR
        self._wide_db = self._data / "sap" / "wide.sqlite"
        self._lock = threading.Lock()
        # The wide index has its own lock: the first-use inflate takes seconds and must not stall a
        # get() on another thread behind it.
        self._wide_lock = threading.Lock()
        self._cache: dict[str, TableDef] = {}
        self._misses: set[str] = set()              # leanx has no page for these (this session)
        self._fetch_allowed = True
        self._shipped_rows: dict[str, dict] | None = None   # index.json rows by name, once read
        self._built_at = ""
        self._ext_rows: dict[str, dict] = {}        # layer-1 index rows, filled as tables are seen
        self._wide_state: bool | None = None        # None until the first wide call decides

    # ----- reads -----
    def get(self, name: str) -> TableDef | None:
        """The table from layer 1 or 2 (cached in memory after the first read), else None.
        Layer 3 is NOT consulted here — see `describe_wide`."""
        n = normalize_table(name)
        if not n:
            return None
        with self._lock:
            hit = self._cache.get(n)
            if hit is not None:
                return hit
            for folder, layer in ((self._ext_dir, "extension"), (self._shipped_tables, "shipped")):
                table = _load_table(_file_of(folder, n))
                if table is None:
                    continue
                if table.name != n:
                    # The file is the authority on where it sits; the name asked for is how the
                    # caller will ask again, so the cache key is the asked-for name.
                    _LOG.warning("catalog file for %s says its name is %s", n, table.name)
                self._cache[n] = table
                if layer == "extension":
                    self._ext_rows[n] = _row_of(table, layer)
                return table
        return None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def names(self) -> tuple[str, ...]:
        """Every table in layers 1 + 2, sorted."""
        return tuple(sorted(self.shipped_names() | self.extension_names()))

    def shipped_names(self) -> set[str]:
        """Layer 2: the index's names plus any shipped file the index forgot."""
        return set(self._shipped_index()) | _list_names(self._shipped_tables)

    def extension_names(self) -> set[str]:
        """Layer 1: what has been fetched or put on this machine."""
        return _list_names(self._ext_dir)

    def built_at(self) -> str:
        """When the shipped catalog was built (index.json's stamp), '' when unknown."""
        self._shipped_index()
        return self._built_at

    def index(self) -> tuple[dict, ...]:
        """The shipped index rows plus layer-1 tables: {name, description, module, component,
        keys: [...], fields: n, source} (plus `layer`: 'shipped' | 'extension'). A layer-1 row
        replaces the shipped row of the same name — the newer facts win, as in `get`."""
        rows = dict(self._shipped_index())
        for n in self.extension_names():
            with self._lock:
                row = self._ext_rows.get(n)
            if row is None and self.get(n) is not None:
                with self._lock:
                    row = self._ext_rows.get(n)
            if row is not None:
                rows[n] = row
        return tuple(dict(rows[n]) for n in sorted(rows))

    def search_index(self, query: str, *, limit: int = 20) -> tuple[dict, ...]:
        """Index rows whose name or description matches the words of `query` (name prefix and
        exact matches first). Every word must appear somewhere (name or description); the rank
        is the best name match of any word: exact, then prefix, then contained, then description
        only. 'TV_AFRU' finds AFRU — the EDW view prefix is dropped like everywhere else."""
        words = _words(query)
        if not words or limit <= 0:
            return ()
        names = [normalize_table(w) for w in words]
        lowers = [w.lower() for w in words]
        hits: list[tuple[int, str, dict]] = []
        for row in self.index():
            name, desc = row["name"], row["description"].lower()
            haystack = name.lower() + " " + desc
            # A word counts as present when it is in the name or description as typed, or —
            # with its TV_ view prefix dropped — in the name ('tv_afko' finds AFKO).
            if not all(w in haystack or (n and n in name)
                       for w, n in zip(lowers, names, strict=True)):
                continue
            rank = 3
            for n in names:
                if not n:
                    continue
                if name == n:
                    rank = 0
                elif name.startswith(n):
                    rank = min(rank, 1)
                elif n in name:
                    rank = min(rank, 2)
            hits.append((rank, name, row))
        hits.sort(key=lambda h: (h[0], h[1]))
        return tuple(h[2] for h in hits[:limit])

    def _shipped_index(self) -> dict[str, dict]:
        """index.json by name, read once. A missing or corrupt index is not fatal: the shipped
        files are listed and opened for their rows instead (slower, once), because a catalog
        whose index went bad must still answer 'what fields does AFRU have'."""
        with self._lock:
            if self._shipped_rows is not None:
                return self._shipped_rows
        rows: dict[str, dict] = {}
        built_at = ""
        path = self._shipped / INDEX_NAME
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            doc = {}
        except (OSError, ValueError) as e:
            _LOG.warning("the shipped SAP index %s is unreadable (%s); listing the files", path, e)
            doc = {}
        if isinstance(doc, dict):
            built_at = str(doc.get("built_at") or "")
            for raw in doc.get("tables") or []:
                row = _index_row(raw)
                if row is not None:
                    rows[row["name"]] = row
        for n in sorted(_list_names(self._shipped_tables) - set(rows)):
            table = _load_table(_file_of(self._shipped_tables, n))
            if table is not None:
                rows[n] = _row_of(table, "shipped")
        with self._lock:
            if self._shipped_rows is None:
                self._shipped_rows = rows
                self._built_at = built_at
            return self._shipped_rows

    # ----- the wide index (layer 3) -----
    def wide_available(self) -> bool:
        return self._ensure_wide()

    def _ensure_wide(self) -> bool:
        """Inflate helix/sapcatalog/wide.sqlite.gz into <data>/sap/wide.sqlite once, then answer
        from memory for the life of the store. The .src stamp beside the db records which gz
        (size:mtime) it came from, so a restart skips the inflate unless a build shipped a new
        gz. Any failure — no gz, a truncated gz, a db without the expected tables — turns the
        wide index off and is logged once; nothing else depends on it."""
        with self._wide_lock:
            if self._wide_state is not None:
                return self._wide_state
            gz = self._shipped / WIDE_NAME
            stamp_file = self._wide_db.with_name(self._wide_db.name + _STAMP_SUFFIX)
            try:
                if not gz.is_file():
                    _LOG.info("no wide SAP index shipped (%s) — ECC-wide field search is off", gz)
                    self._wide_state = False
                    return False
                stamp = _gz_stamp(gz)
                current = ""
                if self._wide_db.is_file():
                    try:
                        current = stamp_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        current = ""
                if current != stamp:
                    _LOG.info("inflating the wide SAP index into %s", self._wide_db)
                    _inflate(gz, self._wide_db)
                    stamp_file.write_text(stamp, encoding="utf-8")
                conn = sqlite3.connect(str(self._wide_db))
                try:
                    have = {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'")}
                    # The shipped file carries no indexes (a third of its size on a million
                    # field rows, in git and in the frozen bundle); build them here, once — a
                    # few seconds on first use, a no-op on every later boot.
                    if {"tables", "fields"} <= have:
                        conn.executescript(WIDE_INDEXES)
                        conn.commit()
                finally:
                    conn.close()
                missing = {"tables", "fields"} - have
                if missing:
                    raise ValueError(f"wide.sqlite lacks {sorted(missing)}")
            except (OSError, EOFError, ValueError, sqlite3.Error) as e:
                _LOG.warning("the wide SAP index is unavailable: %s", e)
                self._wide_state = False
                return False
            self._wide_state = True
            return True

    def _wide_conn(self) -> sqlite3.Connection | None:
        """A fresh connection per call: routes and tools call from different threads, and a
        sqlite connection belongs to the thread that opened it. Opening is milliseconds."""
        if not self._ensure_wide():
            return None
        try:
            conn = sqlite3.connect(str(self._wide_db))
        except sqlite3.Error as e:
            _LOG.warning("could not open the wide SAP index: %s", e)
            return None
        conn.row_factory = sqlite3.Row
        return conn

    def describe_wide(self, name: str) -> TableDef | None:
        """A TableDef from the wide index only (no key flags, no foreign keys, source 'svn11x'),
        or None. Used when a table is neither shipped nor fetched. fk_hints become single-column
        FkRefs marked `partial` — the snapshot keeps only the last column pair of a compound
        key, so they are hints for the join engine, never joins on their own."""
        n = normalize_table(name)
        if not n:
            return None
        conn = self._wide_conn()
        if conn is None:
            return None
        try:
            head = conn.execute(
                "SELECT name, description, category, delivery_class, module, component "
                "FROM tables WHERE name = ?", (n,)).fetchone()
            field_rows = conn.execute(
                "SELECT position, name, description, data_element, domain, data_type, length, "
                "decimals, check_table FROM fields WHERE table_name = ? ORDER BY position, name",
                (n,)).fetchall()
            try:
                fk_rows = conn.execute(
                    "SELECT field, check_table, check_field, kind, cardinality FROM fk_hints "
                    "WHERE table_name = ? ORDER BY field, check_table", (n,)).fetchall()
            except sqlite3.OperationalError:   # an older build without fk_hints
                fk_rows = []
        except sqlite3.Error as e:
            _LOG.warning("wide index read failed for %s: %s", n, e)
            return None
        finally:
            conn.close()
        if head is None and not field_rows:
            return None
        fields = tuple(
            FieldDef(
                name=normalize_field(r["name"] or ""), description=str(r["description"] or ""),
                data_element=str(r["data_element"] or ""), domain=str(r["domain"] or ""),
                check_table=normalize_table(r["check_table"] or ""),
                data_type=str(r["data_type"] or "").upper(),
                length=int(r["length"] or 0), decimals=int(r["decimals"] or 0),
                position=int(r["position"] or 0),
            )
            for r in field_rows if r["name"]
        )
        fks = tuple(
            FkRef(
                field=normalize_field(r["field"] or ""),
                check_table=normalize_table(r["check_table"] or ""),
                columns=((normalize_field(r["field"] or ""),
                          normalize_field(r["check_field"] or "")),),
                partial=True, kind=str(r["kind"] or ""), cardinality=str(r["cardinality"] or ""),
            )
            for r in fk_rows if r["field"] and r["check_table"]
        )
        return TableDef(
            name=n,
            description=str(head["description"] or "") if head else "",
            module=str(head["module"] or "") if head else "",
            component=str(head["component"] or "") if head else "",
            category=str(head["category"] or "").upper() if head else "",
            delivery_class=str(head["delivery_class"] or "") if head else "",
            fields=fields, foreign_keys=fks, source=WIDE_SOURCE,
        )

    def search_wide_fields(self, words: Iterable[str], *, limit: int = 30) -> tuple[dict, ...]:
        """Fields anywhere in ECC whose name or description matches: {table, table_description,
        field, description, data_type, check_table}. Exact field names first (MATNR), then
        prefixes, then names containing a word, then description-only hits; every word must
        appear in the field's name or description."""
        ws = _words(words)
        if not ws or limit <= 0:
            return ()
        conn = self._wide_conn()
        if conn is None:
            return ()
        select = ("SELECT f.table_name AS tbl, t.description AS tdesc, f.name AS fld, "
                  "f.description AS fdesc, f.data_type AS dtype, f.check_table AS chk "
                  "FROM fields f LEFT JOIN tables t ON t.name = f.table_name")
        try:
            rows = _ranked(conn, select, "f.table_name, f.position",
                           _passes("f.name", "f.description", ws), limit,
                           key=lambda r: (r["tbl"], r["fld"]))
        except sqlite3.Error as e:
            _LOG.warning("wide field search failed: %s", e)
            return ()
        finally:
            conn.close()
        return tuple(
            {"table": r["tbl"], "table_description": str(r["tdesc"] or ""), "field": r["fld"],
             "description": str(r["fdesc"] or ""), "data_type": str(r["dtype"] or ""),
             "check_table": str(r["chk"] or "")}
            for r in rows
        )

    def search_wide_tables(self, words: Iterable[str], *, limit: int = 30) -> tuple[dict, ...]:
        """Tables anywhere in ECC whose name or description matches, ranked like
        `search_wide_fields`: {name, description, category, delivery_class, module, component}."""
        ws = _words(words)
        if not ws or limit <= 0:
            return ()
        conn = self._wide_conn()
        if conn is None:
            return ()
        select = ("SELECT name, description, category, delivery_class, module, component "
                  "FROM tables")
        try:
            rows = _ranked(conn, select, "name", _passes("name", "description", ws), limit,
                           key=lambda r: r["name"])
        except sqlite3.Error as e:
            _LOG.warning("wide table search failed: %s", e)
            return ()
        finally:
            conn.close()
        return tuple(
            {"name": r["name"], "description": str(r["description"] or ""),
             "category": str(r["category"] or ""), "delivery_class": str(r["delivery_class"] or ""),
             "module": str(r["module"] or ""), "component": str(r["component"] or "")}
            for r in rows
        )

    # ----- writes / fetch -----
    def put(self, table: TableDef) -> None:
        """Write to layer 1 (extensions) and the memory cache. The cache is updated even when the
        disk write fails (logged): the session keeps working, the next start re-fetches."""
        n = normalize_table(table.name)
        if not n:
            raise ValueError("a table needs a name")
        try:
            _write_table(_file_of(self._ext_dir, n), table)
        except OSError as e:
            _LOG.warning("couldn't persist the SAP table %s to %s: %s", n, self._ext_dir, e)
        with self._lock:
            self._cache[n] = table
            self._ext_rows[n] = _row_of(table, "extension")
            self._misses.discard(n)

    def fetch_table(self, name: str, *, today: str = "") -> TableDef | None:
        """Fetch + parse + store one table from leanx.eu; None when leanx has no such table or the
        network is unavailable (never raises; logs the reason). A 404 or a page that is not a
        table page is remembered as a miss for the session; a network error is not, so the next
        call tries again once the connection is back."""
        n = normalize_table(name)
        if not n:
            return None
        if not self._fetch_allowed:
            _LOG.debug("fetching %s skipped: on-demand SAP fetching is off", n)
            return None
        with self._lock:
            if n in self._misses:
                return None
        try:
            url = leanx_parse.table_url(n)
            status, html = self._download(url)
            if status == 404 or (status == 200 and not leanx_parse.is_table_page(html)):
                _LOG.info("leanx.eu has no table page for %s", n)
                with self._lock:
                    self._misses.add(n)
                return None
            if status != 200:
                return None
            table = leanx_parse.parse_table(
                html, fetched_at=today or date.today().isoformat(), source_url=url)
            if not table.module:
                table = table.with_module(scope_module(table.name))
            self.put(table)
            return table
        except Exception:  # noqa: BLE001 — a fetch must never take a turn down; the reason is logged
            _LOG.warning("fetching the SAP table %s failed", n, exc_info=True)
            return None

    def _download(self, url: str) -> tuple[int, str]:
        """(HTTP status, body) for a leanx page; (0, '') when nothing usable came back — the
        wrong host or scheme (never contacted), a timeout, a connection error, a page over the
        size cap. Runs inside the process-wide throttle."""
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() != LEANX_HOST:
            _LOG.warning("refusing to fetch %s: only https://%s is ever contacted", url, LEANX_HOST)
            return 0, ""
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        with _THROTTLE:
            try:
                with urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                    raw = resp.read(FETCH_MAX_BYTES + 1)
                    headers = getattr(resp, "headers", None)
                    charset = headers.get_content_charset() if headers is not None else None
                    status = int(getattr(resp, "status", 200) or 200)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, ""
                _LOG.warning("leanx.eu answered %s for %s", e.code, url)
                return int(e.code), ""
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                _LOG.warning("could not reach leanx.eu for %s: %s", url, e)
                return 0, ""
        if len(raw) > FETCH_MAX_BYTES:
            _LOG.warning("leanx page %s exceeds the %d-byte cap; ignored", url, FETCH_MAX_BYTES)
            return 0, ""
        try:
            return status, raw.decode(charset or "utf-8", errors="replace")
        except LookupError:   # an unknown charset label in the response header
            return status, raw.decode("utf-8", errors="replace")

    def fetch_allowed(self) -> bool:
        """Whether on-demand fetching is enabled (the service passes the setting in)."""
        return self._fetch_allowed

    def set_fetch_allowed(self, on: bool) -> None:
        self._fetch_allowed = bool(on)
