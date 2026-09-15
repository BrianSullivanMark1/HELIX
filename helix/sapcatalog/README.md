# helix/sapcatalog — the shipped SAP data dictionary

Package data. `build.py` ships this whole folder with `--add-data helix/sapcatalog`, and
`helix/adapters/sap_catalog.py` reads it package-relative through `shipped_dir()` — never from
the cwd, so it is found the same way in development and in a frozen build. The catalog build
script fills it; from git the folder holds only this README and `tables/.gitkeep`, and HELIX runs
fine with it empty (no shipped tables, no wide index — on-demand fetching still works).

## Layout

```
helix/sapcatalog/
  README.md             this file
  index.json            every shipped table's summary — search without opening files
  tables/<NAME>.json.gz one TableDef document per table
  wide.sqlite.gz        the wide index: every ECC table and field, no keys, no foreign keys
```

## tables/<NAME>.json.gz

One gzip-compressed UTF-8 JSON document per table: exactly `TableDef.to_dict()` from
`helix/domain/sap/model.py` (it carries `"v": 1`, the model version). Read back with
`TableDef.from_dict`.

File name: the table name upper-case, percent-encoded with nothing safe —
`urllib.parse.quote(name, safe="") + ".json.gz"`. Plain `A-Z 0-9 _` names are unchanged
(`AFRU.json.gz`, `TQ80_T.json.gz`); a namespaced table keeps its slashes in the dictionary but
not on disk (`/BI0/TCUSTOMER` → `%2FBI0%2FTCUSTOMER.json.gz`). No subfolders.

The user's own extensions use the same layout under `<data>/sap/tables/` and shadow the shipped
file of the same name.

## index.json

```json
{
  "v": 1,
  "built_at": "2026-09-14",
  "tables": [
    {"name": "AFRU", "description": "Order Confirmations", "module": "PP",
     "component": "PP-SFC-EXE-CON", "keys": ["MANDT", "RUECK", "RMZHL"], "fields": 126,
     "source": "leanx"}
  ]
}
```

One row per shipped table: `keys` are the key fields in key order, `fields` the field count,
`source` the TableDef's source (`leanx` today). Rows without a `name` are skipped. If the index is
missing or corrupt the store lists `tables/` and opens each file for its row instead — slower,
once per run — so the index is an accelerator, not a dependency.

## wide.sqlite.gz

A gzip of one SQLite database. The store inflates it into `<data>/sap/wide.sqlite` on first use
and records which gz it came from (`wide.sqlite.src` holds `size:mtime_ns` of the gz), so a
restart skips the inflate until a build ships a new gz. If the gz is absent or unreadable, or the
database lacks `tables`/`fields`, the wide index is simply off (logged once) and everything else
works.

The build script MUST write exactly this schema (the reader's SELECTs name these columns):

```sql
CREATE TABLE tables (
  name           TEXT PRIMARY KEY,   -- upper-case table name, e.g. 'AFRU'
  description    TEXT,               -- SAP's English short text
  category       TEXT,               -- TRANSP | POOL | CLUSTER | STRUCT | VIEW | INTTAB | ''
  delivery_class TEXT,               -- A C L G E S W or ''
  module         TEXT,               -- one of model.MODULES, or '' when unknown
  component      TEXT                -- application component, e.g. 'PP-SFC-EXE-CON', or ''
);
CREATE TABLE fields (
  table_name   TEXT,                 -- → tables.name
  position     INTEGER,              -- field order within the table, 1-based
  name         TEXT,                 -- upper-case field name
  description  TEXT,
  data_element TEXT,
  domain       TEXT,
  data_type    TEXT,                 -- CHAR NUMC DATS TIMS QUAN CURR CLNT LANG UNIT CUKY DEC INT4 RAW …
  length       INTEGER,
  decimals     INTEGER,
  check_table  TEXT                  -- '' when none
);
CREATE TABLE fk_hints (              -- one row per (field, check table): the LAST column pair only
  table_name   TEXT,
  field        TEXT,
  check_table  TEXT,
  check_field  TEXT,
  kind         TEXT,                 -- KEY | REF | TEXT | ''
  cardinality  TEXT                  -- e.g. '1:CN', or ''
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);   -- rows: source, built_at
CREATE INDEX ix_tables_name  ON tables(name);
CREATE INDEX ix_fields_name  ON fields(name);
CREATE INDEX ix_fields_table ON fields(table_name);
```

Notes for the build:

- Names upper-case, `''` (not NULL) for unknown text — the reader tolerates NULL but the searches
  are `LIKE` on these columns.
- `fk_hints` carries only the last column pair of each declared foreign key (that is all the
  source snapshot keeps); the reader turns each row into a single-column `FkRef` with
  `partial=True`. A compound key belongs in `tables/<NAME>.json.gz`, not here.
- `meta` holds at least `source` (`svn11x`) and `built_at` (`YYYY-MM-DD`). A `TableDef` built
  from this index reports `source = 'svn11x'`, no key flags and no joinable foreign keys.
- `fk_hints` may be absent in an older build; the reader then reports no hints.
- The reader ranks searches exact name → name prefix → name contains → description only, using
  range tests on `fields(name)` / `tables(name)` for the first two; keep those indexes.

## Rebuilding the catalog

`scripts/sapcatalog_build.py` fills this folder. It is offline-first and resumable: everything it
fetches lands under `build/sapcatalog/` (gitignored) and a re-run picks up where the last one
stopped, so a closed lid or a killed shell costs nothing and leanx is never asked twice.

```
python scripts/sapcatalog_build.py --status          # where the build stands
python scripts/sapcatalog_build.py --all             # fetch, closure, write, wide — in order
python scripts/sapcatalog_build.py --all --budget 570   # stop fetching after 570 s; re-run to resume
```

The steps, each also runnable alone (`--fetch`, `--closure`, `--write`, `--wide`):

1. **Seed** — every table in `curated.SCOPE`, module = its SCOPE key. Pages are fetched from
   `https://leanx.eu/sap/table/<name>/` at one request per second (20 s timeout, 4 MB cap, one
   retry) into `build/sapcatalog/cache/leanx/<name>.html`. A 404 — leanx answers with its search
   page — goes into `build/sapcatalog/misses.json` and is never asked for again; a transport error
   goes into `errors.json` and is retried next run.
2. **Closure** — one hop out: every check table the seed tables' foreign keys (and fields'
   check-table column) point at, kept when the SVN11X dump knows it as a real table
   (TRANSP/POOL/CLUSTER), most-referenced first, capped so seed + closure ≤ 1500. The choice and
   its reasons are written to `build/sapcatalog/closure.json`; the pages are fetched the same way.
3. **Write** — `tables/<NAME>.json.gz` + `index.json` for every page that parsed. Category and
   delivery class come from the SVN11X tables dump, the application component from SAP's
   `objectReleaseInfoLatest.json` (a trailing `-2CL` stripped). A closure table gets a module
   only when it is the text table of exactly one seed module (a LANG key whose remaining key
   equals a seed table's key). Files no longer in scope are removed.
4. **Wide** — the SVN11X dump (`sap_tables.csv.gz`, `sap_fields.csv.gz`, ~113k tables / 1.1M
   fields) into `build/sapcatalog/wide.sqlite` in exactly the schema above, VACUUMed, gzipped to
   `wide.sqlite.gz`. Rows whose field name equals the table name are the dump's foreign-key
   pseudo-rows (`FIELD CHECKTABLE CHECKFIELD [KIND] [CARDL CARDR]`) and become `fk_hints`.

Sources, downloaded once into `build/sapcatalog/` when not already there:
leanx.eu (table pages), `SVN11X/sap_data_dictionary_scraper` on GitHub (the wide dump) and
`SAP/abap-atc-cr-cv-s4hc` on GitHub (components). Gzip headers carry a zero mtime, so a rebuild
that changed nothing leaves the tree clean. Growing the scope is editing `curated.SCOPE` and
running `--all` again.
