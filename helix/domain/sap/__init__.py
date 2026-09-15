"""SAP ECC data-model expertise — pure (no I/O).

The layer that turns "what field on PLPO is the work center" from a recollection into a lookup:
  model.py        the dictionary records (tables, fields, foreign keys) and the join edges/filters
  curated.py      the expert layer SAP's dictionary does not declare — semantic joins with their
                  compound keys, the filters every reporting query silently needs, plain-English
                  notes, the catalog's scope
  leanx_parse.py  a leanx.eu table page → TableDef (the dictionary source the catalog is built from)
  graph.py        the join graph and shortest join paths between any tables
  sql.py          the Snowflake writer for the user's EDW (EDW.SRC_SAPECC_ARP.TV_<TABLE>)
  overlay.py      what the user's EDW actually has (tables present/missing, real column lists)
"""
