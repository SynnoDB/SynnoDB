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
from typing import Any, List, Optional, Sequence, Tuple

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
    # Exact types (Decimal / int / date / string) must match DuckDB exactly - the engine's
    # result is rounded to the column's scale so a correct answer reproduces DuckDB's value
    # bit for bit, and a discrepancy is a real mismatch that quarantines the engine. Only
    # genuine floating-point columns (DOUBLE) compare with tolerance, since float aggregates
    # are not bit-reproducible across different summation orders.
    if isinstance(x, float) or isinstance(y, float):
        try:
            return math.isclose(float(x), float(y), rel_tol=tol, abs_tol=tol)
        except (TypeError, ValueError):
            return x == y
    return x == y


def _row_equal(r1: Row, r2: Row, tol: float) -> bool:
    return len(r1) == len(r2) and all(_cell_equal(a, b, tol) for a, b in zip(r1, r2))


def _float_close(x: Any, y: Any, tol: float) -> bool:
    if x is None or y is None:
        return x is None and y is None
    try:
        return math.isclose(float(x), float(y), rel_tol=tol, abs_tol=tol)
    except (TypeError, ValueError):
        return x == y


def _float_columns(rows: List[Row]) -> set:
    """Indices of columns holding a Python float in any row (genuine DOUBLE columns). These compare
    with tolerance; every other column is exact and usable as a grouping key."""
    floatish: set = set()
    for r in rows:
        for i, v in enumerate(r):
            if isinstance(v, float):
                floatish.add(i)
    return floatish


# Above this group size, exact bipartite matching (O(n^3) time, O(n^2) memory, recursion depth up
# to n) is replaced by sorted positional pairing. An all-DOUBLE result has no exact grouping key,
# so EVERY row lands in one group - exactly the case that would otherwise blow up and, because a
# RecursionError is caught upstream, silently serve the engine result UNVERIFIED. The cap keeps the
# matcher bounded and recursion-free; the cap is well under Python's recursion limit.
_EXACT_MATCH_CAP = 64


def _vec_sort_key(v: tuple):
    # Total order over float tuples with None/NaN handled deterministically (None < NaN < finite).
    return tuple(
        (
            x is None,
            isinstance(x, float) and x != x,
            x if isinstance(x, (int, float)) and x == x else 0.0,
        )
        for x in v
    )


def _match_float_vecs(a: List[Row], b: List[Row], tol: float) -> bool:
    """Whether the float-tuples in *a* match one-to-one with those in *b* within tolerance.

    A single float column (the common case) uses sorted positional pairing, which is provably
    optimal for one dimension. Several float columns use exact maximum bipartite matching so two
    tolerance-equal rows are never rejected by an unlucky ordering - but only up to a size cap; a
    larger group falls back to sorted pairing, which is a sound conservative heuristic (a false
    mismatch only causes a safe fallback to DuckDB, never an unverified serve). This keeps the
    matcher O(n log n) and recursion-free on the pathological all-float result instead of O(n^3)
    plus a swallowed RecursionError."""
    n = len(a)
    if n == 0:
        return True
    ncols = len(a[0])

    if ncols <= 1 or n > _EXACT_MATCH_CAP:
        sa = sorted(a, key=_vec_sort_key)
        sb = sorted(b, key=_vec_sort_key)
        return all(
            all(_float_close(x, y, tol) for x, y in zip(va, vb))
            for va, vb in zip(sa, sb)
        )

    compat = [
        [all(_float_close(x, y, tol) for x, y in zip(a[i], b[j])) for j in range(n)]
        for i in range(n)
    ]
    match = [-1] * n  # match[j] = the a-index assigned to b[j], or -1

    def augment(i: int, seen: List[bool]) -> bool:
        for j in range(n):
            if compat[i][j] and not seen[j]:
                seen[j] = True
                if match[j] == -1 or augment(match[j], seen):
                    match[j] = i
                    return True
        return False

    return all(augment(i, [False] * n) for i in range(n))


def _multiset_equal(rows_a: List[Row], rows_b: List[Row], tol: float) -> bool:
    """Set/multiset equality with float tolerance: group rows by their EXACT columns (which must
    match as a multiset, NULL included), then within each group match the genuine-float columns
    one-to-one within tolerance. Exact on the exact columns, tolerant only where DuckDB and the
    engine legitimately round differently."""
    from collections import defaultdict

    if not rows_a:
        return not rows_b
    float_idx = _float_columns(rows_a) | _float_columns(rows_b)
    ncols = len(rows_a[0])
    exact_idx = [i for i in range(ncols) if i not in float_idx]

    def key(r: Row) -> tuple:
        return tuple(r[i] for i in exact_idx)

    def fvec(r: Row) -> tuple:
        return tuple(r[i] for i in float_idx)

    groups_a: dict = defaultdict(list)
    groups_b: dict = defaultdict(list)
    for r in rows_a:
        groups_a[key(r)].append(fvec(r))
    for r in rows_b:
        groups_b[key(r)].append(fvec(r))
    if groups_a.keys() != groups_b.keys():
        return False
    for k, va in groups_a.items():
        vb = groups_b[k]
        if len(va) != len(vb):
            return False
        if float_idx and not _match_float_vecs(va, vb, tol):
            return False
    return True


def results_equal(
    bespoke: pa.Table,
    reference: pa.Table,
    *,
    ordered: bool,
    order_keys: Optional[Sequence[int]] = None,
    float_tol: float = 1e-6,
) -> bool:
    """Whether two result tables are equal. Exact columns (Decimal/int/date/string/bool) must match
    exactly and a NULL is distinct from any value; only genuine float columns compare with
    tolerance. Ordered when the query has a top-level ORDER BY, otherwise set/multiset semantics.

    ``order_keys`` (the output-column indices of the ORDER BY keys) enables a tie-aware ordered
    comparison: the key columns must match positionally - the actual ordering contract - while the
    full rows must match as a multiset, so a correct engine that breaks a tie differently from
    DuckDB is not rejected. Without ``order_keys`` an ordered comparison is strict positional,
    which is conservative (it can only over-reject)."""
    if bespoke.num_columns != reference.num_columns:
        return False
    if bespoke.num_rows != reference.num_rows:
        return False
    rows_a, rows_b = _rows(bespoke), _rows(reference)
    if not ordered:
        return _multiset_equal(rows_a, rows_b, float_tol)
    if order_keys:
        # The ordering contract is exactly the key sequence; tied rows may permute. Verify the key
        # columns position-by-position, then the full rows as a multiset (the data).
        for a, b in zip(rows_a, rows_b):
            ka = tuple(a[i] for i in order_keys)
            kb = tuple(b[i] for i in order_keys)
            if not _row_equal(ka, kb, float_tol):
                return False
        return _multiset_equal(rows_a, rows_b, float_tol)
    return all(_row_equal(a, b, float_tol) for a, b in zip(rows_a, rows_b))


def results_diff(
    bespoke: pa.Table,
    reference: pa.Table,
    *,
    ordered: bool,
    order_keys: Optional[Sequence[int]] = None,
    float_tol: float = 1e-6,
    limit: int = 8,
) -> Tuple[List[Tuple[int, str, Any, Any]], int]:
    """Where *bespoke* disagrees with *reference*: up to *limit* ``(row, column, bespoke, duckdb)``
    diffs plus the total count, for building a verbose ``EngineDivergedError`` when the cross-check
    fails. A structural difference (column names or row count) is one diff with row ``-1``; an
    ordered mismatch yields cell diffs; an unordered (multiset) mismatch yields the rows present on
    only one side. This describes a failure already found by :func:`results_equal`; it does not
    re-decide equality, so its float bucketing can be approximate."""
    bnames, rnames = list(bespoke.column_names), list(reference.column_names)
    if bnames != rnames:
        return [(-1, "<columns>", bnames, rnames)], 1
    if bespoke.num_rows != reference.num_rows:
        return [(-1, "<row count>", bespoke.num_rows, reference.num_rows)], 1
    rows_b, rows_r = _rows(bespoke), _rows(reference)

    if ordered and order_keys:
        # Tie-aware: report a wrong ORDER BY key sequence as positional key-cell diffs; if the keys
        # all line up, the failure is a data difference among the rows -> describe it as a multiset
        # diff (the same way an unordered mismatch is described below).
        key_diffs: List[Tuple[int, str, Any, Any]] = []
        key_total = 0
        for i, (rb, rr) in enumerate(zip(rows_b, rows_r)):
            for ci in order_keys:
                if not _cell_equal(rb[ci], rr[ci], float_tol):
                    key_total += 1
                    if len(key_diffs) < limit:
                        key_diffs.append((i, bnames[ci], rb[ci], rr[ci]))
        if key_total:
            return key_diffs, key_total
        ordered = False  # keys agree -> fall through to the multiset (data) diff

    if ordered:
        diffs: List[Tuple[int, str, Any, Any]] = []
        total = 0
        for i, (rb, rr) in enumerate(zip(rows_b, rows_r)):
            for col, a, c in zip(bnames, rb, rr):
                if not _cell_equal(a, c, float_tol):
                    total += 1
                    if len(diffs) < limit:
                        diffs.append((i, col, a, c))
        return diffs, total

    # Unordered: multiset difference. Bucket a row by its exact cells plus its floats snapped to
    # the tolerance grid, then report rows present on only one side (approximate but informative).
    from collections import Counter

    def bucket(r: Row) -> tuple:
        return tuple(
            ("f", None if v != v else round(v / float_tol))
            if isinstance(v, float)
            else ("v", v)
            for v in r
        )

    cb, cr = Counter(bucket(r) for r in rows_b), Counter(bucket(r) for r in rows_r)
    only_b, only_r = cb - cr, cr - cb
    total = sum(only_b.values()) + sum(only_r.values())
    rep_b = {bucket(r): r for r in rows_b}
    rep_r = {bucket(r): r for r in rows_r}
    diffs = []
    for key in only_b:
        if len(diffs) >= limit:
            break
        diffs.append((-1, "<engine-only row>", rep_b[key], None))
    for key in only_r:
        if len(diffs) >= limit:
            break
        diffs.append((-1, "<duckdb-only row>", None, rep_r[key]))
    return diffs, total
