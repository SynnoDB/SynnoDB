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
from typing import Any, Iterable, Optional, Sequence

from .normalize import normalize_sql, tables_in
from .registry import ColumnSpec, EngineBinding, PlaceholderSpec

log = logging.getLogger("synnodb.router.registration")


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
