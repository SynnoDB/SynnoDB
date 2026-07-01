"""End-to-end routing: a registered (test-double) engine vs DuckDB, with the full
fallback matrix. Proves the router → guards → execute → cross-check → quarantine
path and that bespoke results are indistinguishable from DuckDB.

The ``LocalCallableEngine`` stands in for the future C++ worker behind the same
``BespokeEngine`` interface, so these tests exercise all the routing logic without
any C++/IPC.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

import synnodb
from synnodb.router import (
    LocalCallableEngine,
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)

# The engine's "ingested" snapshot of table t.
SNAPSHOT = [1, 2, 3, 4, 5]
TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= 2"


def _correct(ph):
    x = int(ph["p0"])
    return pa.table({"c": pa.array([sum(1 for a in SNAPSHOT if a >= x)], pa.int64())})


def _engine(fn, engine_id="e"):
    calls = {"n": 0}

    def wrapped(ph):
        calls["n"] += 1
        return fn(ph)

    return LocalCallableEngine(engine_id, {"1": wrapped}), calls


def _setup(
    fn,
    *,
    mode=RouterMode.SAMPLED,
    cross_check_rate=1.0,
    breaker_threshold=3,
    engine_id="e",
):
    policy = RouterPolicy(
        mode=mode,
        cross_check_rate=cross_check_rate,
        breaker_threshold=breaker_threshold,
    )
    con = synnodb.connect(policy=policy, registry=TemplateRegistry())
    # Data setup goes through the escape hatch: writes are blocked on the routed surface.
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    engine, calls = _engine(fn, engine_id)
    binding = register_engine(
        con,
        template_sql=TEMPLATE,
        engine=engine,
        placeholders=[PlaceholderSpec("p0", "INTEGER")],
    )
    return con, calls, binding


def _duckdb_answer(con, sql, params=None):
    raw = con.duckdb
    return (
        raw.execute(sql, params) if params is not None else raw.execute(sql)
    ).fetchall()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_matched_query_routes_and_equals_duckdb():
    con, calls, _ = _setup(_correct)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    result = con.execute(sql).fetchall()
    assert calls["n"] == 1  # the engine actually ran
    assert result == _duckdb_answer(con, sql)  # and matches DuckDB exactly


def test_routed_result_description_is_duckdb_typed():
    con, _, _ = _setup(_correct)
    con.execute("SELECT count(*) AS c FROM t WHERE a >= 3")
    # description carries DuckDB's canonical type captured at registration.
    assert con.description[0][0] == "c"
    assert (
        "INT" in con.description[0][1].upper()
        or "BIGINT" in con.description[0][1].upper()
    )


def test_parameterized_query_routes():
    con, calls, _ = _setup(_correct)
    sql = "SELECT count(*) AS c FROM t WHERE a >= ?"
    result = con.execute(sql, [3]).fetchall()
    assert calls["n"] == 1
    assert result == _duckdb_answer(con, sql, [3])


def test_trace_reports_cross_check_and_speedup():
    con, _, _ = _setup(_correct, cross_check_rate=1.0)
    dec = con.router.route("SELECT count(*) AS c FROM t WHERE a >= 2", None, con)
    assert dec.routed is True
    assert dec.trace.cross_checked is True
    assert dec.trace.results_match is True
    assert dec.trace.bespoke_ms is not None and dec.trace.duckdb_ms is not None


def test_cross_check_rate_zero_skips_duckdb():
    con, _, _ = _setup(_correct, cross_check_rate=0.0)
    dec = con.router.route("SELECT count(*) AS c FROM t WHERE a >= 2", None, con)
    assert dec.routed is True
    assert dec.trace.cross_checked is False
    assert dec.trace.duckdb_ms is None


# --------------------------------------------------------------------------- #
# Correctness net: cross-check catches a wrong engine
# --------------------------------------------------------------------------- #
def test_cross_check_mismatch_serves_duckdb_and_quarantines():
    con, calls, binding = _setup(
        lambda ph: pa.table({"c": pa.array([999], pa.int64())})
    )
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    result = con.execute(sql).fetchall()
    assert result == _duckdb_answer(con, sql)  # served DuckDB's truth, not 999
    assert binding.template_id in con.router.registry.stats()["quarantined"]
    # subsequent calls now silently fall back (no engine call)
    before = calls["n"]
    con.execute(sql)
    assert calls["n"] == before


# --------------------------------------------------------------------------- #
# Resilience: engine faults fall back, breaker quarantines
# --------------------------------------------------------------------------- #
def _boom(ph):
    raise RuntimeError("engine exploded")


def test_engine_crash_falls_back_to_duckdb():
    con, _, _ = _setup(_boom, cross_check_rate=0.0)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    assert con.execute(sql).fetchall() == _duckdb_answer(con, sql)


def test_breaker_quarantines_after_threshold():
    con, _, binding = _setup(_boom, cross_check_rate=0.0, breaker_threshold=2)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    con.execute(sql)
    assert binding.template_id not in con.router.registry.stats()["quarantined"]
    con.execute(sql)  # second failure trips the breaker
    assert binding.template_id in con.router.registry.stats()["quarantined"]


# --------------------------------------------------------------------------- #
# Guards: dirty tables, schema drift, and mode gating block routing
# --------------------------------------------------------------------------- #
def test_dirty_table_blocks_routing():
    con, calls, _ = _setup(_correct)
    con.router.registry.mark_tables_dirty(["t"])  # a bound table changed
    con.execute("SELECT count(*) AS c FROM t WHERE a >= 4")
    assert calls["n"] == 0  # dirty-table guard forced fallback


def test_schema_drift_blocks_routing():
    con, calls, _ = _setup(_correct)
    con.duckdb.execute(
        "ALTER TABLE t ADD COLUMN c2 INTEGER"
    )  # drift via the escape hatch
    con.execute("SELECT count(*) AS c FROM t WHERE a >= 4")
    assert calls["n"] == 0  # schema-fingerprint mismatch forced fallback


def test_mode_off_never_routes():
    con, calls, _ = _setup(_correct, mode=RouterMode.OFF)
    sql = "SELECT count(*) AS c FROM t WHERE a >= 4"
    assert con.execute(sql).fetchall() == _duckdb_answer(con, sql)
    assert calls["n"] == 0


def test_bespoke_only_raises_on_guard_failure():
    con, _, _ = _setup(_correct, mode=RouterMode.BESPOKE_ONLY)
    con.router.registry.mark_tables_dirty(["t"])  # guard will fail
    with pytest.raises(RuntimeError, match="bespoke_only"):
        con.execute("SELECT count(*) AS c FROM t WHERE a >= 4")
