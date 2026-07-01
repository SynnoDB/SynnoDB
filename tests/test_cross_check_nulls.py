"""The cross-check must honour SQL null semantics: a NULL is not a 0 / "" / epoch.

With full nullable support the engine can emit real Arrow NULLs (column_egress validity) read
from real input NULLs (column_ingest validity). The safety net is that ``results_equal`` - the
verdict used by both the runtime cross-check and, in spirit, the generation validator - treats a
NULL as distinct from any concrete value. So an engine that regresses to the old null->0 behaviour
diverges from DuckDB and is caught (quarantined / fallback) rather than silently served.
"""

from __future__ import annotations

import pyarrow as pa

from synnodb.router.adapt import results_equal


def _t(values, typ=pa.int64()):
    return pa.table({"k": pa.array(["a", "b", "c"]), "v": pa.array(values, typ)})


def test_null_is_not_zero():
    duckdb = _t([10, None, 20])
    engine_buggy = _t([10, 0, 20])  # the old null->0 behaviour
    engine_exact = _t([10, None, 20])  # full nullable support
    assert results_equal(engine_buggy, duckdb, ordered=True) is False
    assert results_equal(engine_exact, duckdb, ordered=True) is True


def test_null_is_not_empty_string():
    duckdb = _t([None, "x", "y"], pa.string())
    engine_buggy = _t(["", "x", "y"], pa.string())
    engine_exact = _t([None, "x", "y"], pa.string())
    assert results_equal(engine_buggy, duckdb, ordered=True) is False
    assert results_equal(engine_exact, duckdb, ordered=True) is True


def test_null_matches_null_unordered():
    # Set-semantics path (no ORDER BY): rows with NULLs still match by multiset.
    a = pa.table({"g": pa.array(["x", "y"]), "v": pa.array([None, 5], pa.int64())})
    b = pa.table({"g": pa.array(["y", "x"]), "v": pa.array([5, None], pa.int64())})
    assert results_equal(a, b, ordered=False) is True
