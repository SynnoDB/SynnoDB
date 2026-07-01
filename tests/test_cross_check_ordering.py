"""Tie-aware ordered cross-check.

A query with ``ORDER BY`` constrains the order only by its key columns; rows that tie on the keys
may appear in any order. A correct engine may legitimately break those ties differently from
DuckDB, so a strict position-by-position comparison false-rejects it and quarantines a correct
engine. These tests pin the tie-aware comparison: ties permute freely, but a genuine ordering bug
(wrong key sequence) and any data difference are still caught.

Run: .venv/bin/python -m pytest tests/test_cross_check_ordering.py -q
"""

from __future__ import annotations

import pyarrow as pa

import synnodb
from synnodb.router.adapt import results_equal
from synnodb.router.normalize import order_by_key_indices
from synnodb.router import (
    LocalCallableEngine,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)


# ---- order_by_key_indices (unit) -------------------------------------------
def test_order_keys_resolves_name_alias_and_ordinal():
    assert order_by_key_indices("SELECT a, b FROM t ORDER BY a", ["a", "b"]) == [0]
    assert order_by_key_indices("SELECT a, b FROM t ORDER BY b, a", ["a", "b"]) == [
        1,
        0,
    ]
    assert order_by_key_indices("SELECT a AS k, b FROM t ORDER BY k", ["k", "b"]) == [0]
    assert order_by_key_indices("SELECT a, b FROM t ORDER BY 2", ["a", "b"]) == [1]


def test_order_keys_returns_none_when_unresolvable():
    assert order_by_key_indices("SELECT a, b FROM t", ["a", "b"]) is None  # no ORDER BY
    assert (
        order_by_key_indices("SELECT a FROM t ORDER BY a + 1", ["a"]) is None
    )  # expression
    assert (
        order_by_key_indices("SELECT a FROM t ORDER BY t.a", ["a"]) is None
    )  # qualified
    assert (
        order_by_key_indices("SELECT a FROM t ORDER BY hidden", ["a"]) is None
    )  # not projected
    assert (
        order_by_key_indices("SELECT a, a FROM t ORDER BY a", ["a", "a"]) is None
    )  # ambiguous


# ---- results_equal with order_keys (unit) ----------------------------------
def _t(a, b):
    return pa.table({"a": pa.array(a, pa.int64()), "b": pa.array(b)})


def test_tie_permutation_is_equal_with_order_keys():
    duck = _t([1, 1, 2], ["x", "y", "z"])
    eng = _t([1, 1, 2], ["y", "x", "z"])  # same data, tie on a=1 broken the other way
    assert results_equal(eng, duck, ordered=True, order_keys=[0]) is True
    # Strict (no keys) would reject the very same correct result:
    assert results_equal(eng, duck, ordered=True, order_keys=None) is False


def test_wrong_key_order_is_caught_even_with_order_keys():
    duck = _t([1, 2, 3], ["x", "y", "z"])
    eng = _t([2, 1, 3], ["y", "x", "z"])  # key column genuinely mis-ordered
    assert results_equal(eng, duck, ordered=True, order_keys=[0]) is False


def test_data_difference_is_caught_with_order_keys():
    duck = _t([1, 1, 2], ["x", "y", "z"])
    eng = _t([1, 1, 2], ["x", "q", "z"])  # keys line up, but a non-key value differs
    assert results_equal(eng, duck, ordered=True, order_keys=[0]) is False


# ---- end to end through the router -----------------------------------------
def _con():
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t2(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t2 VALUES (1,'x'),(1,'y'),(2,'z')")
    return con


TC = "SELECT a, b FROM t2 ORDER BY a"


def test_tie_permuting_engine_routes_and_is_not_quarantined():
    """The reproduction: a correct engine that breaks an ORDER BY tie differently from DuckDB must
    keep routing, not be quarantined on its first cross-check."""
    con = _con()

    def tie_engine(ph):
        return pa.table(
            {"a": pa.array([1, 1, 2], pa.int32()), "b": pa.array(["y", "x", "z"])}
        )

    register_engine(
        con,
        template_sql=TC,
        engine=LocalCallableEngine("synno-t", {"1": tie_engine}),
        placeholders=[],
    )
    try:
        rows = con.execute(TC).fetchall()
        assert sorted(rows) == [
            (1, "x"),
            (1, "y"),
            (2, "z"),
        ]  # correct multiset, validly ordered
        assert con._last["served_by"] == "engine"  # the engine result was trusted
        assert con.why(TC)["decision"] == "would-route"  # NOT quarantined
    finally:
        con.close()


def test_genuinely_misordered_engine_is_quarantined():
    """Soundness: the tie-aware comparison must still catch a real ordering bug - an engine that
    returns the right rows but violates ORDER BY a is quarantined."""
    con = _con()

    def bad_order(ph):
        # Right multiset, but a=2 placed before a=1 - a genuine ORDER BY violation.
        return pa.table(
            {"a": pa.array([2, 1, 1], pa.int32()), "b": pa.array(["z", "x", "y"])}
        )

    register_engine(
        con,
        template_sql=TC,
        engine=LocalCallableEngine("synno-bad", {"1": bad_order}),
        placeholders=[],
    )
    try:
        rows = con.execute(TC).fetchall()
        assert rows == [
            (1, "x"),
            (1, "y"),
            (2, "z"),
        ]  # served DuckDB's correctly ordered result
        assert con._last["served_by"] == "duckdb"
        assert con.why(TC)["decision"] == "would-fall-back"  # quarantined
    finally:
        con.close()
