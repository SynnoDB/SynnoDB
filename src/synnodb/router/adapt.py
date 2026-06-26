"""Adapt engine Arrow output to a DuckDB-shaped result, and compare results.

* ``to_synno_result`` wraps the engine's ``pyarrow.Table`` as a ``SynnoResult``,
  carrying the binding's canonical DuckDB type strings so ``description`` matches
  DuckDB exactly.
* ``results_equal`` is the cross-check verdict: are the bespoke and DuckDB results
  the same? Set-semantics by default (SQL row order is undefined without
  ``ORDER BY``); ordered when the query has a top-level ``ORDER BY``. Floats compare
  with a tolerance (engines and DuckDB may round differently).
"""
from __future__ import annotations

import math
from typing import Any, List, Sequence, Tuple

import pyarrow as pa

from .registry import ColumnSpec

Row = Tuple[Any, ...]


def to_synno_result(table: pa.Table, output_schema: Sequence[ColumnSpec] = ()) -> Any:
    """Wrap an Arrow table as a ``SynnoResult`` (lazy import avoids an import cycle)."""
    from synnodb.duckdb_compat.result import SynnoResult

    duckdb_types = [c.type for c in output_schema] if output_schema else None
    return SynnoResult(table, duckdb_types=duckdb_types)


def _rows(table: pa.Table) -> List[Row]:
    columns = [col.to_pylist() for col in table.columns]
    return list(zip(*columns)) if columns else []


def _cell_equal(x: Any, y: Any, tol: float) -> bool:
    if x is None or y is None:
        return x is None and y is None
    if isinstance(x, float) or isinstance(y, float):
        try:
            return math.isclose(float(x), float(y), rel_tol=tol, abs_tol=tol)
        except (TypeError, ValueError):
            return x == y
    return x == y


def _row_equal(r1: Row, r2: Row, tol: float) -> bool:
    return len(r1) == len(r2) and all(_cell_equal(a, b, tol) for a, b in zip(r1, r2))


def _sort_key(row: Row):
    # Stable, type-tolerant ordering: (is-null, type-name, str) per cell.
    return tuple((v is None, type(v).__name__, str(v)) for v in row)


def results_equal(
    bespoke: pa.Table,
    reference: pa.Table,
    *,
    ordered: bool,
    float_tol: float = 1e-6,
) -> bool:
    """Whether two result tables are equal (set- or order-semantics, float-tolerant)."""
    if bespoke.num_columns != reference.num_columns:
        return False
    if bespoke.num_rows != reference.num_rows:
        return False
    rows_a, rows_b = _rows(bespoke), _rows(reference)
    if not ordered:
        rows_a = sorted(rows_a, key=_sort_key)
        rows_b = sorted(rows_b, key=_sort_key)
    return all(_row_equal(a, b, float_tol) for a, b in zip(rows_a, rows_b))
