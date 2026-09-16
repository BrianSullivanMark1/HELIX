"""The join graph — how any two SAP tables connect, with the full keys. Pure.

Built from two kinds of edge: the dictionary's declared foreign keys (model.edges_from_fks on
every TableDef the catalog holds) and the curated edges (curated.CURATED_JOINS). A curated edge
between the same pair on the same columns supersedes a dictionary edge (it carries the note,
cardinality and filters); a dictionary edge on different columns is kept beside it.

Path finding is a weighted shortest path: curated edges cost 1, dictionary KEY edges 1.5, other
dictionary edges 2 (3 when walked backwards, out of the check table), text-table hops 1 — so
"AFRU to WBS element" comes back as AFRU → AUFK → AFPO → PRPS (through the order) rather than
through a check table shared by chance (T001W, T006). Unverified curated edges are still used
(they are the expert's best route) but the plan says so.

`plan(tables, root=…)` reaches every requested table from the root along shortest paths and
merges them into one JoinPlan with unique aliases (the table name, or NAME2 when a table appears
twice), the filters every touched edge/table needs (curated.STANDARD_FILTERS for each table, the
edge's own filters, and SPRAS = LANGUAGE for every text table — dictionary-driven: any table whose
key has a LANG field), and the list of tables no path reached.
"""
from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable
from dataclasses import replace

from helix.domain.sap.curated import STANDARD_FILTERS
from helix.domain.sap.model import (
    CLIENT_FIELD,
    Filter,
    JoinEdge,
    JoinPlan,
    JoinStep,
    TableDef,
    edges_from_fks,
    normalize_table,
)

# The client table: every SAP table's MANDT is a KEY dependency on it, so without a ban every pair
# of tables would be "two cheap hops apart" through T000 — a join that means nothing.
CLIENT_TABLE = "T000"
DEFAULT_MAX_HOPS = 6

WEIGHT_CURATED = 1.0
WEIGHT_TEXT = 1.0
WEIGHT_FK_KEY = 1.5     # a KEY dependency: the check table's key is part of the host's key
WEIGHT_FK = 2.0         # a REF/TEXT/unknown dependency: a check-table validation, shared by chance
# A REF dependency walked BACKWARDS — from the check table to a table that merely validates a
# field against it — fans out to every row holding that value. AFRU and PRPS both check a unit of
# measure against T006 (AFRU.GMEIN, PRPS.USE04), so with the whole dictionary shipped
# AFRU → T006 → PRPS "joins" a confirmation to every WBS element with the same unit: two hops that
# tie the real three-hop route through the order (AFRU → AUFK → AFPO → PRPS) on cost and beat it on
# hops. The surcharge makes any route that leaves a check table backwards dearer than the curated
# route around it. A KEY dependency is not surcharged: walked backwards it is header → items (the
# child's key extends the parent's), a real relationship.
FANOUT_SURCHARGE = 1.0

# Joins that are valid but usually EMPTY on a production order. With every curated edge at cost 1,
# AFRU → AUFK → PRPS (the header's PSPEL) beats AFRU → AUFK → AFPO → PRPS by a hop and
# AFRU → AFVC → PRPS (the operation's PROJN, blank on nearly every order) ties it — the plan would
# join the WBS on a field that is usually blank and the report would lose its WBS column. These
# edges stay in the graph (they are the only route when AFPO is not wanted, and the right one when
# the user asks for them) but cost more, so a route through the item wins whenever one exists.
# Keyed by the unordered pair of TABLE.FIELD that identifies the join, so orientation is moot.
SECONDARY_SURCHARGE = 1.5
SECONDARY_JOINS: dict[frozenset[str], str] = {
    frozenset({"AUFK.PSPEL", "PRPS.PSPNR"}):
        "the header's WBS element — usually the item's AFPO.PROJN again, or blank",
    frozenset({"AFVC.PROJN", "PRPS.PSPNR"}):
        "a WBS element on the operation itself — rare, usually blank",
}

# When a table is requested twice (PRPS for the item's WBS and again for the settlement
# receiver's), the second instance should arrive by a DIFFERENT join than the first, or it is the
# same rows again under a new alias. Edges the plan already used cost this much more.
USED_EDGE_SURCHARGE = 1.0

_INF = float("inf")


class JoinGraph:
    """Edges indexed by table. `lookup(name) -> TableDef | None` lets the graph pull dictionary
    edges and text-table facts lazily for tables it has not seen (the catalog's reader), so a
    freshly fetched table joins the graph without a rebuild."""

    def __init__(self, curated: Iterable[JoinEdge] = (), *,
                 lookup: Callable[[str], TableDef | None] | None = None,
                 language: str = "E") -> None:
        self._lookup = lookup
        self._language = language
        # Edges live once, keyed by JoinEdge.key (the unordered pair + columns), in the orientation
        # they were given; edges_of() orients them on demand. A table's list holds keys in
        # insertion order so curated order (a deliberate preference) survives as the tie-break.
        self._edges: dict[tuple, JoinEdge] = {}
        self._by_table: dict[str, list[tuple]] = {}
        self._defs: dict[str, TableDef | None] = {}     # lookup cache; None = asked, not found
        self._indexed: set[str] = set()                  # tables whose foreign keys became edges
        self._text: set[str] = set()     # text tables: a LANG key, or a text edge's right side
        for edge in curated:
            self.add_edge(edge)

    # ----- building -----
    def add_table(self, table: TableDef) -> None:
        """Index the table's dictionary foreign keys as edges (idempotent)."""
        name = normalize_table(table.name)
        if not name:
            return
        self._defs[name] = table
        if table.is_text_table:
            self._text.add(name)
        self._indexed.add(name)
        for edge in edges_from_fks(table):
            self.add_edge(edge)

    def add_edge(self, edge: JoinEdge) -> None:
        left, right = normalize_table(edge.left), normalize_table(edge.right)
        if not left or not right or left == right or not edge.on:
            return
        if (left, right) != (edge.left, edge.right):
            edge = replace(edge, left=left, right=right)
        key = edge.key
        old = self._edges.get(key)
        if old is not None and old.kind != "fk" and edge.kind == "fk":
            return      # the dictionary never overrides the expert on the same columns
        if old is not None:
            # A replacement (curated over fk, or a verified edge over its unverified twin) takes
            # the newcomer's position so "curated order" means the order the expert listed.
            del self._edges[key]
            for t in (old.left, old.right):
                keys = self._by_table.get(t, [])
                if key in keys:
                    keys.remove(key)
        self._edges[key] = edge
        for t in (left, right):
            self._by_table.setdefault(t, []).append(key)
        if edge.kind == "text":
            self._text.add(right)

    def _table_def(self, name: str) -> TableDef | None:
        """The dictionary record, through `lookup` once per name; a found table is indexed."""
        if name in self._defs:
            return self._defs[name]
        found: TableDef | None = None
        if self._lookup is not None:
            try:
                found = self._lookup(name)
            except Exception:  # noqa: BLE001 — a catalog that cannot read one table must not sink the whole plan; that table simply has no dictionary edges
                found = None
        self._defs[name] = found
        if found is not None:
            self.add_table(found)
        return found

    # ----- reading -----
    def edges_of(self, table: str) -> tuple[JoinEdge, ...]:
        """Every edge touching `table`, oriented with `table` on the LEFT, curated first, each
        de-duplicated by JoinEdge.key. Pulls the table's dictionary edges through `lookup` when
        it has not been added yet."""
        name = normalize_table(table)
        self._table_def(name)
        oriented = []
        for key in self._by_table.get(name, ()):
            edge = self._edges[key]
            oriented.append(edge if edge.left == name else edge.reversed())
        oriented.sort(key=lambda e: e.kind == "fk")     # stable: curated/text keep their order
        return tuple(oriented)

    def neighbours(self, table: str) -> tuple[str, ...]:
        out: list[str] = []
        for edge in self.edges_of(table):
            if edge.right not in out:
                out.append(edge.right)
        return tuple(out)

    def direct(self, left: str, right: str) -> tuple[JoinEdge, ...]:
        """The edges joining exactly these two tables (left on the left), best first."""
        l_name, r_name = normalize_table(left), normalize_table(right)
        self._table_def(r_name)     # the right side's own foreign keys may be the only edge
        edges = [e for e in self.edges_of(l_name) if e.right == r_name]
        edges.sort(key=self._weight)    # stable: equal weights keep curated order
        return tuple(edges)

    # ----- weights and the intermediate-node rule -----
    def _weight(self, edge: JoinEdge) -> float:
        if edge.kind == "fk":
            key_dep = bool(edge.cardinality) or "(KEY)" in edge.note
            weight = WEIGHT_FK_KEY if key_dep else WEIGHT_FK
            if not key_dep and self._is_fanout(edge):
                weight += FANOUT_SURCHARGE
        elif edge.kind == "text":
            weight = WEIGHT_TEXT
        else:
            weight = WEIGHT_CURATED
        if self._is_secondary(edge):
            weight += SECONDARY_SURCHARGE
        return weight

    def _is_fanout(self, edge: JoinEdge) -> bool:
        """A dictionary edge oriented against the way it was declared: from the check table to
        the table that references it (see FANOUT_SURCHARGE). Edges are stored host → check table,
        so the stored orientation tells."""
        stored = self._edges.get(edge.key)
        return stored is not None and stored.left != edge.left

    @staticmethod
    def _is_secondary(edge: JoinEdge) -> bool:
        return any(
            frozenset({f"{edge.left}.{lf}", f"{edge.right}.{rf}"}) in SECONDARY_JOINS
            for lf, rf in edge.on
        )

    def _is_text_table(self, table: str) -> bool:
        self._table_def(table)      # a LANG key in the dictionary marks it too
        return table in self._text

    def _may_pass_through(self, table: str) -> bool:
        """A text table joins a description onto its base table and nothing else — routing
        THROUGH one (MARA → MAKT → AFPO) is a join on a description table's key, never what
        the user meant. The client table is banned for the reason at CLIENT_TABLE."""
        return table != CLIENT_TABLE and not self._is_text_table(table)

    # ----- shortest paths -----
    def path(self, start: str, goal: str, *,
             max_hops: int = DEFAULT_MAX_HOPS) -> tuple[JoinEdge, ...]:
        """The cheapest chain of edges from `start` to `goal` (each oriented left→right along the
        path), or () when none exists within `max_hops`. Never routes through a client table
        (T000) or a pure text table as an intermediate hop."""
        s, g = normalize_table(start), normalize_table(goal)
        if not s or not g or s == g:
            return ()
        self._table_def(g)      # the goal's own foreign keys may be the only way in
        return self._search((s,), g, max_hops=max_hops)

    @staticmethod
    def _shared_child(e_in: JoinEdge, e_out: JoinEdge) -> bool:
        """1:n in, n:1 out, on the same columns of the middle table: two parents joined through a
        child they share. The child's column is polymorphic — JEST.OBJNR names an order, an
        operation or a WBS element, OBJK.OBKNR any object list — so AFVC → JEST → PRPS is
        AFVC.OBJNR = PRPS.OBJNR across object types: empty. Were A.x = B.x a real relationship
        it would be an edge of its own. (n:1 then 1:n — siblings through a shared parent, as in
        AFRU → AUFK → AFPO — is a real route and stays; the weights keep it off check tables.)"""
        if e_in.cardinality != "1:n" or e_out.cardinality != "n:1":
            return False
        mid_in = {rf for _lf, rf in e_in.on if rf != CLIENT_FIELD}
        mid_out = {lf for lf, _rf in e_out.on if lf != CLIENT_FIELD}
        return bool(mid_out) and mid_out <= mid_in

    def _search(self, sources: Iterable[str], goal: str, *, max_hops: int,
                used: frozenset = frozenset(),
                entered: dict[str, JoinEdge] | None = None) -> tuple[JoinEdge, ...]:
        """Multi-source Dijkstra over (table, arriving edge, hops) states. The hop cap is exact,
        not a cut-off applied after the fact (a cheaper long route must not hide a slightly dearer
        short one); the arriving edge is part of the state because what may follow it depends on
        it (_shared_child). Every source starts at cost 0, so a plan grows from whichever reached
        table is nearest; `entered` says how a plan reached each source, so the rule holds across
        hops the plan took earlier. Ties fall to fewer hops, then to the order edges were listed
        (curated order — a deliberate preference)."""
        entered = entered or {}
        heap: list[tuple[float, int, int, str, tuple | None]] = []
        best: dict[tuple[str, tuple | None, int], float] = {}
        prev: dict[tuple[str, tuple | None, int], tuple[tuple, JoinEdge]] = {}
        seq = 0
        for s in sources:
            e_in = entered.get(s)
            state = (s, e_in.key if e_in is not None else None, 0)
            if s == goal or state in best:
                continue
            best[state] = 0.0
            heapq.heappush(heap, (0.0, 0, seq, s, state[1]))
            seq += 1
        while heap:
            cost, hops, _, table, in_key = heapq.heappop(heap)
            state = (table, in_key, hops)
            if cost > best.get(state, _INF):
                continue        # stale: this state was reached cheaper later
            if table == goal:
                return self._rebuild(prev, state)
            if hops >= max_hops:
                continue
            # A source the plan itself joined earlier is an intermediate too — a text table in
            # the plan is not a place to join the next table from.
            if (hops or in_key is not None) and not self._may_pass_through(table):
                continue
            e_in = prev[state][1] if hops else entered.get(table)
            for edge in self.edges_of(table):
                if edge.key == in_key:
                    continue    # straight back along the edge that brought us here
                if e_in is not None and self._shared_child(e_in, edge):
                    continue
                nxt, nk, nh = edge.right, edge.key, hops + 1
                nc = cost + self._weight(edge) + (USED_EDGE_SURCHARGE if edge.key in used else 0.0)
                # Dominated: the same arrival in fewer hops for no more — nothing beyond it can
                # beat what that state already reaches.
                if any(best.get((nxt, nk, h), _INF) <= nc for h in range(nh + 1)):
                    continue
                best[(nxt, nk, nh)] = nc
                prev[(nxt, nk, nh)] = (state, edge)
                heapq.heappush(heap, (nc, nh, seq, nxt, nk))
                seq += 1
        return ()

    @staticmethod
    def _rebuild(prev: dict, state: tuple) -> tuple[JoinEdge, ...]:
        chain: list[JoinEdge] = []
        while state in prev:
            state, edge = prev[state]
            chain.append(edge)
        chain.reverse()
        return tuple(chain)

    # ----- plans -----
    def plan(self, tables: Iterable[str], *, root: str | None = None,
             standard_filters: dict[str, tuple] | None = None) -> JoinPlan:
        """One JoinPlan reaching every table in `tables` from `root` (default: the first table).
        Steps are in a valid FROM/JOIN order (a step's alias_left is always already present).
        `standard_filters` defaults to curated.STANDARD_FILTERS. Filters are de-duplicated and
        carry the table alias's TABLE name (the SQL writer maps to the alias). Tables that cannot
        be reached are listed in `unreachable` with a note explaining the nearest hop found.

        A table named twice is a request for a second INSTANCE (PRPS for the item's WBS and
        PRPS2 for the settlement receiver's) — it is joined by a route that avoids the joins the
        plan already used, and later hops join onto the FIRST instance. De-duplicate the request
        unless a self-join is what you want."""
        std = STANDARD_FILTERS if standard_filters is None else standard_filters
        requested = [n for n in (normalize_table(t) for t in tables) if n]
        root_name = normalize_table(root) if root else (requested[0] if requested else "")
        if not root_name:
            return JoinPlan(root="", notes=("nothing to plan: no table was named",))
        if root_name in requested:
            requested.remove(root_name)     # the root is consumed once; another mention is a repeat
        for name in (root_name, *requested):
            self._table_def(name)       # their foreign keys (to shared check tables) must be known

        aliases: list[tuple[str, str]] = [(root_name, root_name)]
        reached: list[str] = [root_name]
        steps: list[JoinStep] = []
        used: set[tuple] = set()
        unreachable: list[str] = []
        notes: list[str] = []

        def alias_of(table: str) -> str:
            for alias, t in aliases:
                if t == table:
                    return alias
            return table

        for target in requested:
            repeat = target in reached
            sources = [s for s in reached if s != target]
            hops = ()
            if sources:
                entered = {s.edge.right: s.edge for s in reversed(steps)}   # first instance wins
                hops = self._search(sources, target, max_hops=DEFAULT_MAX_HOPS,
                                    used=frozenset(used) if repeat else frozenset(),
                                    entered=entered)
            if not hops:
                unreachable.append(target)
                notes.append(self._unreachable_note(target, reached, repeat=repeat))
                continue
            for edge in hops:
                if edge.right in reached and not (repeat and edge.right == target):
                    continue    # a reached table in the middle of a path — nothing new to join
                alias_right = self._fresh_alias(edge.right, aliases)
                steps.append(JoinStep(edge=edge, alias_left=alias_of(edge.left),
                                      alias_right=alias_right))
                aliases.append((alias_right, edge.right))
                used.add(edge.key)
                if edge.right not in reached:
                    reached.append(edge.right)

        plan_tables = list(dict.fromkeys(t for _a, t in aliases))
        filters = self._plan_filters(plan_tables, steps, std, notes)
        unverified = [f"{s.edge.left} → {s.edge.right}" for s in steps
                      if s.edge.kind != "fk" and not s.edge.verified]
        if unverified:
            notes.append("curated, unverified against the dictionary (the expert's best route, "
                         "not yet checked): " + ", ".join(unverified))
        return JoinPlan(root=root_name, steps=tuple(steps), aliases=tuple(aliases),
                        filters=filters, unreachable=tuple(unreachable), notes=tuple(notes))

    @staticmethod
    def _fresh_alias(table: str, aliases: list[tuple[str, str]]) -> str:
        taken = {a for a, _t in aliases}
        if table not in taken:
            return table
        n = 2
        while f"{table}{n}" in taken:
            n += 1
        return f"{table}{n}"

    def _plan_filters(self, plan_tables: list[str], steps: list[JoinStep],
                      std: dict[str, tuple], notes: list[str]) -> tuple[Filter, ...]:
        """Standard filters per table, each hop's own filters, then one language filter per text
        table — de-duplicated on (table, field, op, value) so an edge that carries the same
        filter as the table's standard set does not say it twice."""
        out: list[Filter] = []
        seen: set[tuple[str, str, str, str]] = set()

        def add(flt: Filter) -> None:
            ident = (flt.table, flt.field, flt.op, flt.value)
            if ident not in seen:
                seen.add(ident)
                out.append(flt)

        for table in plan_tables:
            for flt in std.get(table, ()):
                add(flt)
        for step in steps:
            for flt in self._edge_filters(step.edge):
                add(flt)
        for table in plan_tables:
            lang = self._language_filter(table, notes)
            if lang is not None:
                add(lang)
        return tuple(out)

    @staticmethod
    def _edge_filters(edge: JoinEdge) -> tuple[Filter, ...]:
        """An edge's filters name their own table (CRHD.OBJTY on AFRU → CRHD, COBRB.KONTY on
        COBRB → PRPS — the LEFT side); a filter that names neither side is taken to be the
        right side's, as the JoinEdge contract says. A reversed edge keeps them right because
        the table name, not the side, is what is kept."""
        sides = (edge.left, edge.right)
        return tuple(f if f.table in sides else replace(f, table=edge.right) for f in edge.filters)

    def _language_filter(self, table: str, notes: list[str]) -> Filter | None:
        tdef = self._table_def(table)
        if tdef is not None:
            key = tdef.language_key()
            if key:
                return Filter(table=table, field=key, op="=", value=self._language,
                              why="one language")
            if table in self._text:
                notes.append(f"{table} is curated as a text table but its dictionary record has no "
                             f"language key — no language filter added; check the field types")
            return None
        if table in self._text:
            # Curated says it is a text table and the dictionary is silent: SAP text tables key on
            # SPRAS by convention, and a missing language filter multiplies every row by the
            # number of languages loaded — assume SPRAS and say so.
            return Filter(table=table, field="SPRAS", op="=", value=self._language,
                          why="one language (SPRAS assumed — the table is not in the dictionary)")
        return None

    def _unreachable_note(self, table: str, reached: list[str], *, repeat: bool) -> str:
        where = ", ".join(reached)
        if repeat:
            head = (f"a second {table}: no join path from {where} other than through {table} "
                    f"itself within {DEFAULT_MAX_HOPS} hops")
        else:
            head = f"{table}: no join path from {where} within {DEFAULT_MAX_HOPS} hops"
        nbrs = self.neighbours(table)
        if nbrs:
            shown = ", ".join(nbrs[:6]) + (" …" if len(nbrs) > 6 else "")
            return f"{head}; {table} joins {shown} — add one of those to bridge it"
        return (f"{head}; {table} has no known joins (not curated, and no dictionary record "
                f"with foreign keys)")

    # ----- words -----
    def explain(self, plan: JoinPlan) -> str:
        """The plan in words, one line per hop: 'AFRU → AUFK on MANDT, AUFNR (n:1, curated, "
        "verified) — the order a confirmation belongs to', then the filters with their reasons."""
        lines = [f"FROM {plan.root}" + ("" if plan.steps else " (no joins)")]
        for step in plan.steps:
            edge = step.edge
            on = ", ".join(lf if lf == rf else f"{lf} = {rf}" for lf, rf in edge.on)
            tags = [edge.cardinality] if edge.cardinality else []
            if edge.kind == "fk":
                tags.append("dictionary")
            else:
                tags += [edge.kind, "verified" if edge.verified else "unverified"]
            line = f"{step.alias_left} → {step.alias_right} on {on} ({', '.join(tags)})"
            if edge.note:
                line += f" — {edge.note}"
            lines.append(line)
        if plan.filters:
            lines.append("Filters:")
            lines += [f"  {_filter_text(f)}" for f in plan.filters]
        if plan.unreachable:
            lines.append("Unreachable: " + ", ".join(plan.unreachable))
        if plan.notes:
            lines.append("Notes:")
            lines += [f"  {n}" for n in plan.notes]
        return "\n".join(lines)


def _filter_text(flt: Filter) -> str:
    """AUFK.AUTYP = '10' — production orders only … (optional). Prose, not SQL: the writer
    (sql.py) renders the real condition; this only has to read right."""
    op = (flt.op or "=").strip().upper()
    ref = f"{flt.table}.{flt.field}"
    if op in ("IS NULL", "IS NOT NULL"):
        cond = f"{ref} {op}"
    elif op == "IN":
        vals = ", ".join(_quoted(v.strip()) for v in flt.value.split(","))
        cond = f"{ref} IN ({vals})"
    else:
        cond = f"{ref} {op} {_quoted(flt.value)}"
    if flt.why:
        cond += f" — {flt.why}"
    if flt.optional:
        cond += " (optional)"
    return cond


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
