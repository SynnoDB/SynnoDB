"""Pick parameter values for templated workload queries from the data.

A bring-your-own query can be a template with ``[PLACEHOLDER]`` holes (the TPC-H
convention). To generate the correctness sweep and a parameterized engine we need real
values to fill them in, without writing a generator per query.

Approach:

  1. For each placeholder, sample a pool of candidate values from the column it is
     compared against in the query (via DuckDB). A placeholder used as an INTERVAL offset
     gets an integer sweep sized to that column's date span.
  2. Sample whole assignments and run the query in DuckDB, keeping the ones that return
     rows. Running the real query handles arithmetic (``- interval [DELTA] day``), a
     placeholder used in several places (``[NATION1]`` twice), correlated placeholders,
     joins and ``BETWEEN``/``IN``/``LIKE`` without any per-operator code.
  3. Raise if no assignment produces a non-empty result, rather than emit a broken query.
"""
from __future__ import annotations

import datetime
import decimal
import logging
import os
import random
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from synnodb.workloads.workload_provider import DEFAULT_NUM_INSTANTIATIONS

logger = logging.getLogger(__name__)


def configure_byo_debug() -> bool:
    """Turn on verbose param-inference debug logging.

    Off by default. With ``SYNNODB_BYO_DEBUG=1`` the inference logs, per query, the
    placeholder->column bindings, candidate pools, accepted/rejected instantiations, the
    derived schema, and the SQL + args line the engine will receive. Same env-var pattern
    as SYNNODB_WORKER_LOG.
    """
    on = os.environ.get("SYNNODB_BYO_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    if on:
        for name in ("synnodb.workloads.param_infer", "synnodb.workloads.byo_workload"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        root = logging.getLogger()
        if not root.handlers:  # standalone (registration before the framework logging is set up)
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            root.addHandler(h)
            root.setLevel(logging.DEBUG)
    return on


BYO_DEBUG = configure_byo_debug()

_PLACEHOLDER_RE = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)\]")


def find_placeholders(sql: str) -> list[str]:
    """Distinct placeholder names in template order (e.g. ['DELTA'] / ['NATION1','NATION2'])."""
    seen: dict[str, None] = {}
    for m in _PLACEHOLDER_RE.finditer(sql):
        seen.setdefault(m.group(1), None)
    return list(seen)


def render_value(v) -> str:
    """Render a sampled value as the bare literal that goes where ``[PH]`` sat (the
    template already supplies surrounding quotes / ``date`` / ``interval`` syntax)."""
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (decimal.Decimal, float)):
        return format(v, "f") if isinstance(v, decimal.Decimal) else repr(v)
    return str(v)


def coerce_for_engine(v) -> str:
    """Render an inferred value as a string.

    The framework passes all query placeholder values as strings (the built-in TPC-H
    generator returns DELTA='68', QUANTITY='25'): format_args_element quotes them and the
    generated arg-parser reads them with std::quoted into std::string fields, then the
    engine converts. Passing native int/float here produced an args line of `"2520"` that
    the parser, expecting an unquoted `2520`, rejected with "Q1: failed to parse DELTA".
    render_value is unaffected, so the engine arg and the DuckDB query stay consistent."""
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return format(v, "f")  # exact decimal text, e.g. "0.09"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def substitute(template: str, assignment: dict[str, object]) -> str:
    sql = template
    for ph, val in assignment.items():
        sql = sql.replace(f"[{ph}]", render_value(val))
    return sql


@dataclass
class _PHContext:
    column: str | None  # the column the placeholder co-occurs with in its predicate
    is_interval: bool   # placeholder is an INTERVAL quantity (an integer offset)


def _placeholder_context(template: str, ph: str) -> _PHContext:
    """Use the SQL structure (sqlglot) to find the column a placeholder is constrained
    against, and whether it is an interval offset. Tolerant: on any parse miss returns an
    empty context and the caller falls back to a typed default + selectivity validation."""
    # replace every placeholder with a parseable sentinel literal, remember ph's sentinel
    sentinel = f"PH_{ph}_X"
    probe = _PLACEHOLDER_RE.sub(lambda m: f"PH_{m.group(1)}_X", template)
    try:
        tree = sqlglot.parse_one(probe, read="duckdb")
    except Exception:
        return _PHContext(column=None, is_interval=False)

    node = None
    for n in tree.find_all((exp.Literal, exp.Column)):
        if n.name == sentinel:
            node = n
            break
    if node is None:
        return _PHContext(column=None, is_interval=False)

    is_interval = node.find_ancestor(exp.Interval) is not None
    pred = node.find_ancestor(
        exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.In, exp.Like, exp.Between
    )
    column = None
    if pred is not None:
        for c in pred.find_all(exp.Column):
            column = c.name  # bare column name; the value pool is sampled by this name
            break
    return _PHContext(column=column, is_interval=is_interval)


def _table_of_column(schema: dict[str, dict[str, str]], column: str) -> str | None:
    for table, cols in schema.items():
        if column in cols:
            return table
    return None


def _candidate_pool(
    ph: str,
    ctx: _PHContext,
    schema: dict[str, dict[str, str]],
    con,
    pool_size: int = 40,
) -> list[object]:
    """Candidate values for one placeholder, sampled from the data. This only needs to be
    a reasonable superset; the joint validation downstream drops values that don't yield a
    result, so it does not have to be exact."""
    # INTERVAL offset: an integer sweep sized to the related date column's span.
    if ctx.is_interval and ctx.column is not None:
        table = _table_of_column(schema, ctx.column)
        if table is not None:
            span = con.execute(
                f"SELECT date_diff('day', min({ctx.column}), max({ctx.column})) "
                f"FROM {table}"
            ).fetchone()[0]
            span = int(span or 365)
            step = max(1, span // pool_size)
            return list(range(0, span + 1, step))
        return list(range(0, 366, 10))

    if ctx.column is None:
        return []

    table = _table_of_column(schema, ctx.column)
    if table is None:
        return []
    ctype = schema[table][ctx.column].upper()

    if any(t in ctype for t in ("CHAR", "STRING", "TEXT")):
        rows = con.execute(
            f"SELECT {ctx.column} FROM {table} WHERE {ctx.column} IS NOT NULL "
            f"GROUP BY {ctx.column} ORDER BY count(*) DESC LIMIT {pool_size}"
        ).fetchall()
        return [r[0] for r in rows]

    # numeric / date / decimal: actual quantile values so range predicates are meaningful
    qs = [round(i / (pool_size + 1), 4) for i in range(1, pool_size + 1)]
    rows = con.execute(
        f"SELECT DISTINCT unnest(approx_quantile({ctx.column}, {qs})) AS v "
        f"FROM {table} WHERE {ctx.column} IS NOT NULL ORDER BY v"
    ).fetchall()
    return [r[0] for r in rows]


def _query_is_valid(template: str, assignment: dict, con) -> bool:
    """Run the query with these values in DuckDB; valid if it executes and returns at
    least one row."""
    sql = substitute(template, assignment)
    try:
        return con.execute(f"SELECT count(*) FROM ({sql.rstrip().rstrip(';')}) _t").fetchone()[0] > 0
    except Exception as e:
        logger.debug("candidate rejected (%s): %s", e, assignment)
        return False


def infer_valid_assignments(
    template: str,
    con,
    schema: dict[str, dict[str, str]],
    n: int = DEFAULT_NUM_INSTANTIATIONS,
    seed: int = 42,
    max_tries: int = 400,
) -> list[dict]:
    """Return up to ``n`` placeholder assignments that produce a non-empty query.

    Candidate values are sampled per placeholder from the data; assignments are sampled
    jointly and checked by running the query, which covers correlated and repeated
    placeholders. Raises if no valid assignment is found.
    """
    phs = find_placeholders(template)
    if not phs:
        return [{}]

    pools = {}
    for ph in phs:
        ctx = _placeholder_context(template, ph)
        pool = _candidate_pool(ph, ctx, schema, con)
        pools[ph] = pool
        logger.debug(
            "param-infer [%s]: bound to column=%s interval=%s; pool size=%d sample=%s",
            ph, ctx.column, ctx.is_interval, len(pool), pool[:5],
        )
    empty = [ph for ph, p in pools.items() if not p]
    if empty:
        raise ValueError(
            f"Could not derive candidate values for placeholder(s) {empty} in template; "
            f"could not bind them to a data column. Rewrite to compare a column directly, "
            f"or supply explicit values."
        )

    rnd = random.Random(seed)
    out: list[dict] = []
    seen: set[tuple] = set()
    rejected = 0
    for _ in range(max_tries):
        assign = {ph: rnd.choice(pools[ph]) for ph in phs}
        key = tuple(sorted((k, str(v)) for k, v in assign.items()))
        if key in seen:
            continue
        seen.add(key)
        if _query_is_valid(template, assign, con):
            coerced = {k: coerce_for_engine(v) for k, v in assign.items()}
            out.append(coerced)
            logger.debug("param-infer: accepted #%d %s", len(out), coerced)
            if len(out) >= n:
                break
        else:
            rejected += 1

    if not out:
        raise ValueError(
            f"No valid instantiation found for template after {max_tries} tries "
            f"(placeholders {phs}). The query may be unsatisfiable over this data."
        )
    logger.info(
        "Inferred %d valid instantiations for placeholders %s (%d candidates rejected as empty)",
        len(out), phs, rejected,
    )
    return out


def build_schema(con, tables: list[str]) -> dict[str, dict[str, str]]:
    """{table: {column: type}} via DuckDB DESCRIBE."""
    schema: dict[str, dict[str, str]] = {}
    for t in tables:
        rows = con.execute(f"DESCRIBE SELECT * FROM {t}").fetchall()
        schema[t] = {r[0]: r[1] for r in rows}
    return schema
