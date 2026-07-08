"""Referential (FK-preserving) downscaling of a DuckDB database.

The pre-scaled small scale-factor subsets used to be supplied by the user. This module derives
them instead: given a live DuckDB connection and the workload's queries, it produces a
*referentially closed* downscaled sample - a fraction of the data whose joins still produce
rows - so the cheap correctness rungs remain non-vacuous.

Why not naive per-table sampling
--------------------------------
Sampling every table independently at rate ``f`` retains only ``f**2`` of a two-way join's
matches, so joins collapse to near-empty and the correctness check becomes vacuous (engine vs.
DuckDB both return nothing, so a broken join still "matches"). The sample has to follow the
join graph.

The algorithm (largest-table anchor + parent-ward propagation)
--------------------------------------------------------------
Stated over an arbitrary schema - a set of tables with row counts and a join graph ``G`` of
undirected equi-join edges ``Ti.col <-> Tj.col``:

1. ``ANCHOR`` = the largest table. Sample a deterministic fraction ``f`` of it by hashing its
   key columns: ``hash(key) % K < round(f*K)``. Deterministic (no RNG) so a subset is
   reproducible and cache-keyable.
2. Tables at or below a small-row threshold, and tables with no join path to the anchor, are
   kept **whole** - cheap to keep, and dropping their rows would only lose join coverage.
   Keeping a dimension whole is also what makes ``fact <-> dim`` joins resolve for every kept
   fact row.
3. Every other table is **propagated to** in a deterministic processing order (the anchor
   first, then the rest by ``(distance-from-anchor, name)``). A table's kept set is the rows
   joinable to a neighbour that is *already processed* (nearer the anchor, or an equal-distance
   sibling sorted earlier):

       keep[V] = { r in V : r satisfies some join relationship to an earlier-kept neighbour U }

   Join **relationships** carry provenance so alternative paths and composite keys are combined
   correctly. The column pairs of a single join condition - a composite key such as
   ``lineitem <-> partsupp`` on ``partkey`` *and* ``suppkey`` - are ``AND``-ed within one
   relationship. Distinct relationships between the same table pair - two *alternative* join
   paths, e.g. ``events.user_id`` and ``events.referrer_id`` both referencing ``users`` in
   different queries - are ``OR``-ed. ``AND``-ing alternatives would keep only their
   intersection and leave most kept rows with dangling keys on the path they did not match;
   ``OR`` keeps every path's join non-vacuous.

Two deliberate refinements:

* **Processing-order, not back-edges.** A table is restricted only by neighbours *earlier* in
  the order. This bounds size (a kept fact row cannot pull in a parent's whole fan-out back
  through a child) while still following equal-distance **sibling** edges - two dimensions that
  each join the anchor and also join each other. A strict distance rule drops those sibling
  edges and samples the two tables independently, collapsing their mutual join to ``f**2``.
* **Whole neighbours don't restrict.** A whole table's key column already contains the full
  domain, so ``V.fk IN (whole_dim.pk)`` matches *every* ``V`` row and is useless as a filter
  (it would make ``V`` whole too). We therefore never propagate *through* a whole table; the
  whole dimension is simply present so ``V``'s references resolve.

Both directly address the "propagation blow-up" and low-cardinality-edge risks. Realized subset
size is emergent (propagation pulls in joinable rows) and is logged per table - no silent
truncation.

Sinks
-----
The same plan feeds three sinks (only the sink differs): ephemeral DuckDB **temp tables**
(``keep_<table>``, the subset consumed in-connection); a standalone **subset.duckdb** database
(``fraction<f>/subset.duckdb``, the DuckDB-native default the engine ingests over shm and the oracle
materializes flat from); and a **parquet** materialization (``fraction<f>/<table>.parquet``, the
internal fallback). Nothing is written back to the caller's database.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import duckdb

from synnodb.workloads.dataset.custom_scaler.scale_parquet import (
    _list_tables,
    _load_table_info,
    _quote_ident,
)

logger = logging.getLogger(__name__)

# Hash-sample resolution: hash(key) % K < round(f*K). 1000 gives fractions to 0.1% granularity.
_HASH_MODULUS = 1000

# A [PLACEHOLDER] token in a BYO query template - replaced with a literal so the SQL parses.
_PLACEHOLDER_RE = re.compile(r"\[[A-Za-z0-9_]+\]")


@dataclass(frozen=True)
class JoinEdge:
    """An undirected single-column equi-join relationship ``table_a.col_a == table_b.col_b``.

    Normalized so ``(table_a, col_a) <= (table_b, col_b)`` lexically; this is the one object the
    downscaler follows (§6.2 of the design). No key/FK direction is carried - the sampler only
    follows edges and keeps joinable rows, so it never needs to know which side is the key.
    """

    table_a: str
    col_a: str
    table_b: str
    col_b: str

    @staticmethod
    def make(ta: str, ca: str, tb: str, cb: str) -> "JoinEdge":
        left, right = sorted([(ta, ca), (tb, cb)])
        return JoinEdge(left[0], left[1], right[0], right[1])

    def other(self, table: str) -> tuple[str, str, str] | None:
        """``(own_col, other_table, other_col)`` for ``table``'s side of the edge, or None if
        this edge does not touch ``table``. A self-edge (both sides the same table) returns
        None - self-referential edges are out of scope (v1)."""
        if self.table_a == self.table_b:
            return None
        if table == self.table_a:
            return (self.col_a, self.table_b, self.col_b)
        if table == self.table_b:
            return (self.col_b, self.table_a, self.col_a)
        return None


@dataclass(frozen=True)
class JoinRelationship:
    """A single join condition between two distinct tables: one or more equi-join column pairs
    matched *together*. The pairs of one condition (a composite key) are ``AND``-ed; the
    downscaler ``OR``s across *distinct* relationships. Normalized so ``table_a <= table_b`` with
    the pairs aligned to that order and sorted, making the relationship hashable and dedupable
    across the queries/constraints that named it.
    """

    table_a: str
    table_b: str
    pairs: tuple[tuple[str, str], ...]  # (col_a, col_b) aligned to (table_a, table_b)

    @staticmethod
    def from_equalities(
        equalities: Sequence[tuple[str, str, str, str]],
    ) -> "JoinRelationship":
        """Build a relationship from ``(ta, ca, tb, cb)`` equalities that all connect the same
        unordered table pair (one join condition's column pairs)."""
        table_pairs = {tuple(sorted((ta, tb))) for ta, _, tb, _ in equalities}
        if len(table_pairs) != 1:
            raise ValueError(
                "JoinRelationship.from_equalities needs one table pair, got "
                f"{sorted(table_pairs)}."
            )
        table_a, table_b = next(iter(table_pairs))
        pairs: set[tuple[str, str]] = set()
        for ta, ca, tb, cb in equalities:
            pairs.add((ca, cb) if (ta, tb) == (table_a, table_b) else (cb, ca))
        return JoinRelationship(table_a, table_b, tuple(sorted(pairs)))

    def other(self, table: str) -> tuple[list[str], str, list[str]] | None:
        """``(own_cols, other_table, other_cols)`` for ``table``'s side, or None if the
        relationship does not touch ``table`` or is a self-join (out of scope, v1)."""
        if self.table_a == self.table_b:
            return None
        if table == self.table_a:
            return ([a for a, _ in self.pairs], self.table_b, [b for _, b in self.pairs])
        if table == self.table_b:
            return ([b for _, b in self.pairs], self.table_a, [a for a, _ in self.pairs])
        return None


# --------------------------------------------------------------------------- introspection
@dataclass
class SchemaInfo:
    """Everything the downscaler reads off the connection once, up front."""

    tables: list[str]
    row_counts: dict[str, int]
    columns: dict[str, list[str]]  # table -> ordered column names
    pk_columns: dict[str, list[str]]  # table -> declared PK columns (may be empty)
    col_owner: dict[str, set[str]]  # lowercased column name -> tables that have it


def introspect(con: duckdb.DuckDBPyConnection) -> SchemaInfo:
    tables = _list_tables(con)
    row_counts: dict[str, int] = {}
    columns: dict[str, list[str]] = {}
    pk_columns: dict[str, list[str]] = {}
    col_owner: dict[str, set[str]] = defaultdict(set)
    for t in tables:
        info = _load_table_info(con, t)
        cols = list(info.keys())
        columns[t] = cols
        pk_columns[t] = [c for c, meta in info.items() if meta.get("pk")]
        for c in cols:
            col_owner[c.lower()].add(t)
        row_counts[t] = int(
            con.execute(f"SELECT COUNT(*) FROM {_quote_ident(t)}").fetchone()[0]
        )
    return SchemaInfo(tables, row_counts, columns, pk_columns, dict(col_owner))


def declared_constraint_edges(con: duckdb.DuckDBPyConnection) -> list[JoinEdge]:
    """Foreign-key relationships declared in the catalog, normalized to join edges.

    Free and exact when present, but ``dbgen`` TPC-H declares none, so this is only ever extra
    signal - never required. A composite FK is decomposed into one edge per column pair (v1
    handles single-column edges; a composite relationship then shows up as several of them)."""
    try:
        rows = con.execute(
            "SELECT table_name, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() "
            "WHERE constraint_type = 'FOREIGN KEY'"
        ).fetchall()
    except Exception:  # older/newer builds may not expose duckdb_constraints()
        return []
    edges: list[JoinEdge] = []
    for table, from_cols, ref_table, to_cols in rows:
        if not (table and ref_table and from_cols and to_cols):
            continue
        for fc, tc in zip(list(from_cols), list(to_cols)):
            edges.append(JoinEdge.make(str(table), str(fc), str(ref_table), str(tc)))
    return edges


# ------------------------------------------------------------------ join inference from SQL
def _strip_placeholders(sql: str) -> str:
    """Replace ``[PLACEHOLDER]`` holes with a harmless literal so the template parses. The
    replacement only needs to be a valid token in every position a placeholder appears
    (scalar, quoted string body, IN-list); a bare ``1`` satisfies all of them."""
    return _PLACEHOLDER_RE.sub("1", sql)


def _infer_equalities(
    sql: str, schema: SchemaInfo, dialect: str = "duckdb"
) -> list[tuple[str, str, str, str]]:
    """Cross-table equi-join column pairs ``(table_a, col_a, table_b, col_b)`` from one query.

    Every ``FROM a JOIN b ON a.x = b.y`` (and the implicit ``WHERE a.x = b.y`` form) names a
    real join. Each column is resolved to its table via the query's alias map, falling back to
    the unique owner of an unqualified column name. Only equalities between two columns of
    *different* tables are returned; column-vs-literal predicates are ignored. Table names are
    the schema's real casing so downstream lookups never miss on case.
    """
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except Exception:  # pragma: no cover - sqlglot is a core dependency
        logger.warning("sqlglot unavailable; cannot infer join edges from SQL")
        return []

    try:
        trees = sqlglot.parse(_strip_placeholders(sql), read=dialect)
    except Exception as e:
        logger.warning("Skipping unparseable query for join inference: %s", e)
        return []

    real_tables = {t.lower(): t for t in schema.tables}
    equalities: list[tuple[str, str, str, str]] = []
    for tree in trees:
        if tree is None:
            continue
        # alias (and bare name) -> real table name, for every table reference in the statement
        alias_map: dict[str, str] = {}
        for tnode in tree.find_all(exp.Table):
            real = real_tables.get(tnode.name.lower())
            if real is None:
                continue
            alias_map[tnode.alias_or_name.lower()] = real
            alias_map[tnode.name.lower()] = real

        def resolve(col: "exp.Column") -> tuple[str, str] | None:
            qualifier = col.table
            if qualifier:
                table = alias_map.get(qualifier.lower())
            else:
                owners = schema.col_owner.get(col.name.lower(), set())
                table = next(iter(owners)) if len(owners) == 1 else None
            if table is None:
                return None
            return (table, col.name)

        for eq in tree.find_all(exp.EQ):
            lhs, rhs = eq.this, eq.expression
            if not (isinstance(lhs, exp.Column) and isinstance(rhs, exp.Column)):
                continue
            left = resolve(lhs)
            right = resolve(rhs)
            if left is None or right is None or left[0] == right[0]:
                continue
            equalities.append((left[0], left[1], right[0], right[1]))
    return equalities


def infer_join_edges_from_sql(
    sql: str, schema: SchemaInfo, dialect: str = "duckdb"
) -> set[JoinEdge]:
    """The undirected single-column join edges named by one query (see :func:`_infer_equalities`)."""
    return {
        JoinEdge.make(ta, ca, tb, cb)
        for ta, ca, tb, cb in _infer_equalities(sql, schema, dialect)
    }


def infer_join_relationships_from_sql(
    sql: str, schema: SchemaInfo, dialect: str = "duckdb"
) -> list["JoinRelationship"]:
    """The join *relationships* named by one query: the equi-join column pairs grouped by the
    unordered table pair they connect, so a query's composite key (several column pairs between
    the same two tables) becomes one ``AND``-combined relationship rather than separate edges."""
    by_pair: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
    for ta, ca, tb, cb in _infer_equalities(sql, schema, dialect):
        by_pair[tuple(sorted((ta, tb)))].append((ta, ca, tb, cb))
    return [JoinRelationship.from_equalities(eqs) for eqs in by_pair.values()]


def _normalize_explicit(
    relationships: Iterable[tuple[str, str]] | Iterable[Sequence[str]],
    schema: SchemaInfo,
) -> list[JoinEdge]:
    """Turn caller-supplied ``(table_a.col, table_b.col)`` pairs into join edges.

    Each side is ``"table.column"``; both tables must exist. Single-column equi-joins only -
    a clear error otherwise, matching the project's "reject complex shapes" stance. Table names
    are matched case-insensitively but stored in the schema's real casing, so a caller who
    writes ``Orders.o_custkey`` still resolves to the ``orders`` table everywhere downstream."""
    out: list[JoinEdge] = []
    real = {t.lower(): t for t in schema.tables}
    for rel in relationships:
        rel = list(rel)
        if len(rel) != 2:
            raise ValueError(
                f"join_relationships entry {rel!r} must be a pair of 'table.column' strings."
            )
        parsed: list[tuple[str, str]] = []
        for side in rel:
            if not isinstance(side, str) or side.count(".") != 1:
                raise ValueError(
                    f"join_relationships side {side!r} must be 'table.column' "
                    "(single-column equi-joins only)."
                )
            tbl, col = side.split(".")
            if tbl.lower() not in real:
                raise ValueError(
                    f"join_relationships references unknown table {tbl!r}. "
                    f"Known tables: {sorted(schema.tables)}"
                )
            parsed.append((real[tbl.lower()], col))
        (ta, ca), (tb, cb) = parsed
        out.append(JoinEdge.make(ta, ca, tb, cb))
    return out


def build_join_graph(
    schema: SchemaInfo,
    sql_by_id: dict[str, str] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    explicit_relationships: Iterable[Sequence[str]] | None = None,
) -> set[JoinEdge]:
    """The workload's join graph, unioned from every available source (§6.2):

    1. **query JOINs** (the lead signal - always available, needs zero user input),
    2. **declared FK constraints** (unioned in when present; absent on ``dbgen`` TPC-H),
    3. **explicit ``join_relationships``** (a caller override for anything inference misses).

    All three normalize to the same undirected :class:`JoinEdge`, so they simply union. Extra
    (even spurious) edges are safe: an edge only ever adds an ``OR`` term that keeps *more*
    rows, never fewer - it can never break referential closure.
    """
    edges: set[JoinEdge] = set()
    for sql in (sql_by_id or {}).values():
        edges |= infer_join_edges_from_sql(sql, schema)
    if con is not None:
        edges.update(declared_constraint_edges(con))
    if explicit_relationships:
        edges.update(_normalize_explicit(explicit_relationships, schema))
    return edges


def declared_constraint_relationships(
    con: duckdb.DuckDBPyConnection,
) -> list[JoinRelationship]:
    """Declared foreign keys as join relationships - one per constraint, so a composite FK stays
    a single ``AND``-combined relationship (unlike :func:`declared_constraint_edges`, which
    flattens it to independent single-column edges)."""
    try:
        rows = con.execute(
            "SELECT table_name, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() "
            "WHERE constraint_type = 'FOREIGN KEY'"
        ).fetchall()
    except Exception:  # older/newer builds may not expose duckdb_constraints()
        return []
    rels: list[JoinRelationship] = []
    for table, from_cols, ref_table, to_cols in rows:
        if not (table and ref_table and from_cols and to_cols):
            continue
        eqs = [
            (str(table), str(fc), str(ref_table), str(tc))
            for fc, tc in zip(list(from_cols), list(to_cols))
        ]
        if eqs:
            rels.append(JoinRelationship.from_equalities(eqs))
    return rels


def build_join_relationships(
    schema: SchemaInfo,
    sql_by_id: dict[str, str] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    explicit_relationships: Iterable[Sequence[str]] | None = None,
) -> list[JoinRelationship]:
    """The workload's join graph as provenance-carrying relationships (same three sources as
    :func:`build_join_graph`), deduplicated. Each query's composite key is one relationship;
    alternative join paths between the same tables stay distinct relationships, so the downscaler
    ``OR``s rather than intersects them."""
    rels: list[JoinRelationship] = []
    for sql in (sql_by_id or {}).values():
        rels.extend(infer_join_relationships_from_sql(sql, schema))
    if con is not None:
        rels.extend(declared_constraint_relationships(con))
    if explicit_relationships:
        for edge in _normalize_explicit(explicit_relationships, schema):
            rels.append(
                JoinRelationship.from_equalities(
                    [(edge.table_a, edge.col_a, edge.table_b, edge.col_b)]
                )
            )
    # The same relationship is typically named by many queries; dedup preserves first-seen order.
    return list(dict.fromkeys(rels))


# --------------------------------------------------------------------------- the downscaler
@dataclass
class SubsetTable:
    """One table's contribution to a subset: how its kept set is defined."""

    table: str
    mode: str  # "anchor" | "whole" | "sample"
    sql: str  # SELECT that yields the kept rows (over source tables / keep_* temp tables)
    kept_rows: int | None = None


@dataclass
class SubsetResult:
    fraction: float
    anchor: str
    tables: list[SubsetTable] = field(default_factory=list)


class ReferentialDownscaler:
    """Builds referentially-closed downscaled subsets off a live DuckDB connection.

    Introspection and the join graph are computed once; :meth:`materialize_temp_subset` and
    :meth:`copy_subset_to_parquet` then realize any fraction. The caller's database is only read;
    all intermediate state lives in session-local ``TEMP`` tables that are dropped when done.
    """

    #: Prefix for the ephemeral per-table kept-set tables (session-local temp schema).
    KEEP_PREFIX = "_synno_keep_"

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        sql_by_id: dict[str, str] | None = None,
        join_relationships: Iterable[Sequence[str]] | None = None,
        whole_table_threshold: int = 10_000,
    ):
        self.con = con
        self.schema = introspect(con)
        self.edges = build_join_graph(
            self.schema,
            sql_by_id=sql_by_id,
            con=con,
            explicit_relationships=join_relationships,
        )
        # Provenance-carrying view of the same graph: the semi-join predicate ANDs a composite
        # key's columns within one relationship and ORs across alternative relationships.
        self.relationships = build_join_relationships(
            self.schema,
            sql_by_id=sql_by_id,
            con=con,
            explicit_relationships=join_relationships,
        )
        self.whole_table_threshold = whole_table_threshold
        self._reject_self_referential_edges()
        self.adjacency = self._build_adjacency()

    # -- graph helpers ------------------------------------------------------
    def _build_adjacency(self) -> dict[str, list[tuple[str, str, str]]]:
        """table -> list of ``(own_col, other_table, other_col)`` incident edges."""
        adj: dict[str, list[tuple[str, str, str]]] = {t: [] for t in self.schema.tables}
        for e in self.edges:
            side = e.other(e.table_a)
            if side is None:  # self-edge, skipped above
                continue
            adj[e.table_a].append(side)
            adj[e.table_b].append(e.other(e.table_b))  # type: ignore[arg-type]
        return adj

    def _reject_self_referential_edges(self) -> None:
        """Self-referential edges are unsupported in v1 (§11); reject with a clear message."""
        for e in self.edges:
            if e.table_a == e.table_b:
                raise ValueError(
                    f"Self-referential join edge on {e.table_a!r} "
                    f"({e.col_a} == {e.col_b}) is not supported (v1 handles single-column "
                    "edges between distinct tables)."
                )

    def anchor(self) -> str:
        """The largest table (ties broken by name for determinism)."""
        return max(sorted(self.schema.tables), key=lambda t: self.schema.row_counts[t])

    def _bfs_depth(self, anchor: str) -> dict[str, int]:
        depth = {anchor: 0}
        q: deque[str] = deque([anchor])
        while q:
            u = q.popleft()
            for _, v, _ in self.adjacency[u]:
                if v not in depth:
                    depth[v] = depth[u] + 1
                    q.append(v)
        return depth

    def _sample_key(self, anchor: str) -> str:
        """The ``hash(...)`` argument list used to sample the anchor deterministically:
        declared PK if any, else the anchor's join-key columns, else all columns."""
        cols = self.schema.pk_columns.get(anchor) or sorted(
            {own for own, _, _ in self.adjacency[anchor]}
        )
        if not cols:
            cols = self.schema.columns[anchor]
        return ", ".join(_quote_ident(c) for c in cols)

    # -- planning -----------------------------------------------------------
    def _keep_name(self, table: str) -> str:
        """The quoted identifier of ``table``'s keep-set temp table. Quoting keeps the SQL valid
        for source tables whose names need it (spaces, reserved words, embedded quotes)."""
        return _quote_ident(f"{self.KEEP_PREFIX}{table}")

    def plan_subset(self, fraction: float) -> SubsetResult:
        """Compute each table's kept-set SELECT for ``fraction`` without executing anything.

        The SELECTs reference source tables and the ``keep_*`` temp tables of *closer* tables,
        so executing them in the returned order materializes a consistent subset.
        """
        if not (0.0 < fraction <= 1.0):
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")

        anchor = self.anchor()
        counts = self.schema.row_counts
        depth = self._bfs_depth(anchor)
        whole = {
            t
            for t in self.schema.tables
            if t != anchor and counts[t] <= self.whole_table_threshold
        }

        result = SubsetResult(fraction=fraction, anchor=anchor)
        if fraction >= 1.0:
            # Benchmark subset: the full dataset, every table as-is.
            for t in self.schema.tables:
                result.tables.append(
                    SubsetTable(t, "whole", f"SELECT * FROM {_quote_ident(t)}")
                )
            return result

        # anchor: deterministic hash sample
        keep_cut = round(fraction * _HASH_MODULUS)
        if keep_cut <= 0:
            raise ValueError(
                f"fraction {fraction} is below the sampling granularity "
                f"{1.0 / _HASH_MODULUS:g} (hash resolution {_HASH_MODULUS}); the anchor sample "
                "would be empty. Use a larger fraction."
            )
        result.tables.append(
            SubsetTable(
                anchor,
                "anchor",
                f"SELECT * FROM {_quote_ident(anchor)} "
                f"WHERE hash({self._sample_key(anchor)}) % {_HASH_MODULUS} < {keep_cut}",
            )
        )

        # Deterministic processing order: anchor first, then by (distance-from-anchor, name). A
        # table is restricted only by neighbours *earlier* in this order, so equal-distance
        # siblings still restrict each other (the later-sorted one follows the edge).
        ordered = sorted(
            (t for t in self.schema.tables if t != anchor),
            key=lambda t: (depth.get(t, 1 << 30), t),
        )
        order_index = {t: i for i, t in enumerate([anchor, *ordered])}

        def semi_join_predicate(table: str) -> str | None:
            """OR-across-relationships semi-join predicate over already-processed, sampled
            neighbours. None if the table has no such neighbour, so it is kept whole. A composite
            relationship matches its column pairs as one tuple - the whole key must come from a
            single kept parent row (a correlated EXISTS), not each column independently, or a
            composite child key could survive with no matching parent. Alternative join paths
            between the same tables are separate relationships and are ORed."""
            own_quoted = _quote_ident(table)
            terms: list[str] = []
            for rel in self.relationships:
                side = rel.other(table)
                if side is None:
                    continue
                own_cols, other, other_cols = side
                if other in whole or order_index.get(other, 1 << 30) >= order_index[table]:
                    continue  # whole neighbours don't restrict; only earlier-processed ones
                keep = self._keep_name(other)
                match = " AND ".join(
                    f"{keep}.{_quote_ident(rc)} = {own_quoted}.{_quote_ident(oc)}"
                    for oc, rc in zip(own_cols, other_cols)
                )
                terms.append(f"EXISTS (SELECT 1 FROM {keep} WHERE {match})")
            if not terms:
                return None
            return " OR ".join(dict.fromkeys(terms))

        for t in ordered:
            pred = None if t in whole else semi_join_predicate(t)
            if pred is None:
                # small dim, disconnected, or reachable only through whole tables -> keep whole
                result.tables.append(
                    SubsetTable(t, "whole", f"SELECT * FROM {_quote_ident(t)}")
                )
            else:
                result.tables.append(
                    SubsetTable(
                        t, "sample", f"SELECT * FROM {_quote_ident(t)} WHERE {pred}"
                    )
                )
        return result

    # -- materialization ----------------------------------------------------
    def _drop_keep_tables(self) -> None:
        for t in self.schema.tables:
            self.con.execute(f"DROP TABLE IF EXISTS {self._keep_name(t)}")

    def materialize_temp_subset(self, fraction: float) -> SubsetResult:
        """Create one ``TEMP TABLE _synno_keep_<table>`` per table for ``fraction`` and return
        the realized row counts. The temp tables are the subset (native sink); the caller reads
        them and is responsible for cleanup via :meth:`drop`.
        """
        plan = self.plan_subset(fraction)
        self._drop_keep_tables()
        # A single pass in plan order is the fixpoint: tables are materialized nearest-anchor
        # first, and each sampled table's predicate references only the keep_* sets of strictly
        # closer tables, which are already final. Parent-ward propagation follows no back-edges,
        # so no keep_* set can grow once built - re-evaluation would be a no-op.
        for tt in plan.tables:
            self.con.execute(
                f"CREATE TEMP TABLE {self._keep_name(tt.table)} AS {tt.sql}"
            )
        for tt in plan.tables:
            tt.kept_rows = self.con.execute(
                f"SELECT COUNT(*) FROM {self._keep_name(tt.table)}"
            ).fetchone()[0]
        self._log_subset(plan)
        return plan

    def _materialize_subset(
        self, fraction: float, sink: Callable[[SubsetTable, str], None]
    ) -> SubsetResult:
        """Shared subset-materialization skeleton: resolve the plan for ``fraction`` and drive
        ``sink(subset_table, source_relation)`` once per table.

        ``source_relation`` is the table's ``keep_*`` temp table for a fractional subset or the
        source table itself for the full subset. Fractional subsets build the ``keep_*`` temp tables
        up front and drop them afterwards; the full subset reads the source directly and back-fills
        row counts from the schema. Callers supply only the per-table sink and manage any output
        resource (a connection, a directory) around this call.
        """
        plan = (
            self.materialize_temp_subset(fraction)
            if fraction < 1.0
            else self.plan_subset(fraction)
        )
        try:
            for tt in plan.tables:
                source = (
                    self._keep_name(tt.table)
                    if fraction < 1.0
                    else _quote_ident(tt.table)
                )
                sink(tt, source)
            if fraction >= 1.0:
                # No temp tables were created; count rows for the summary from the source.
                for tt in plan.tables:
                    tt.kept_rows = self.schema.row_counts[tt.table]
                self._log_subset(plan)
        finally:
            if fraction < 1.0:
                self._drop_keep_tables()
        return plan

    def copy_subset_to_parquet(self, fraction: float, out_dir: Path | str) -> SubsetResult:
        """Materialize ``fraction`` and write ``<out_dir>/<table>.parquet`` for every table.

        Whole tables are copied straight from the source (no temp table needed); sampled tables
        are materialized as temp tables first so the parquet exactly matches what the subset
        contains. Column types are preserved by ``COPY (SELECT * ...) TO parquet``.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        def sink(tt: SubsetTable, source: str) -> None:
            dest = (out / f"{tt.table}.parquet").as_posix().replace("'", "''")
            self.con.execute(
                f"COPY (SELECT * FROM {source}) TO '{dest}' (FORMAT 'parquet')"
            )

        return self._materialize_subset(fraction, sink)

    def copy_subset_to_duckdb(
        self, fraction: float, out_db_path: Path | str
    ) -> SubsetResult:
        """Materialize ``fraction`` into a standalone ``subset.duckdb`` at ``out_db_path`` - the
        DuckDB-native sink. Each kept table becomes a plainly-named table in the new database
        (``lineitem``, ``orders``, …), which the candidate engine ingests over the shm plane and
        the DuckDB oracle materializes flat from. No parquet is written.

        Rows are transferred table-by-table as Arrow into a fresh *writable* connection, so this
        works even when the caller's source connection is read-only (a direct ``ATTACH`` from a
        read-only connection cannot create the new file). Arrow preserves DuckDB's exact column
        types. The output file must not already exist; the caller handles idempotency.

        The database is built in a ``.partial`` sibling and atomically renamed into place only
        after every table is written, so an interrupted run never leaves a
        complete-*looking* subset.duckdb behind.
        """
        out_db = Path(out_db_path)
        if out_db.exists():
            raise FileExistsError(f"subset database already exists: {out_db}")
        out_db.parent.mkdir(parents=True, exist_ok=True)
        tmp_db = out_db.with_name(out_db.name + ".partial")
        tmp_wal = tmp_db.with_name(tmp_db.name + ".wal")
        tmp_db.unlink(missing_ok=True)
        tmp_wal.unlink(missing_ok=True)
        subset_con = duckdb.connect(str(tmp_db))

        def sink(tt: SubsetTable, source: str) -> None:
            arrow_tbl = self.con.execute(f"SELECT * FROM {source}").to_arrow_table()
            subset_con.register("_synno_src", arrow_tbl)
            subset_con.execute(
                f"CREATE TABLE {_quote_ident(tt.table)} AS SELECT * FROM _synno_src"
            )
            subset_con.unregister("_synno_src")

        try:
            result = self._materialize_subset(fraction, sink)
            subset_con.execute("CHECKPOINT")
        except BaseException:
            subset_con.close()
            tmp_db.unlink(missing_ok=True)
            tmp_wal.unlink(missing_ok=True)
            raise
        subset_con.close()
        tmp_wal.unlink(missing_ok=True)
        tmp_db.replace(out_db)
        return result

    def drop(self) -> None:
        """Drop the ephemeral ``keep_*`` temp tables (idempotent)."""
        self._drop_keep_tables()

    def _log_subset(self, plan: SubsetResult) -> None:
        total = sum(t.kept_rows or 0 for t in plan.tables)
        logger.info(
            "Downscaled subset fraction=%s (anchor=%s): %d rows across %d tables",
            plan.fraction,
            plan.anchor,
            total,
            len(plan.tables),
        )
        for tt in sorted(plan.tables, key=lambda t: t.table):
            full = self.schema.row_counts[tt.table]
            pct = (100.0 * (tt.kept_rows or 0) / full) if full else 0.0
            logger.info(
                "  %-16s %10d / %-10d (%5.1f%%)  [%s]",
                tt.table,
                tt.kept_rows or 0,
                full,
                pct,
                tt.mode,
            )


# --------------------------------------------------------------------------- source snapshot
def _unlink_duckdb_files(db_path: Path) -> None:
    """Remove a DuckDB database file and its sidecar WAL, if present."""
    db_path.unlink(missing_ok=True)
    db_path.with_name(db_path.name + ".wal").unlink(missing_ok=True)


def _snapshot_via_copy_database(source_con: duckdb.DuckDBPyConnection, out_db: Path) -> None:
    """Snapshot with ``COPY FROM DATABASE`` - one statement, one transaction, full schema
    (including declared constraints). Needs a read-write source connection to attach a writable
    target; raises for a read-only one, which cannot."""
    rows = source_con.execute("PRAGMA database_list").fetchall()
    if not rows:
        raise RuntimeError("source connection exposes no database to snapshot")
    src_name = rows[0][1]  # (seq, name, file): the primary database's catalog name
    alias = _quote_ident("synno_snapshot_target")
    attach_path = str(out_db).replace("'", "''")
    source_con.execute(f"ATTACH '{attach_path}' AS {alias}")
    try:
        source_con.execute(f"COPY FROM DATABASE {_quote_ident(src_name)} TO {alias}")
        source_con.execute(f"CHECKPOINT {alias}")  # fold the WAL so the file is self-contained
    finally:
        source_con.execute(f"DETACH {alias}")


def _snapshot_via_arrow(source_con: duckdb.DuckDBPyConnection, out_db: Path) -> None:
    """Snapshot table-by-table as Arrow, wrapped in a single source transaction so the copy is
    internally consistent. The fallback for a read-only source connection (COPY FROM DATABASE
    cannot attach a writable target through one). Exact column types are preserved; declared
    constraints - optional join-graph signal - are not."""
    dest = duckdb.connect(str(out_db))
    source_con.execute("BEGIN TRANSACTION")
    try:
        for table in _list_tables(source_con):
            arrow_tbl = source_con.execute(
                f"SELECT * FROM {_quote_ident(table)}"
            ).to_arrow_table()
            dest.register("_synno_src", arrow_tbl)
            dest.execute(
                f"CREATE TABLE {_quote_ident(table)} AS SELECT * FROM _synno_src"
            )
            dest.unregister("_synno_src")
        dest.execute("CHECKPOINT")
    finally:
        source_con.execute("ROLLBACK")  # read-only snapshot: never commit
        dest.close()


def snapshot_source_database(
    source_con: duckdb.DuckDBPyConnection, out_db_path: Path | str
) -> None:
    """Copy a consistent point-in-time snapshot of ``source_con``'s primary database to a
    standalone DuckDB file at ``out_db_path`` that SynnoDB then owns.

    This is what lets a caller keep *writing* to their database in parallel with benchmark
    building: the benchmark subset and every downscaled rung are derived from this frozen image,
    never from the moving source. The snapshot is taken under a single transaction (DuckDB
    snapshot isolation), so it is internally consistent across all tables - no table can reflect a
    newer commit than another.

    ``COPY FROM DATABASE`` is preferred (it also reproduces declared constraints, extra signal for
    the join graph); a read-only source connection - which cannot attach a writable target - falls
    back to a transaction-wrapped Arrow copy. The database is built in a ``.partial`` sibling and
    atomically renamed into place, so an interrupted snapshot never leaves a complete-looking file
    behind.
    """
    out_db = Path(out_db_path)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = out_db.with_name(out_db.name + ".partial")
    _unlink_duckdb_files(tmp_db)
    try:
        _snapshot_via_copy_database(source_con, tmp_db)
    except Exception as exc:
        logger.info(
            "COPY FROM DATABASE snapshot unavailable (%s); falling back to Arrow snapshot",
            exc,
        )
        _unlink_duckdb_files(tmp_db)
        _snapshot_via_arrow(source_con, tmp_db)
    tmp_db.with_name(tmp_db.name + ".wal").unlink(missing_ok=True)
    _unlink_duckdb_files(out_db)
    tmp_db.replace(out_db)
