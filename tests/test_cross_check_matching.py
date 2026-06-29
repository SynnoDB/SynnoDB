"""The unordered cross-check must not raise a false mismatch on tolerance-equal float rows.

The old comparison stringified each row, sorted both sides, and compared positionally. When a
float on one side rounds across a decimal boundary relative to its match on the other (e.g. 9.9999999
vs 10.0000000), the two sides sort into different positions and the positional compare reports a
mismatch even though the multisets are tolerance-equal - an engine that is actually correct gets
quarantined. ``results_equal`` now groups by the exact columns and matches the genuine-float
columns one-to-one within tolerance (maximum bipartite matching), so this no longer happens, while
real differences are still caught.
"""
from __future__ import annotations

import pyarrow as pa

from synnodb.router.adapt import results_equal


def _t(rows):
    return pa.table({"k": pa.array([r[0] for r in rows]), "v": pa.array([r[1] for r in rows], pa.float64())})


def test_tolerance_equal_rows_match_despite_sort_misalignment():
    # 5.0~5.0000001 and 9.9999999~10.0000000 (both within 1e-6), but str-sorting puts "10.0"
    # before "5.0000001", misaligning the two sides under the old positional compare.
    reference = _t([("a", 5.0), ("a", 9.9999999)])
    bespoke = _t([("a", 5.0000001), ("a", 10.0000000)])
    assert results_equal(bespoke, reference, ordered=False) is True


def test_genuine_float_difference_is_caught():
    reference = _t([("a", 5.0), ("a", 10.0)])
    bespoke = _t([("a", 5.0), ("a", 12.0)])  # 10 vs 12 is a real divergence
    assert results_equal(bespoke, reference, ordered=False) is False


def test_grouping_keeps_floats_within_their_exact_key():
    # Two groups; a float that would match the *other* group's value must not be accepted.
    reference = _t([("a", 1.0), ("b", 2.0)])
    swapped = _t([("a", 2.0), ("b", 1.0)])  # same floats, wrong groups
    assert results_equal(swapped, reference, ordered=False) is False


def test_duplicate_rows_are_multiset_matched():
    reference = _t([("a", 1.0), ("a", 1.0), ("a", 2.0)])
    same = _t([("a", 2.0), ("a", 1.0), ("a", 1.0)])
    fewer = _t([("a", 1.0), ("a", 2.0), ("a", 2.0)])  # one 1.0 replaced by a 2.0
    assert results_equal(same, reference, ordered=False) is True
    assert results_equal(fewer, reference, ordered=False) is False


def test_no_float_columns_is_pure_multiset():
    a = pa.table({"k": pa.array(["x", "y", "y"]), "n": pa.array([1, 2, 2], pa.int64())})
    b = pa.table({"k": pa.array(["y", "x", "y"]), "n": pa.array([2, 1, 2], pa.int64())})
    c = pa.table({"k": pa.array(["y", "x", "y"]), "n": pa.array([2, 1, 3], pa.int64())})
    assert results_equal(b, a, ordered=False) is True
    assert results_equal(c, a, ordered=False) is False


def test_large_all_float_result_is_bounded_and_correct():
    """An all-DOUBLE result has no exact grouping key, so every row lands in ONE group. The matcher
    must stay fast and recursion-free (the old O(n^3)+recursive version blew up and the crash was
    swallowed into an unverified serve) while staying correct."""
    import time

    n = 5000
    vals = [float(i) for i in range(n)]
    a = pa.table({"x": pa.array(vals, pa.float64())})
    b_same = pa.table({"x": pa.array([v + 1e-9 for v in reversed(vals)], pa.float64())})  # same multiset
    b_diff = pa.table({"x": pa.array(vals[:-1] + [float(n) + 10.0], pa.float64())})        # one row off

    t = time.perf_counter()
    assert results_equal(b_same, a, ordered=False) is True
    assert results_equal(b_diff, a, ordered=False) is False
    assert time.perf_counter() - t < 2.0, "matcher is not bounded (O(n^3) regression)"


def test_dense_group_does_not_recursionerror():
    """Thousands of mutually tolerance-compatible rows in one group must not RecursionError (the
    old recursive matcher crashed near ~998 rows, and the crash was swallowed -> unverified serve)."""
    n = 3000
    a = pa.table({"x": pa.array([0.0] * n, pa.float64())})
    b = pa.table({"x": pa.array([1e-9] * n, pa.float64())})  # all within tol of 0.0
    assert results_equal(b, a, ordered=False) is True
