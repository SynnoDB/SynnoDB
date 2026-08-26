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


def test_order_keys_resolves_projected_expressions():
    # CEB 11a/11b shape: the sort key is the aggregate expression itself, not a name.
    assert order_by_key_indices(
        "SELECT g, r, n, COUNT(*) FROM t GROUP BY g, r, n ORDER BY COUNT(*) DESC",
        ["g", "r", "n", "count_star()"],
    ) == [3]
    # A qualified column resolves when it structurally matches a projection.
    assert order_by_key_indices(
        "SELECT cn.name, COUNT(*) FROM t GROUP BY cn.name ORDER BY cn.name, COUNT(*) DESC",
        ["name", "count_star()"],
    ) == [0, 1]
    # The same expression projected twice stays ambiguous -> strict.
    assert (
        order_by_key_indices(
            "SELECT COUNT(*), COUNT(*) FROM t ORDER BY COUNT(*)",
            ["count_star()", "count_star()_1"],
        )
        is None
    )


def test_order_keys_resolve_table_qualified_key_against_bare_output_name():
    """RTABench Q7 shape: ``ORDER BY oe.order_id`` over an output column named ``order_id``.

    Validation used to match ORDER BY key *strings* against result column names, so a
    table-qualified key could never match its own unqualified output name and the
    cross-check aborted the whole run tool with an AssertionError - a failure no engine
    edit could fix. Resolution is structural, so the qualifier is irrelevant.
    """
    sql = (
        "SELECT DISTINCT ON (oe.order_id) oe.order_id, event_created, event_type "
        "FROM order_events oe JOIN orders ON orders.order_id = oe.order_id "
        "WHERE orders.order_id = 2344 "
        "ORDER BY oe.order_id ASC, event_created DESC"
    )
    assert order_by_key_indices(sql, ["order_id", "event_created", "event_type"]) == [
        0,
        1,
    ]


TAGG = "SELECT b, COUNT(*) FROM t2 GROUP BY b ORDER BY COUNT(*) DESC"


def test_aggregate_order_key_tie_permutation_not_quarantined():
    """A correct engine ordering COUNT(*) ties differently from DuckDB must keep routing.
    Before expression keys resolved, this exact shape (CEB 11a/11b) was strict-compared,
    declared divergent on its first cross-check, and quarantined."""
    con = _con()

    def agg_engine(ph):
        return pa.table(
            {"b": pa.array(["z", "x", "y"]), "n": pa.array([1, 1, 1], pa.int64())}
        )

    register_engine(
        con,
        template_sql=TAGG,
        engine=LocalCallableEngine("synno-agg", {"1": agg_engine}),
        placeholders=[],
    )
    try:
        rows = con.execute(TAGG).fetchall()
        assert sorted(rows) == [("x", 1), ("y", 1), ("z", 1)]
        assert con._last["served_by"] == "engine"
        assert con.why(TAGG)["decision"] == "would-route"
        assert con.router_stats()["session"]["cross_check_mismatch"] == 0
    finally:
        con.close()


# ---- ORDER BY ... LIMIT: the tie AT THE CUT ---------------------------------
TLIM = "SELECT k, COUNT(*) FROM t3 GROUP BY k ORDER BY COUNT(*) DESC LIMIT 2"


def _limit_con():
    con = synnodb.connect(
        ":memory:",
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t3(k VARCHAR)")
    # a:3, b:2, c:2 - LIMIT 2 cuts straight through the b/c tie, so which of them
    # appears at rank 2 is DuckDB's arbitrary pick, not something ORDER BY fixes.
    con.duckdb.execute(
        "INSERT INTO t3 VALUES ('a'),('a'),('a'),('b'),('b'),('c'),('c')"
    )
    return con


def test_engine_breaking_a_limit_tie_differently_is_not_quarantined():
    """RTABench Q30 shape: ``ORDER BY count(*) DESC LIMIT n`` with a tie at the cut.

    Serving quarantined a correct engine here because the router compared the truncated
    window strictly, while the generation-time cross-check already re-ran the query wide
    enough to close the tie group and checked membership. Two customers tied on 1185
    orders; DuckDB kept one and the engine kept the other, and the engine was thrown out
    for it. The router must read ORDER BY ... LIMIT the same way validation does.
    """
    con = _limit_con()

    def cut_tie_the_other_way(ph):
        # Same ranking, the other member of the tie group at rank 2.
        return pa.table(
            {
                "k": pa.array(["a", "c"]),
                "count_star()": pa.array([3, 2], pa.int64()),
            }
        )

    register_engine(
        con,
        template_sql=TLIM,
        engine=LocalCallableEngine("synno-lim", {"1": cut_tie_the_other_way}),
        placeholders=[],
    )
    try:
        rows = con.execute(TLIM).fetchall()
        assert rows[0] == ("a", 3)  # the unambiguous rank-1 row is reproduced
        assert rows[1][1] == 2  # rank 2 is some member of the tie group
        assert con._last["served_by"] == "engine"
        assert con.why(TLIM)["decision"] == "would-route"  # NOT quarantined
        assert con.router_stats()["session"]["cross_check_mismatch"] == 0
    finally:
        con.close()


def test_limit_engine_returning_a_row_outside_the_ranking_is_quarantined():
    """Soundness: membership is checked against the real ranking, not waived."""
    con = _limit_con()

    def wrong_row(ph):
        return pa.table(
            {
                "k": pa.array(["a", "zzz"]),
                "count_star()": pa.array([3, 2], pa.int64()),
            }
        )

    register_engine(
        con,
        template_sql=TLIM,
        engine=LocalCallableEngine("synno-bad", {"1": wrong_row}),
        placeholders=[],
    )
    try:
        con.execute(TLIM).fetchall()
        assert con._last["served_by"] != "engine"
        assert con.router_stats()["session"]["cross_check_mismatch"] == 1
    finally:
        con.close()
