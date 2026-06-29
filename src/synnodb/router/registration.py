"""Helpers to register a bespoke engine against a live connection.

Binding an engine needs three facts taken from the *live* DuckDB connection (the
source of truth), so the runtime guards line up with how the engine was built:

* the canonical **output schema** — DuckDB's own ``description`` for the template,
  so the bespoke result is type-locked to DuckDB;
* the **schema fingerprint** of the bound tables, so a later schema drift falls back;
* the set of **tables** the query reads, for dirty-tracking.

``make_binding`` assembles an :class:`EngineBinding` from these; ``register_engine``
adds it to a connection's registry. This is the runtime half of the
factory↔runtime contract — Phase 2c builds the same binding from a ``manifest.json``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, List, Optional, Sequence

from ..errors import SynnoUnsupportedQuery
from .normalize import normalize_sql, tables_in
from .registry import ColumnSpec, EngineBinding, PlaceholderSpec

log = logging.getLogger("synnodb.router.registration")


# Output types the exact-egress path (cpp_helpers/column_egress.hpp) cannot reproduce. Everything
# else - every integer width, decimal128/256, BOOLEAN, DOUBLE/REAL, VARCHAR, DATE, naive TIMESTAMP -
# is emitted exactly (or the engine fails loudly), so the guard is a deny-list, not an allow-list,
# in keeping with the "delegate to arrow::compute::Cast, do not enumerate" design. A query
# producing one of these is refused at bind time and served by DuckDB instead of failing later.
# Note the deny-list must keep pace with DuckDB's type *grammar* (array suffixes ``[]``/``[N]``,
# ``... WITH TIME ZONE``), not just leading tokens; a future hardening could instead probe whether
# DuckDB's Arrow type for the column is reachable from an egress builder family.
_NESTED_OUTPUT_BASES = frozenset({"LIST", "ARRAY", "STRUCT", "MAP", "UNION", "ROW"})
_UNSUPPORTED_OUTPUT_BASES = frozenset(
    {"INTERVAL", "BLOB", "BYTEA", "VARBINARY", "BIT", "UUID", "TIME", "ENUM", "JSON"}
)


def _unsupported_output_reasons(output_schema: Sequence[ColumnSpec]) -> List[str]:
    """Reasons each output column is outside the exact-egress vocabulary (empty = all routable)."""
    reasons: List[str] = []
    for c in output_schema:
        t = str(c.type).upper().strip()
        m = re.match(r"[A-Z0-9_]+", t)
        base = m.group(0) if m else t  # leading type token: TIME vs TIMESTAMP, DECIMAL(38,2)->DECIMAL
        # Nested/array types: DuckDB spells them ``INTEGER[]``, ``INTEGER[3]``, ``STRUCT(...)``, ...
        if re.search(r"\[\s*\d*\s*\]", t) or base in _NESTED_OUTPUT_BASES:
            reasons.append(
                f"output column '{c.name}' has a nested/array type ({c.type}); exact egress emits "
                "flat columns only"
            )
        # Time-zone-bearing timestamps: egress builds a tz-naive timestamp, so the zone is not
        # reproduced - refuse rather than silently emit a tz-naive value DuckDB would qualify.
        elif "TIME ZONE" in t or base in ("TIMESTAMPTZ", "TIMETZ"):
            reasons.append(
                f"output column '{c.name}' type {c.type} carries a time zone, which exact egress "
                "does not reproduce"
            )
        elif base in _UNSUPPORTED_OUTPUT_BASES:
            reasons.append(
                f"output column '{c.name}' type {c.type} is outside the exact-egress vocabulary"
            )
    return reasons


def _connection(conn: Any) -> Any:
    """Return the underlying DuckDB connection from a SynnoConnection or itself."""
    return getattr(conn, "duckdb", conn)


def _literalize(template_sql: str, placeholders: Sequence[PlaceholderSpec]) -> str:
    """Replace ``?``/``$name`` parameters with typed ``CAST(NULL AS <type>)`` literals.

    A parameterized template cannot be executed without values, but its *output
    schema* is independent of the values — so we substitute typed NULLs (typed, so
    expression type inference stays correct) and describe that. Returns the template
    unchanged if it cannot be parsed or has no parameters.
    """
    import sqlglot
    from sqlglot import expressions as exp

    try:
        tree = sqlglot.parse_one(template_sql, read="duckdb")
    except Exception:
        return template_sql
    if tree is None:
        return template_sql

    by_name = {p.name: p.type for p in placeholders}
    counter = {"i": 0}

    def _typed_null(type_str: str) -> "exp.Expression":
        try:
            return exp.Cast(this=exp.Null(), to=exp.DataType.build(type_str, dialect="duckdb"))
        except Exception:
            return exp.Null()

    def repl(node: "exp.Expression") -> "exp.Expression":
        if isinstance(node, exp.Placeholder):  # anonymous ?
            i = counter["i"]
            counter["i"] += 1
            type_str = placeholders[i].type if i < len(placeholders) else "VARCHAR"
            return _typed_null(type_str)
        if isinstance(node, exp.Parameter):  # $name / :name
            return _typed_null(by_name.get(node.name, "VARCHAR"))
        return node

    try:
        return tree.transform(repl).sql(dialect="duckdb")
    except Exception:
        return template_sql


def describe_output(
    conn: Any, template_sql: str, placeholders: Sequence[PlaceholderSpec] = ()
) -> tuple[ColumnSpec, ...]:
    """DuckDB's canonical output schema for *template_sql* (name + type), no rows."""
    duck = _connection(conn)
    sql = _literalize(template_sql, placeholders)
    cursor = duck.execute(f"SELECT * FROM ({sql}) AS _synno_schema LIMIT 0")
    return tuple(ColumnSpec(name=col[0], type=str(col[1])) for col in cursor.description)


def schema_fingerprint(conn: Any, tables: Iterable[str]) -> str:
    """Fingerprint the bound tables' schema using the connection's own helper."""
    fp = getattr(conn, "schema_fingerprint", None)
    if callable(fp):
        return fp(list(tables))
    # Fallback for a raw DuckDB connection.
    from ..duckdb_compat.connection import SynnoConnection

    return SynnoConnection(conn).schema_fingerprint(list(tables))


def make_binding(
    conn: Any,
    *,
    template_sql: str,
    engine: Any,
    query_id: str = "1",
    engine_id: Optional[str] = None,
    placeholders: Sequence[PlaceholderSpec] = (),
    tables: Optional[Iterable[str]] = None,
    scale_factor: Optional[float] = None,
    storage_mode: str = "flat",
) -> EngineBinding:
    """Build an :class:`EngineBinding` for *engine* serving *template_sql* on *conn*."""
    engine_id = engine_id or getattr(engine, "engine_id", "engine")
    normalized = normalize_sql(template_sql)
    if normalized is None:
        raise ValueError(f"template_sql is not parseable: {template_sql!r}")
    bound_tables = frozenset(tables) if tables is not None else frozenset(tables_in(template_sql))
    output_schema = describe_output(conn, template_sql, placeholders)
    # Fail fast: a query whose output types the engine cannot reproduce exactly is refused here
    # (DuckDB serves it) instead of binding and failing later inside egress.
    reasons = _unsupported_output_reasons(output_schema)
    if reasons:
        raise SynnoUnsupportedQuery(reasons, engine_id=engine_id, query_id=query_id)
    fingerprint = schema_fingerprint(conn, bound_tables)
    log.debug(
        "binding %s::%s tables=%s fingerprint=%s output=%s normalized=%r",
        engine_id, query_id, sorted(bound_tables), fingerprint,
        [(c.name, c.type) for c in output_schema], normalized,
    )
    return EngineBinding(
        template_id=f"{engine_id}::{query_id}",
        normalized_sql=normalized,
        query_id=query_id,
        engine_id=engine_id,
        placeholders=tuple(placeholders),
        output_schema=output_schema,
        tables=bound_tables,
        schema_fingerprint=fingerprint,
        scale_factor=scale_factor,
        storage_mode=storage_mode,
        engine=engine,
        template_sql=template_sql,
    )


def register_engine(conn: Any, *, template_sql: str, engine: Any, **kwargs: Any) -> EngineBinding:
    """Build a binding and add it to *conn*'s router registry. Returns the binding."""
    registry = conn.router.registry
    binding = make_binding(conn, template_sql=template_sql, engine=engine, **kwargs)
    registry.register(binding)
    # The engine has just (re)ingested this connection's data, so clear any dirt the
    # initial load left on the bound tables.
    registry.clear_dirty(binding.tables)
    return binding
