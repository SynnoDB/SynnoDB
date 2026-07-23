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


def _match_float_vecs_subset(a: List[Row], b: List[Row], tol: float) -> bool:
    """Whether every float-tuple in *a* matches a DISTINCT tolerance-equal tuple in *b*. The
    containment counterpart of :func:`_match_float_vecs`, with the same size cap and the same
    conservative fallback: a false mismatch only over-rejects."""
    if not a:
        return True
    if len(a) > len(b):
        return False

    if len(a[0]) <= 1 or len(b) > _EXACT_MATCH_CAP:
        sb = sorted(b, key=_vec_sort_key)
        j = 0
        for va in sorted(a, key=_vec_sort_key):
            while j < len(sb) and not all(
                _float_close(x, y, tol) for x, y in zip(va, sb[j])
            ):
                j += 1
            if j == len(sb):
                return False
            j += 1
        return True

    compat = [
        [all(_float_close(x, y, tol) for x, y in zip(va, vb)) for vb in b] for va in a
    ]
    match = [-1] * len(b)  # match[j] = the a-index assigned to b[j], or -1

    def augment(i: int, seen: List[bool]) -> bool:
        for j in range(len(b)):
            if compat[i][j] and not seen[j]:
                seen[j] = True
                if match[j] == -1 or augment(match[j], seen):
                    match[j] = i
                    return True
        return False

    return all(augment(i, [False] * len(b)) for i in range(len(a)))


def _multiset_contained(rows_a: List[Row], rows_b: List[Row], tol: float) -> bool:
    """Whether every row of *a* is a row of *b*, counting multiplicity - the same exact-columns
    grouping and float tolerance as :func:`_multiset_equal`, but containment instead of equality.
    A row repeated more often in *a* than *b* holds it fails, so an engine cannot fill its result
    by duplicating one legitimate row."""
    from collections import defaultdict

    if not rows_a:
        return True
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
    for k, va in groups_a.items():
        vb = groups_b.get(k)
        if vb is None or len(va) > len(vb):
            return False
        if float_idx and not _match_float_vecs_subset(va, vb, tol):
            return False
    return True


# How far the reference query may be widened while hunting for the end of the tie group its LIMIT
# cut through, as a multiple of the rows the original window covers. A group that outruns this is
# pathological (the whole window sits inside one tied block); the caller then compares strictly
# rather than re-running an ever-larger query.
_MAX_WIDEN_FACTOR = 1024


def candidate_superset(
    reference: pa.Table,
    *,
    order_keys: Sequence[int],
    row_limit: Optional[int],
    row_offset: int,
    fetch_widened: Any,
    float_tol: float = 1e-6,
) -> Optional[List[Row]]:
    """Every row the query's window could legitimately have contained - the pool a correct engine
    is free to answer from.

    ``ORDER BY k LIMIT n`` fixes how many rows tied on ``k`` survive the cut but never which ones,
    so DuckDB's pick at the cut is arbitrary and not even stable across runs of the identical
    query. Rather than exempt those rows from checking, we re-run the query wide enough to hold the
    whole tie group it cut through and let the caller verify the engine's rows are members of it.

    *fetch_widened* runs the query over the first N rows of the same ranking (top-level LIMIT set
    to N, any OFFSET dropped - see ``normalize.widened_query``) and returns its Arrow result, or
    ``None`` if it cannot. It is called with an exponentially growing N until the superset is
    provably complete: either the result ends on a different key than the window did, so the
    window's last tie group is closed inside it, or it came back short of N and is therefore the
    entire ranking.

    Returns ``None`` when the result was not truncated (the reference is already the complete
    answer) or the group could not be closed within :data:`_MAX_WIDEN_FACTOR`; the caller then
    compares strictly, which can only over-reject.
    """
    rows = _rows(reference)
    if not rows or not order_keys or row_limit is None or len(rows) != row_limit:
        return None
    last_key = tuple(rows[-1][i] for i in order_keys)

    window = row_offset + row_limit  # rows of the ranking the original query spans
    limit = window
    while limit <= window * _MAX_WIDEN_FACTOR:
        limit *= 2
        widened = fetch_widened(limit)
        if widened is None:
            return None
        wrows = _rows(widened)
        if not wrows:
            return None
        if len(wrows) < limit or not _row_equal(
            tuple(wrows[-1][i] for i in order_keys), last_key, float_tol
        ):
            # Short of the limit = the whole ranking; a different trailing key = the window's last
            # tie group closed inside this result. Either way every candidate row is in hand.
            return wrows
    return None


def results_equal(
    bespoke: pa.Table,
    reference: pa.Table,
    *,
    ordered: bool,
    order_keys: Optional[Sequence[int]] = None,
    float_tol: float = 1e-6,
    row_limit: Optional[int] = None,
    candidates: Optional[Sequence[Row]] = None,
) -> bool:
    """Whether two result tables are equal. Exact columns (Decimal/int/date/string/bool) must match
    exactly and a NULL is distinct from any value; only genuine float columns compare with
    tolerance. Ordered when the query has a top-level ORDER BY, otherwise set/multiset semantics.

    ``order_keys`` (the output-column indices of the ORDER BY keys) enables a tie-aware ordered
    comparison: the key columns must match positionally - the actual ordering contract - while the
    full rows must match as a multiset, so a correct engine that breaks a tie differently from
    DuckDB is not rejected. Without ``order_keys`` an ordered comparison is strict positional,
    which is conservative (it can only over-reject).

    ``candidates`` (with the query's ``row_limit``) handles a result a top-level LIMIT truncated,
    where matching DuckDB row for row is not merely strict but wrong - it demands the engine
    reproduce an arbitrary pick from the tie group at the cut. Given the superset of rows the
    window could have held (see :func:`candidate_superset`), the engine's rows must instead be
    MEMBERS of that pool: the key sequence pins what each row's rank must be worth, and membership
    pins each row to a genuine row of the ranking. Together those accept every correct answer and
    only correct answers - no row goes unchecked."""
    if bespoke.num_columns != reference.num_columns:
        return False
    if bespoke.num_rows != reference.num_rows:
        return False
    rows_a, rows_b = _rows(bespoke), _rows(reference)
    if not ordered:
        return _multiset_equal(rows_a, rows_b, float_tol)
    if order_keys:
        # The ordering contract is exactly the key sequence; tied rows may permute. Verify the key
        # columns position-by-position, then the rows themselves.
        for a, b in zip(rows_a, rows_b):
            ka = tuple(a[i] for i in order_keys)
            kb = tuple(b[i] for i in order_keys)
            if not _row_equal(ka, kb, float_tol):
                return False
        if (
            candidates is not None
            and row_limit is not None
            and len(rows_b) == row_limit
        ):
            # Truncated: any member of the ranking carrying the key its position calls for is a
            # legitimate answer, so check membership rather than identity. Where a key value has
            # exactly as many rows in the ranking as the window shows, membership IS identity -
            # the freedom appears only where the query genuinely leaves the choice open.
            return _multiset_contained(rows_a, list(candidates), float_tol)
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
    row_limit: Optional[int] = None,
    candidates: Optional[Sequence[Row]] = None,
) -> Tuple[List[Tuple[int, str, Any, Any]], int]:
    """Where *bespoke* disagrees with *reference*: up to *limit* ``(row, column, bespoke, duckdb)``
    diffs plus the total count, for building a verbose ``EngineDivergedError`` when the cross-check
    fails. A structural difference (column names or row count) is one diff with row ``-1``; an
    ordered mismatch yields cell diffs; an unordered (multiset) mismatch yields the rows present on
    only one side. With ``candidates`` (a truncated result, see :func:`results_equal`) the rows
    reported are the engine's own rows that are not in the ranking at all - the actual complaint,
    since which member of a tie group it picked is not one. This describes a failure already found
    by :func:`results_equal`; it does not re-decide equality, so its float bucketing can be
    approximate."""
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
        if (
            candidates is not None
            and row_limit is not None
            and len(rows_r) == row_limit
        ):
            # Keys agree and the result was truncated, so the failure is rows that are not in the
            # ranking. Report those, not a row-for-row diff against DuckDB's arbitrary pick.
            # Consume as we match so a row the engine returned twice, but the ranking holds once,
            # is reported on its second appearance rather than silently passing.
            remaining = list(candidates)
            strays = []
            for r in rows_b:
                for j, cand in enumerate(remaining):
                    if _row_equal(r, cand, float_tol):
                        remaining.pop(j)
                        break
                else:
                    strays.append(r)
            return [
                (-1, "<row not in the ranking>", r, None) for r in strays[:limit]
            ], len(strays)
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
