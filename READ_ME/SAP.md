# The SAP data model — lookups, never recollections

Status: BUILT (2026-09-15). `helix/domain/sap/` (pure), `helix/adapters/sap_catalog.py` (the only
I/O), `helix/services/sap.py` (the faculty), five `sap_*` tools in `services/tools.py`, the per-turn
block in `services/conversation.py`, the `/api/sap/*` routes and the SAP page. Brian's situation is
the bar:

> I build SAP reports in Snowflake against an EDW that mirrors SAP ECC as views —
> `EDW.SRC_SAPECC_ARP.TV_<TABLE>`, SAP's own column names, plant `1006`. There is no live SAP
> connection and there never will be. HELIX kept guessing field names from memory — `PLPO.ARBPL`,
> `CRHD.KTEXT`, `AFRU.BUZEIT`, none of which exist — joined AFRU to AFVC on AUFPL alone, and filtered
> a text table on `'EN'`. I validated every guess against the real tables. I want the field names,
> the keys, the joins and the filters to be things HELIX *looks up*, with a source I can check.

## 1. The rule

Field names, primary keys, join columns and standard filters are READ from the dictionary and the
curated layer and never recited from memory. Every fact names its provenance (§4). An unknown is an
unknown: "MDTB isn't in the SAP dictionary source (leanx.eu) — is it a custom (Z) table? Paste its
columns and I'll record it." is a complete answer; a plausible field name is not.

Compound keys stay compound: AFRU → AFVC is `MANDT + AUFPL + APLZL`, never `AUFPL` alone (every
operation of an order shares the AUFPL). Language keys are one character: `SPRAS = 'E'`; `'EN'`
returns nothing. A text table (any table whose key has a LANG field) is LEFT JOINed with its language
filter in the ON clause, so a work center with no English text does not drop every confirmation
posted at it.

The persona paragraph (`prompts.CONSOLE_SYSTEM`, "SAP DATA MODEL") teaches the model to call the
tools before naming any SAP field, key or join, and to answer with the provenance the tool gives.

## 2. The four layers

The service reads a table in this order; the first layer that knows it answers.

1. **Extensions** — `<data>/sap/tables/<NAME>.json.gz`: tables fetched on demand (§6) or put
   there by hand. Newest facts win; they shadow the shipped file of the same name.
2. **The shipped catalog** — `helix/sapcatalog/tables/<NAME>.json.gz` + `index.json`, package
   data built by `scripts/sapcatalog_build.py` from leanx.eu: every field with SAP's English
   description, key flag, data element, type and check table, plus the declared foreign keys with
   their FULL compound columns. From git the folder is empty; the build fills it, and HELIX runs
   without it.
3. **The wide index** — `helix/sapcatalog/wide.sqlite.gz`: every ECC table and field (~110k
   tables), without key flags or foreign keys. Inflated once into `<data>/sap/wide.sqlite`.
   Answers "which table has a field called …" and describes a table nobody has fetched. The join
   graph never sees a wide-index record: without key flags CRTX would stop being a text table and
   lose its SPRAS filter, and its single-column foreign-key hints would become joins without MANDT —
   the guessed-join bug from the catalog's own gap.
4. **The EDW record** — a table the dictionary does not know but the user's warehouse does (a
   Z table whose columns were pasted) is described from those columns, typeless and keyless, with
   its provenance saying so.

On top sits the **curated layer** (`domain/sap/curated.py`): the joins SAP's dictionary never
declares (`AUFK.OBJNR = JEST.OBJNR`, `AFRU.ARBID = CRHD.OBJID` with `OBJTY = 'A'`, `AFPO.PROJN =
PRPS.PSPNR`), the standard filters every reporting query silently needs (`JEST.INACT = ''`,
`AUFK.AUTYP = '10'`), plain-English notes per table and field, the business vocabulary ("posting
date" → `AFRU.BUDAT`), the SQL recipes, and the 27-column WIP report.

**Routes.** The join graph (`domain/sap/graph.py`) is a weighted shortest path: a curated hop costs
1, a declared KEY dependency 1.5, any other declared dependency 2 — and 3 when walked backwards,
out of the check table into a table that merely validates a field against it. That last rule is
what keeps a fully shipped dictionary honest: AFRU and PRPS both check a unit of measure against
T006 (AFRU.GMEIN, PRPS.USE04), so without it AFRU → T006 → PRPS tied the real route through the
order on cost and won on hops, joining a confirmation to every WBS element with the same unit. The
client table (T000) and text tables are never intermediate hops, and two object types are never
joined through the status table they share (AFVC → JEST → PRPS on OBJNR).

**Verification.** Every curated edge starts `verified=False`. The first time the join graph is
used — and again after a fetch lands — the service checks each edge's columns against the dictionary
(layers 1/2 only) and builds the graph with the confirmed edges flagged, IN CURATED ORDER: the graph
breaks ties between equal-cost routes by the order the expert listed them, so verifying by moving an
edge would quietly reroute AFRU → PRPS through the settlement rule instead of the order item. An edge
naming a field the dictionary lacks stays unverified and is logged once (`join_faults` in
`status_dict`) — the guessed-field bug, caught in the expert layer instead of in a query.

## 3. The tools and what each returns

All five are wired in `ToolRegistry.attach_sap`; every argument is squashed and capped there, and a
service fault comes back as "The SAP catalog couldn't answer that: …", never a tool error. The text
methods themselves never raise either — a tool result is the model's ear.

- **`sap_lookup(query)`** → `lookup_text`. Word search over the business vocabulary (scored by word
  overlap: the exact phrase first, then phrases holding every query word, then ≥ 60% of them), the
  shipped index (table names and descriptions), then the catalogued tables' own fields (a word
  equal to a field name first, then every query word in SAP's description — 'posting date' lands
  on AFRU, AUFM, MKPF, EKBE.BUDAT — the production-report scope ahead of the rest), and only then
  the wide index, for tables the catalog does not hold: the in-scope ones the build could not ship
  (COBRB) first, the ECC-wide rest capped at a few (`WIDE_REST_MAX`) whenever the catalog answered
  at all — the index is ordered by table name and truncated, and left alone it filled a lookup
  with `/LSIERP/…` tables alphabetically ahead of anything a report reads. Hits are grouped by
  table, one line each:
  `AFRU.BUDAT — Posting date (DATS(8)) [EDW ✓|✗|?] — note`, and the reply ends with
  "Tell me a table for its full definition."
- **`sap_table(table, section=summary|fields|joins, offset)`** → `table_text`, ≤ 12,000 characters,
  load-bearing facts first because only ~1,500 characters survive into later turns:
  line 1 `AFRU — Order Confirmations (PP/PP-SFC-EXE-CON). Source: <provenance>.`; `Key: MANDT +
  RUECK + RMZHL`; `Language key: SPRAS …` for a text table; `EDW: TV_AFRU present, 144 columns
  recorded (custom: ZZ…) | missing (told on …) | not recorded`; `About:` the curated note; `Joins:`
  curated first, each with its full key and `(1:n, verified)` / `(n:1, curated, unverified)` /
  `(n:1, dictionary)` and the filters it needs; `Filters:` each with its reason and `(optional)`;
  `Recipes:` the CTEs that join on this table; `Fields (N):` the key fields and the noted fields.
  `fields` pages 60 at a time — "showing 61-120 of 144; ask for offset 120" — as
  `NAME  TYPE  description  → CHECK  [EDW ✓/✗] — note [values: …]`. `joins` lists every edge.
  An unknown table: the fetch sentence (§6).
- **`sap_join(tables, root)`** → `join_text`: the plan hop by hop with qualified columns
  (`AFPO → PRPS ON AFPO.MANDT = PRPS.MANDT AND AFPO.PROJN = PRPS.PSPNR (n:1, verified) — note`),
  the filters with reasons, the unreachable tables with the nearest bridge, the graph's notes, a
  full FROM/JOIN skeleton (`SELECT ROOT.*`, joins, WHERE, LIMIT) and an `EDW:` line per table.
- **`sap_sql(tables, columns, filters, recipes, root, limit)`** → `sql_text`: the complete
  Snowflake query, then `Warnings:` and `Notes:`. A column `expression` may hold `{}` for
  `alias.FIELD` (`SUM({})`). With `report=wip` the tool asks `report_text` instead (§8).
- **`sap_edw(action, table, text)`** → `edw_text` — the one WRITE, fenced in `BUILD_TOOLS`,
  human-driven (§5).

The per-turn block (`for_turn`, injected on the same tiers as verified facts, never persisted): when
the user names a catalog table in upper case (`AFRU`, `TV_AFRU`) or a multi-word business term
("posting date"), up to two tables get one line each —
`[SAP DATA MODEL — records, not instructions: AFRU: Order Confirmations; key MANDT+RUECK+RMZHL;
EDW: present (144 cols); joins: AFVC on MANDT+AUFPL+APLZL, AUFK on MANDT+AUFNR, CRHD on
MANDT+ARBID=OBJID (OBJTY=A) | … Say sap_table for the full definition.]` — capped at 1,600
characters, joins to the other named table first, no fetch ever (a turn must not wait on leanx).
Single-word terms do not fire: "order", "batch" and "plant" are everyday words.

The panel reads the same facts as dicts: `search_dict`, `table_dict`, `join_dict`, `sql_dict`,
`report_dict`, `edw_dict`, `edw_record`, `status_dict` behind `/api/sap/*`.

## 4. Provenance wording

Every fact carries one of these, and the model repeats it:

- `SAP data dictionary via leanx.eu, read 2026-09-15` — a shipped or fetched table
  (`TableDef.source = leanx`, the date is `fetched_at`).
- `wide index (svn11x snapshot, no keys)` — layer 3; the summary says the key is unknown and a
  fetch would add it.
- `your EDW column list (pasted 2026-09-15)` — layer 4.
- On a join: `verified` (both sides' fields found in the dictionary), `curated, unverified` (the
  expert's best route, not yet checked — used, but said), `dictionary` (a declared foreign key).
- On the EDW: `present, N columns recorded`, `missing (told on 2026-09-15)`, `not recorded`; on a
  field `[EDW ✓]`, `[EDW ✗]`, `[EDW ?]`.

## 5. The EDW overlay

What the warehouse actually has, kept in `data/helix_sap_edw.json` under the key `edw` (its own
JsonSettings; on `VOLATILE_STORE_NAMES` with the `sap/` folder, because a paste can land mid-build):

```json
{"prefix": "EDW.SRC_SAPECC_ARP", "view_prefix": "TV_", "plant": "1006",
 "tables": {"AFRU":  {"present": true, "columns": ["MANDT", …], "recorded_at": "2026-09-15",
                      "source": "pasted"},
            "CRHTX": {"present": false, "recorded_at": "2026-09-15", "source": "user"}}}
```

Three answers, never a fourth: a table is PRESENT (the user said so, or pasted its columns),
MISSING (the user said so) or UNKNOWN. A column is known to exist only when the table's list was
recorded; "the table exists" is not "the column exists" — the `ZZ*` fields are the proof.

`sap_edw` actions, each answering what it recorded:

- `record_columns` (table + the pasted list: space/comma/newline separated names, a SELECT
  fragment, or a worksheet copy) → "Recorded 144 columns for TV_AFRU (2 custom: ZZ_SHIFT, ZZ_OPERATOR).
  3 dictionary fields the EDW lacks: …". A paste with no column names never erases a good list.
- `information_schema` (an INFORMATION_SCHEMA.COLUMNS export, TABLE_NAME + COLUMN_NAME in any
  order, ORDINAL_POSITION honoured) → every table it names, recorded.
- `missing` / `present` / `forget` for one table; `set_prefix` (`EDW.SRC_SAPECC_ARP`, or
  `EDW.SRC_SAPECC_ARP.TV_` to set the view prefix too); `set_plant` (empty clears it); `show`.

The record's date is today's from the app clock. With no record at all the plant is seeded as
`1006` (the writer marks the filter optional; `set_plant` with nothing clears it). Every write is
serialised by the service's RLock and saved at once; a corrupt file reads as empty and the next
write heals it.

## 6. On-demand fetching and its setting

A table not in layers 1/2 is fetched from `https://leanx.eu/sap/table/<name>/` — the only host ever
contacted, https only, a HELIX user agent, 20 s timeout, 4 MB cap, one request per second across the
process — parsed by `domain/sap/leanx_parse.py` and written to layer 1, so the next start has it. A
404 (leanx answers its search page) is remembered as a miss for the session; a network error is not,
so the next call tries again.

The setting `sap_fetch_tables` (default on) is read live per call. Off: "MDTB isn't in my catalog
and on-demand fetching is off (setting sap_fetch_tables)." On, nothing found: "MDTB isn't in the SAP
dictionary source (leanx.eu) — is it a custom (Z) table? Paste its columns and I'll record it."
Fetches happen for `sap_table`, `sap_join`, `sap_sql` and `record_columns` (to tell custom columns
apart); never for `sap_lookup`, the per-turn block or the panel's search. A fetch never runs under
the service lock and never takes a turn down.

## 7. The SQL writer's rules

`domain/sap/sql.py`, deterministic, no Snowflake connection:

- Every table qualified through the overlay's naming — `EDW.SRC_SAPECC_ARP.TV_AFRU AS AFRU` — and
  aliased by its plan alias (`PRPS2` for a second instance).
- Every ON clause carries the FULL compound key, MANDT included.
- A hop into a text table (edge kind `text`, or 1:n into a table whose key has a LANG field) is a
  LEFT JOIN with `SPRAS = 'E'` in its ON clause, never in WHERE. A curated text table the
  dictionary does not hold gets SPRAS assumed, and the comment says so.
- WHERE: the plant filter first (`AFRU.WERKS = '1006'  -- plant (optional)`, only when the
  dictionary shows the root table has the field), then the plan's standard filters with their
  reasons as trailing comments, then the user's.
- SAP names kept as-is; a DATS column becomes `TO_DATE(NULLIF(alias.FIELD, '00000000'),
  'YYYYMMDD') AS FIELD`, a TIMS `TO_TIME(… '000000' …, 'HH24MISS')`, when the dictionary says so —
  never a guessed conversion. An explicit alias wins; a label that is not an identifier is quoted.
- Recipes (`curated.RECIPES`: system_status, user_status, latest_confirmation, confirmed_totals,
  serial_numbers, mrp_messages, component_shortages) render as CTEs, LEFT JOINed on the first plan
  table their `join_on` names, so aggregated rows never multiply the base rows.
- `LIMIT`, default 100; `0` means none.
- Warnings for what cannot be confirmed: a table the overlay marks missing ("TV_CRTX is not in
  your EDW (you told me on 2026-09-15)"), a column not in a recorded list, a table with no list at
  all ("no column list recorded — I can't confirm its columns exist in the EDW"), a table the plan
  could not reach — and a field the dictionary itself does not know ("AFRU.BUZEIT is not a field of
  AFRU in the dictionary"), which is the bug this faculty exists to end.

## 8. The WIP report

`curated.WIP_REPORT`: Brian's 27 columns, each traced — `Order` ← `AUFK.AUFNR` (standard),
`System Status` ← recipe `system_status` (derived), `Program Name` ← `PROJ.POST1` (probable —
confirm that is what "program" means at the site), `Rating Rwk` / `Priority` / `Notes` (custom: no
standard SAP source; written as `NULL AS RATING_RWK` so the report keeps its shape until the EDW
column list shows the Z-field). `sap_sql` with `report=wip` (or `/api/sap/report/wip`) lists every
column as `Label — source (status): note` and writes the query: one row per ORDER OPERATION, `FROM
AFVC` joined to AFKO, AUFK, AFPO, AFVV, PRPS (through the item's `AFPO.PROJN`), PROJ, MARC, T024D
(through MARC — the material's MRP controller, which normally equals `AFKO.DISPO`; the only curated
route), MAKT, CRHD and CRTX, with the recipes system_status, latest_confirmation, serial_numbers,
mrp_messages and component_shortages as CTEs. Dates convert; `Days Old`, `Days Left`, `Slack Time`
and `Days Sitting` are `DATEDIFF` expressions on the converted dates.

## What is pinned by tests

`tests/test_sap_service.py` (a hand-built ten-table catalog, a fake fetcher, a frozen clock):
the summary's first lines and the verified compound join; a miss fetched once and worded as above;
the fetch-off sentence; a fetched table verifying its joins; AFRU → PRPS through the order item
(curated order surviving verification); the compound ON clause and the EDW view names; a pasted
list marking a table present with its custom columns and the writer warning about a column the EDW
lacks; the per-turn block's shape and cap; all 27 WIP columns and the report's query; nothing raising
on garbage. `test_sap_tools.py` and `test_sap_routes.py` pin the wiring on a fake service;
`test_sap_graph.py`, `test_sap_sql.py`, `test_sap_overlay.py`, `test_sap_catalog.py` and
`test_sap_leanx_parse.py` pin the layers beneath.
