"""Build helix/sapcatalog — the shipped SAP data dictionary — offline, politely, resumably.

WHY a script and not a service: the catalog is package data. HELIX ships it (build.py adds the
folder) and reads it package-relative, so the tables are looked up, never guessed, even on a laptop
that cannot reach leanx. Somebody has to fill the folder once and again after the scope grows;
this is that somebody. Everything it needs is either in the repo (the scope, the parser, the
model) or fetched once into build/sapcatalog/ (gitignored) and kept, so a re-run after a crash, a
closed lid or a killed shell picks up where it stopped instead of hammering a volunteer-run site.

The pipeline, each step idempotent and re-runnable on its own:

  --fetch    SEED: every table in curated.SCOPE (module = its SCOPE key). Pages already
             downloaded by an earlier session are copied into the cache first; the rest are
             fetched from leanx.eu one per second, cached as HTML, and a 404 (leanx answers with
             its search page) is remembered in misses.json so it is never asked for again.
  --closure  one hop out: every check table the seed tables' foreign keys point at, if the SVN11X
             dictionary dump knows it as a real table (TRANSP/POOL/CLUSTER), most-referenced first,
             capped so seed + closure <= MAX_TABLES; then fetched the same way.
  --write    helix/sapcatalog/tables/<NAME>.json.gz (TableDef.to_dict, one per table) and
             index.json. Category and delivery class come from the SVN11X dump, the application
             component from SAP's own release-info JSON; a closure table gets a module only when
             it is the text table of exactly one seed module.
  --wide     the SVN11X dump (every ECC table and field, no keys, no compound foreign keys) into
             build/sapcatalog/wide.sqlite in the schema helix/sapcatalog/README.md fixes, then
             gzipped to helix/sapcatalog/wide.sqlite.gz.
  --status   where the build stands; --all runs the four steps in order and stops cleanly
             where it must wait for more pages (re-run to continue).

Run from the repo root:  python scripts/sapcatalog_build.py --all [--budget 570]
`--budget` bounds one run's fetching in seconds so a shell with a timeout can drive it.
Stdlib only, plus helix.domain.sap (pure). No test touches this file's network path.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so 'helix' imports directly

from helix.domain.sap import leanx_parse
from helix.domain.sap.curated import SCOPE, scope_module
from helix.domain.sap.model import TableDef, normalize_table

# ----- where things live -----
ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "helix" / "sapcatalog"
SHIPPED_TABLES = SHIPPED / "tables"
INDEX_JSON = SHIPPED / "index.json"
WIDE_GZ = SHIPPED / "wide.sqlite.gz"

BUILD = ROOT / "build" / "sapcatalog"
CACHE = BUILD / "cache" / "leanx"
MISSES_JSON = BUILD / "misses.json"       # leanx has no page: never asked again
ERRORS_JSON = BUILD / "errors.json"       # transient failures: retried next run
CLOSURE_JSON = BUILD / "closure.json"     # the chosen one-hop closure, with its reasons
WIDE_DB = BUILD / "wide.sqlite"
SVN_DIR = BUILD / "svn11x"
ORI_JSON = BUILD / "objectReleaseInfoLatest.json"

# Pages and dumps an earlier session already downloaded into its scratchpad: copied into the
# build folder when present so nothing is fetched twice. Absent → simply skipped.
SCRATCH = Path(
    "C:/Users/brian/AppData/Local/Temp/claude/C--Users-brian-HELIX-V3/"
    "779b74b4-6c4a-400e-a0cd-24ed0cd99018/scratchpad"
)
SCRATCH_PAGES = SCRATCH / "leanx"
SCRATCH_ORI = SCRATCH / "ori.json"

# ----- sources -----
USER_AGENT = "HELIX-catalog/0.1 (+desktop assistant; contact: brian_sullivan@mark1online.com)"
FETCH_INTERVAL_S = 1.0
THROTTLED_INTERVAL_S = 2.0            # after a 403/429: gentler for the rest of the run
THROTTLED_BACKOFF_S = 15.0
FETCH_TIMEOUT_S = 20.0
FETCH_MAX_BYTES = 4_000_000
SVN_BASE = "https://raw.githubusercontent.com/SVN11X/sap_data_dictionary_scraper/main/data/"
SVN_FILES = ("sap_tables.csv.gz", "sap_fields.csv.gz")
ORI_URL = ("https://raw.githubusercontent.com/SAP/abap-atc-cr-cv-s4hc/main/src/"
           "objectReleaseInfoLatest.json")

MAX_TABLES = 1500                       # seed + closure, the shipped catalog's size cap
REAL_TABLE_CATEGORIES = ("TRANSP", "POOL", "CLUSTER")
KNOWN_CATEGORIES = ("TRANSP", "POOL", "CLUSTER", "VIEW", "STRUCT", "INTTAB", "APPEND")
FK_KINDS = ("KEY", "REF", "TEXT")
# --budget counts from process start, not per step: --all runs fetch then closure, and a
# shell with a 10-minute timeout needs ONE clock across both.
_PROCESS_START = time.monotonic()
_IDENT = re.compile(r"^[A-Z0-9_/]+$")


def say(msg: str) -> None:
    print(msg, flush=True)


def _ascii_safe_stdout() -> None:
    """A Windows console (and a pipe on one) is cp1252 with strict errors: one SAP description
    with a character outside it would kill a run that had already done its work."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def today() -> str:
    return date.today().isoformat()


# ----- small JSON state files, written atomically so a killed run never leaves half a file -----
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(part, path)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def _gzip_file(src: Path, dst: Path) -> None:
    """gzip with a zeroed header mtime so an unchanged input gives a byte-identical output
    (a rebuild that changed nothing should not dirty the tree)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    with open(src, "rb") as fin, open(part, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    os.replace(part, dst)


def _gzip_text(dst: Path, text: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    with open(part, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as fout:
            fout.write(text.encode("utf-8"))
    os.replace(part, dst)


def _size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "absent"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


# ----- the seed -----
def seed_tables() -> dict[str, str]:
    """name → module, in SCOPE order (the fetch order, so the PP core lands first)."""
    return {normalize_table(t): module for module, names in SCOPE.items() for t in names}


# ----- the leanx page cache -----
def cache_path(name: str) -> Path:
    """<cache>/<name lower-case>.html — the same spelling the scratchpad used (tq80_t.html), so a
    copied page is found under the name the build asks for; a namespaced table's slashes are
    percent-encoded because they cannot be a file name."""
    return CACHE / (quote(normalize_table(name).lower(), safe="") + ".html")


def cached_html(name: str) -> str | None:
    p = cache_path(name)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def cached_date(name: str) -> str:
    """The day the page was fetched — the cache file's mtime (copies keep it)."""
    try:
        return datetime.fromtimestamp(cache_path(name).stat().st_mtime).date().isoformat()
    except OSError:
        return today()


def load_misses() -> dict[str, dict]:
    return _read_json(MISSES_JSON, {})


def record_miss(misses: dict[str, dict], name: str, why: str) -> None:
    misses[name] = {"when": today(), "why": why}
    _write_json(MISSES_JSON, misses)


def load_errors() -> dict[str, dict]:
    return _read_json(ERRORS_JSON, {})


def copy_scratch_pages(pages_from: Path) -> int:
    """Pages a previous session downloaded → the cache, unless the cache already has them.
    copy2 keeps the mtime, which is the page's fetch date."""
    if not pages_from.is_dir():
        return 0
    CACHE.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(pages_from.glob("*.html")):
        dst = CACHE / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            copied += 1
    return copied


class Fetcher:
    """One request per second to leanx, a 20 s timeout, a 4 MB cap, one retry. Returns
    ('ok', html) for a table page, ('miss', html) for a 404 or any non-table document (leanx's
    404 IS its search page, so both mean 'no such table'), ('error', reason) for anything that
    should be tried again another day."""

    def __init__(self, interval: float = FETCH_INTERVAL_S) -> None:
        self.interval = interval
        self.last = 0.0
        self.requests = 0

    def _wait(self) -> None:
        wait = self.last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str) -> tuple[int, bytes]:
        self._wait()
        self.last = time.monotonic()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                body = resp.read(FETCH_MAX_BYTES + 1)
                status = resp.status
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            body, status = e.read(FETCH_MAX_BYTES + 1), 404
        if len(body) > FETCH_MAX_BYTES:
            raise ValueError(f"page over {FETCH_MAX_BYTES} bytes")
        return status, body

    def fetch(self, name: str) -> tuple[str, str]:
        url = leanx_parse.table_url(name)
        last_error = ""
        for attempt in (1, 2):
            try:
                status, body = self._get(url)
            except Exception as e:  # noqa: BLE001 — any transport failure is a retry-later
                last_error = f"{type(e).__name__}: {e}"
                throttled = isinstance(e, urllib.error.HTTPError) and e.code in (403, 429)
                if throttled:
                    # Cloudflare in front of leanx answers a sustained 1/s crawl with sporadic
                    # 403s that clear on their own; ease off for the rest of the run.
                    self.interval = max(self.interval, THROTTLED_INTERVAL_S)
                if attempt == 1:
                    time.sleep(THROTTLED_BACKOFF_S if throttled else 2.0)
                continue
            html = body.decode("utf-8", errors="replace")
            if status == 404 or not leanx_parse.is_table_page(html):
                return "miss", html
            return "ok", html
        return "error", last_error


def ensure_pages(names: Iterable[str], *, budget: float = 0.0, label: str = "") -> bool:
    """Fetch every table in `names` not yet cached or missed. Returns True when nothing is left
    to fetch (every name is cached or a known miss), False when the budget ran out or errors
    remain for another run."""
    misses = load_misses()
    errors = load_errors()
    todo = [n for n in names if not cache_path(n).exists() and n not in misses]
    if not todo:
        return True
    say(f"{label}: {len(todo)} page(s) to fetch from leanx.eu at one per second")
    fetcher = Fetcher()
    failed: list[str] = []
    for i, name in enumerate(todo, 1):
        if budget and time.monotonic() - _PROCESS_START > budget:
            say(f"{label}: budget of {budget:.0f}s reached with {len(todo) - i + 1} page(s) "
                "left - re-run to continue")
            _write_json(ERRORS_JSON, errors)
            return False
        state, payload = fetcher.fetch(name)
        if state == "error":
            errors[name] = {"when": today(), "error": payload}
            failed.append(name)
            say(f"  [{i}/{len(todo)}] {name:<12} ERROR {payload}")
            continue
        _write_bytes(cache_path(name), payload.encode("utf-8"))
        errors.pop(name, None)
        if state == "miss":
            record_miss(misses, name, "not on leanx (404 / search page)")
            say(f"  [{i}/{len(todo)}] {name:<12} miss")
        else:
            say(f"  [{i}/{len(todo)}] {name:<12} ok    {len(payload) // 1024} KB")
    _write_json(ERRORS_JSON, errors)
    if failed:
        say(f"{label}: {len(failed)} page(s) failed and will be retried next run: "
            + ", ".join(failed))
        return False
    return True


def parse_cached(names: Iterable[str]) -> dict[str, TableDef]:
    """Every cached page of `names` → TableDef. A cached page that turns out not to be a table
    page (a copied 404) is recorded as a miss here, so the copy step needs no parser."""
    misses = load_misses()
    out: dict[str, TableDef] = {}
    for name in names:
        if name in misses:
            continue
        html = cached_html(name)
        if html is None:
            continue
        if not leanx_parse.is_table_page(html):
            record_miss(misses, name, "cached page is not a table page")
            continue
        table = leanx_parse.parse_table(html, fetched_at=cached_date(name))
        if table.name != name:
            # leanx answered with a different table (a redirect): the page is real but it is not
            # the one asked for, and pretending otherwise would file it under the wrong name.
            record_miss(misses, name, f"leanx served {table.name} instead")
            continue
        out[name] = table
    return out


def settled(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """(pending, missed) among `names`: pending = neither cached nor a known miss."""
    misses = load_misses()
    pending = [n for n in names if n not in misses and not cache_path(n).exists()]
    missed = [n for n in names if n in misses]
    return pending, missed


# ----- the SVN11X dump: every ECC table and field -----
def ensure_svn_files() -> None:
    SVN_DIR.mkdir(parents=True, exist_ok=True)
    for fn in SVN_FILES:
        dst = SVN_DIR / fn
        if dst.exists():
            continue
        src = SCRATCH / fn
        if src.exists():
            shutil.copy2(src, dst)
            say(f"svn11x: copied {fn} from the scratchpad")
            continue
        say(f"svn11x: downloading {fn} ...")
        _download(SVN_BASE + fn, dst)


def _download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    _write_bytes(dst, data)
    say(f"  {dst.name}: {len(data) // 1024} KB")


def _fix_shifted(row: dict) -> dict:
    """~280 rows of the tables dump have an empty description and the scraper let the columns
    slide left: description='TRANSP', table_category='A', delivery_class=''. Put them back."""
    cat, desc = row.get("table_category", ""), row.get("description", "")
    if cat not in KNOWN_CATEGORIES and desc in KNOWN_CATEGORIES:
        return {**row, "description": "", "table_category": desc, "delivery_class": cat}
    return row


def svn_tables() -> dict[str, dict]:
    """name → {description, category, delivery_class} from sap_tables.csv.gz."""
    ensure_svn_files()
    out: dict[str, dict] = {}
    with gzip.open(SVN_DIR / "sap_tables.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            row = _fix_shifted(row)
            name = normalize_table(row.get("table_name", ""))
            if name and name not in out:
                out[name] = {
                    "description": " ".join((row.get("description") or "").split()),
                    "category": (row.get("table_category") or "").upper(),
                    "delivery_class": (row.get("delivery_class") or "").upper(),
                }
    return out


def _fk_hint(description: str) -> tuple[str, str, str, str, str] | None:
    """A foreign-key pseudo-row's description 'FIELD CHECKTABLE CHECKFIELD [KIND] [CARDL CARDR]'
    → (field, check_table, check_field, kind, cardinality). The dump numbers these rows as if
    they were fields and names them after the table; the three-to-six-token shape is all that
    identifies them, so anything else (a real field that happens to share the table's name) is
    None."""
    tokens = description.split()
    if len(tokens) < 3 or len(tokens) > 6:
        return None
    fld, check, check_field = tokens[:3]
    if not all(_IDENT.match(t) for t in (fld, check, check_field)):
        return None
    rest = tokens[3:]
    kind = ""
    if rest and rest[0] in FK_KINDS:
        kind, rest = rest[0], rest[1:]
    elif len(rest) == 3:
        kind, rest = rest[0], rest[1:]
    card = f"{rest[0]}:{rest[1]}" if len(rest) == 2 else ""
    return fld, normalize_table(check), check_field, kind, card


def _int(text: str | None) -> int:
    try:
        return int((text or "").strip() or 0)
    except ValueError:
        return 0


WIDE_SCHEMA = """
CREATE TABLE tables (
  name           TEXT PRIMARY KEY,
  description    TEXT,
  category       TEXT,
  delivery_class TEXT,
  module         TEXT,
  component      TEXT
);
CREATE TABLE fields (
  table_name   TEXT,
  position     INTEGER,
  name         TEXT,
  description  TEXT,
  data_element TEXT,
  domain       TEXT,
  data_type    TEXT,
  length       INTEGER,
  decimals     INTEGER,
  check_table  TEXT
);
CREATE TABLE fk_hints (
  table_name   TEXT,
  field        TEXT,
  check_table  TEXT,
  check_field  TEXT,
  kind         TEXT,
  cardinality  TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
WIDE_INDEXES = """
CREATE INDEX ix_tables_name  ON tables(name);
CREATE INDEX ix_fields_name  ON fields(name);
CREATE INDEX ix_fields_table ON fields(table_name);
"""


def build_wide(components: dict[str, str]) -> dict[str, int]:
    """The dump → build/sapcatalog/wide.sqlite (README schema, nothing else — the reader's
    SELECTs name these columns) → helix/sapcatalog/wide.sqlite.gz. Plain rows, VACUUMed; the
    repeated description strings are left in place because a texts table would change the
    documented schema."""
    ensure_svn_files()
    tables = svn_tables()
    part = WIDE_DB.with_name(WIDE_DB.name + ".part")
    if part.exists():
        part.unlink()
    conn = sqlite3.connect(part)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(WIDE_SCHEMA)
    say(f"wide: {len(tables):,} tables ...")
    conn.executemany(
        "INSERT OR IGNORE INTO tables VALUES (?,?,?,?,?,?)",
        ((n, t["description"], t["category"], t["delivery_class"], scope_module(n),
          components.get(n, "")) for n, t in tables.items()),
    )
    say("wide: fields (1.3 M rows, a minute or so) ...")
    counts = Counter()
    batch_f: list[tuple] = []
    batch_k: list[tuple] = []

    def flush() -> None:
        if batch_f:
            conn.executemany("INSERT INTO fields VALUES (?,?,?,?,?,?,?,?,?,?)", batch_f)
            batch_f.clear()
        if batch_k:
            conn.executemany("INSERT INTO fk_hints VALUES (?,?,?,?,?,?)", batch_k)
            batch_k.clear()

    with gzip.open(SVN_DIR / "sap_fields.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            tname = normalize_table(row.get("table_name", ""))
            fname = (row.get("field_name") or "").strip().upper()
            if not tname or not fname:
                continue
            desc = " ".join((row.get("description") or "").split())
            pseudo = (fname == tname and not (row.get("data_element") or "").strip()
                      and not (row.get("data_type") or "").strip())
            if pseudo:
                hint = _fk_hint(desc)
                if hint is None:
                    counts["fk_junk"] += 1
                else:
                    batch_k.append((tname, *hint))
                    counts["fk_hints"] += 1
            else:
                check = normalize_table(row.get("check_table") or "")
                batch_f.append((
                    tname, _int(row.get("position")), fname, desc,
                    (row.get("data_element") or "").strip().upper(),
                    (row.get("domain") or "").strip().upper(),
                    (row.get("data_type") or "").strip().upper(),
                    _int(row.get("length")), _int(row.get("decimals")),
                    "" if check == "*" else check,   # '*' is the dictionary's 'generic', no table
                ))
                counts["fields"] += 1
            if len(batch_f) >= 20_000 or len(batch_k) >= 20_000:
                flush()
                if counts["fields"] % 200_000 < 20_000:
                    say(f"  ... {counts['fields']:,} fields")
    flush()
    # No indexes in the SHIPPED file: on 1.1M field rows they are a third of its size, and the
    # gz rides in git and in the frozen bundle. The adapter creates them (WIDE_INDEXES, mirrored
    # in helix/adapters/sap_catalog.py) once, right after inflating the db on first use.
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("source", "svn11x"), ("built_at", today()),
        ("tables_url", SVN_BASE + "sap_tables.csv.gz"),
        ("fields_url", SVN_BASE + "sap_fields.csv.gz"),
        ("component_source", ORI_URL),
    ])
    conn.commit()
    conn.execute("VACUUM")
    counts["tables"] = conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
    conn.close()
    os.replace(part, WIDE_DB)
    say(f"wide: {counts['tables']:,} tables, {counts['fields']:,} fields, "
        f"{counts['fk_hints']:,} fk hints ({counts['fk_junk']:,} pseudo-rows unreadable); "
        f"wide.sqlite {_size(WIDE_DB)} - gzipping ...")
    _gzip_file(WIDE_DB, WIDE_GZ)
    say(f"wide: wrote {WIDE_GZ.relative_to(ROOT)} {_size(WIDE_GZ)}")
    return dict(counts)


# ----- application components from SAP's release info -----
def components() -> dict[str, str]:
    """TABL name → applicationComponent, a trailing '-2CL' (SAP's 'compatibility layer' suffix
    in that file) stripped. The first non-empty entry per table wins."""
    if not ORI_JSON.exists():
        if SCRATCH_ORI.exists():
            shutil.copy2(SCRATCH_ORI, ORI_JSON)
            say("components: copied the release-info JSON from the scratchpad")
        else:
            say("components: downloading SAP's objectReleaseInfoLatest.json (10 MB) ...")
            _download(ORI_URL, ORI_JSON)
    doc = _read_json(ORI_JSON, {})
    items = doc.get("objectReleaseInfo", []) if isinstance(doc, dict) else doc
    out: dict[str, str] = {}
    for item in items or ():
        if not isinstance(item, dict) or item.get("tadirObject") != "TABL":
            continue
        name = normalize_table(item.get("tadirObjName") or "")
        comp = str(item.get("applicationComponent") or "").strip()
        if comp.endswith("-2CL"):
            comp = comp[: -len("-2CL")]
        if name and comp and name not in out:
            out[name] = comp
    return out


# ----- the closure: one hop out from the seed -----
def compute_closure(seed_defs: dict[str, TableDef], svn: dict[str, dict],
                    seed: dict[str, str]) -> dict:
    """Check tables the seed tables point at (declared foreign keys and the fields' check-table
    column), kept when the dump knows them as real tables, ranked by how many seed tables need
    them, capped at MAX_TABLES - len(seed)."""
    refs: Counter = Counter()
    for t in seed_defs.values():
        wanted = set(t.check_tables()) | {f.check_table for f in t.fields if f.check_table}
        for c in wanted:
            if c and c not in seed:
                refs[c] += 1
    ranked = sorted(refs.items(), key=lambda kv: (-kv[1], kv[0]))
    skipped = [c for c, _n in ranked
               if svn.get(c, {}).get("category") not in REAL_TABLE_CATEGORIES]
    eligible = [(c, n) for c, n in ranked if c not in skipped]
    room = max(0, MAX_TABLES - len(seed))
    chosen = eligible[:room]
    return {
        "computed_at": today(), "seed": len(seed), "seed_parsed": len(seed_defs),
        "candidates": len(ranked), "not_real_tables": skipped, "cap": MAX_TABLES,
        "cut_off": [c for c, _n in eligible[room:]],
        "tables": [c for c, _n in chosen], "referenced_by": dict(chosen),
    }


def text_table_module(table: TableDef, seed_defs: dict[str, TableDef],
                      seed: dict[str, str]) -> str:
    """A closure table earns a module only as the text table of exactly one seed module: it has a
    LANG key and, without it, its key is a seed table's key (CRTX ← CRHD, TJ02T ← TJ02)."""
    lang = table.language_key()
    if not lang:
        return ""
    bare = tuple(k for k in table.primary_key() if k != lang)
    if not bare:
        return ""
    modules = {seed[n] for n, s in seed_defs.items() if s.primary_key() == bare}
    return modules.pop() if len(modules) == 1 else ""


def finalize(table: TableDef, module: str, svn: dict[str, dict], comps: dict[str, str]) -> TableDef:
    meta = svn.get(table.name, {})
    return replace(
        table, module=module, component=comps.get(table.name, ""),
        category=meta.get("category") or table.category,
        delivery_class=meta.get("delivery_class") or table.delivery_class,
    )


# ----- writing the shipped folder -----
def shipped_path(name: str) -> Path:
    """helix/sapcatalog/tables/<NAME>.json.gz, percent-encoded with nothing safe — the
    adapter's rule, so it finds what the build wrote."""
    return SHIPPED_TABLES / (quote(name, safe="") + ".json.gz")


def write_shipped(defs: dict[str, TableDef]) -> int:
    SHIPPED_TABLES.mkdir(parents=True, exist_ok=True)
    keep = {shipped_path(n).name for n in defs}
    stale = [p for p in SHIPPED_TABLES.glob("*.json.gz") if p.name not in keep]
    for p in stale:
        p.unlink()
    for name in sorted(defs):
        _gzip_text(shipped_path(name), json.dumps(
            defs[name].to_dict(), ensure_ascii=False, separators=(",", ":")))
    rows = [
        {"name": t.name, "description": t.description, "module": t.module,
         "component": t.component, "keys": list(t.primary_key()), "fields": len(t.fields),
         "source": t.source}
        for t in (defs[n] for n in sorted(defs))
    ]
    # One row per line: index.json is tracked, and a scope change should diff as the rows it
    # added, not as one re-flowed blob.
    body = ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    text = f'{{"v": 1, "built_at": {json.dumps(today())}, "tables": [\n{body}\n]}}\n'
    part = INDEX_JSON.with_name(INDEX_JSON.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, INDEX_JSON)
    say(f"write: {len(defs)} tables -> {SHIPPED_TABLES.relative_to(ROOT)} "
        f"({len(stale)} stale removed); index.json {_size(INDEX_JSON)}")
    return len(defs)


# ----- the steps -----
def step_fetch(args) -> bool:
    seed = seed_tables()
    BUILD.mkdir(parents=True, exist_ok=True)
    copied = copy_scratch_pages(Path(args.pages_from))
    if copied:
        say(f"fetch: copied {copied} page(s) from {args.pages_from}")
    parse_cached(seed)                 # files copied 404s as misses before we count pending
    return ensure_pages(seed, budget=args.budget, label="fetch seed")


def step_closure(args) -> bool:
    seed = seed_tables()
    pending, _missed = settled(seed)
    if pending:
        say(f"closure: the seed is not settled - {len(pending)} page(s) still to fetch "
            "(run --fetch)")
        return False
    say("closure: parsing the seed pages ...")
    seed_defs = parse_cached(seed)
    svn = svn_tables()
    doc = compute_closure(seed_defs, svn, seed)
    _write_json(CLOSURE_JSON, doc)
    say(f"closure: {doc['candidates']} check tables referenced by {len(seed_defs)} parsed seed "
        f"tables; {len(doc['not_real_tables'])} not real tables in the dump; "
        f"{len(doc['tables'])} chosen (cap {MAX_TABLES} total, {len(doc['cut_off'])} cut off)")
    return ensure_pages(doc["tables"], budget=args.budget, label="fetch closure")


def _ready_to_write(allow_partial: bool) -> tuple[dict[str, str], list[str]] | None:
    seed = seed_tables()
    closure = _read_json(CLOSURE_JSON, None)
    if closure is None:
        say("write: no closure yet - run --closure first")
        return None
    names = list(seed) + [c for c in closure.get("tables", []) if c not in seed]
    pending, _missed = settled(names)
    if pending and not allow_partial:
        say(f"write: {len(pending)} page(s) still to fetch - run --fetch/--closure, or "
            "--allow-partial to ship what is here")
        return None
    return seed, names


def step_write(args) -> bool:
    ready = _ready_to_write(args.allow_partial)
    if ready is None:
        return False
    seed, names = ready
    say("write: parsing every cached page ...")
    defs = parse_cached(names)
    svn = svn_tables()
    comps = components()
    seed_defs = {n: t for n, t in defs.items() if n in seed}
    final: dict[str, TableDef] = {}
    text_modules = 0
    for name, table in defs.items():
        if name in seed:
            module = seed[name]
        else:
            module = text_table_module(table, seed_defs, seed)
            text_modules += bool(module)
        final[name] = finalize(table, module, svn, comps)
    say(f"write: {len(seed_defs)} seed + {len(final) - len(seed_defs)} closure tables "
        f"({text_modules} closure text tables filed under a seed module); "
        f"{sum(1 for t in final.values() if t.component)} with a component")
    write_shipped(final)
    return True


def step_wide(_args) -> bool:
    build_wide(components())
    return True


def step_status(_args) -> bool:
    seed = seed_tables()
    misses = load_misses()
    errors = load_errors()
    pending, missed = settled(seed)
    cached = [n for n in seed if cache_path(n).exists() and n not in misses]
    say(f"seed        : {len(seed)} tables in {len(SCOPE)} modules (curated.SCOPE)")
    say(f"  fetched   : {len(cached)}   pending: {len(pending)}   "
        f"errors last run: {len([e for e in errors if e in seed])}")
    say(f"  missed    : {len(missed)}  {' '.join(sorted(missed)) or '-'}")
    closure = _read_json(CLOSURE_JSON, None)
    if closure is None:
        say("closure     : not computed yet (--closure)")
    else:
        names = [c for c in closure.get("tables", []) if c not in seed]
        c_pending, c_missed = settled(names)
        c_cached = [n for n in names if cache_path(n).exists() and n not in misses]
        say(f"closure     : {len(names)} tables chosen of {closure.get('candidates')} candidates "
            f"(cap {closure.get('cap')} total, computed {closure.get('computed_at')})")
        say(f"  fetched   : {len(c_cached)}   pending: {len(c_pending)}   missed: {len(c_missed)}")
    say(f"misses.json : {len(misses)} tables leanx does not have")
    index = _read_json(INDEX_JSON, None)
    if not isinstance(index, dict):
        say("written     : nothing yet (--write)")
    else:
        rows = index.get("tables", [])
        by_module = Counter(r.get("module") or "-" for r in rows)
        say(f"written     : {len(rows)} tables in index.json ({_size(INDEX_JSON)}, built "
            f"{index.get('built_at')}); files on disk: "
            f"{len(list(SHIPPED_TABLES.glob('*.json.gz')))}")
        say("  by module : " + "  ".join(f"{m}={n}" for m, n in sorted(by_module.items())))
    if WIDE_DB.exists():
        conn = sqlite3.connect(WIDE_DB)
        try:
            t, f, k = (conn.execute(f"SELECT COUNT(*) FROM {x}").fetchone()[0]
                       for x in ("tables", "fields", "fk_hints"))
            built = dict(conn.execute("SELECT key, value FROM meta")).get("built_at", "?")
        finally:
            conn.close()
        say(f"wide        : {t:,} tables, {f:,} fields, {k:,} fk hints (built {built}); "
            f"wide.sqlite {_size(WIDE_DB)} -> wide.sqlite.gz {_size(WIDE_GZ)}")
    else:
        say(f"wide        : not built yet (--wide); wide.sqlite.gz {_size(WIDE_GZ)}")
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--status", action="store_true", help="print where the build stands")
    p.add_argument("--fetch", action="store_true", help="cache the seed tables' leanx pages")
    p.add_argument("--closure", action="store_true",
                   help="choose and fetch the one-hop closure (needs the seed fetched)")
    p.add_argument("--write", action="store_true",
                   help="write helix/sapcatalog/tables/*.json.gz and index.json")
    p.add_argument("--wide", action="store_true", help="build helix/sapcatalog/wide.sqlite.gz")
    p.add_argument("--all", action="store_true", help="fetch, closure, write, wide - in order")
    p.add_argument("--budget", type=float, default=0.0,
                   help="stop fetching after this many seconds (0 = no limit); re-run to resume")
    p.add_argument("--allow-partial", action="store_true",
                   help="--write even while pages are still pending")
    p.add_argument("--pages-from", default=str(SCRATCH_PAGES),
                   help="a folder of already-downloaded leanx pages to seed the cache from")
    args = p.parse_args(argv)
    steps: list = []
    if args.all:
        steps = [step_fetch, step_closure, step_write, step_wide, step_status]
    else:
        for flag, fn in (("fetch", step_fetch), ("closure", step_closure), ("write", step_write),
                         ("wide", step_wide), ("status", step_status)):
            if getattr(args, flag):
                steps.append(fn)
    if not steps:
        p.print_help()
        return 2
    _ascii_safe_stdout()
    BUILD.mkdir(parents=True, exist_ok=True)   # every step may run alone, first
    for fn in steps:
        say(f"== {fn.__name__.removeprefix('step_')} ==")
        if not fn(args):
            say("stopped here - re-run to continue")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
