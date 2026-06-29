"""When a routed engine disagrees with DuckDB, the divergence must be VERBOSE to the operator -
not a silent quarantine. The router builds an ``EngineDivergedError`` naming the engine, the
query, and the offending rows/cells, logs it at WARNING, records it on the trace, quarantines the
template, and serves the trusted DuckDB result.
"""
from __future__ import annotations

import logging

import pyarrow as pa

import synnodb
from synnodb.router import (
    LocalCallableEngine,
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)

TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= 2"


def _wrong_engine(ph):
    return pa.table({"c": pa.array([999], pa.int64())})  # always wrong (DuckDB returns 4)


def _con():
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    register_engine(con, template_sql=TEMPLATE, engine=LocalCallableEngine("synno-x", {"1": _wrong_engine}),
                    placeholders=[PlaceholderSpec("p0", "INTEGER")])
    return con


def test_divergence_is_logged_verbosely_and_falls_back(caplog):
    con = _con()
    try:
        with caplog.at_level(logging.WARNING):
            rows = con.execute(TEMPLATE).fetchall()
        # Served the correct DuckDB answer (4), not the engine's wrong 999.
        assert rows == [(4,)]
        # A verbose divergence was logged, naming the engine, the disagreement (999 vs 4).
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "DIVERGED" in msgs and "synno-x" in msgs
        assert "999" in msgs and "4" in msgs
        # The template is quarantined, so it no longer routes.
        assert con.why(TEMPLATE)["decision"] == "would-fall-back"
    finally:
        con.close()


def _crashing(ph):
    raise RuntimeError("boom in run_q1")


def test_engine_crash_is_loud_and_quarantine_reported_by_why(caplog):
    """A registered engine that crashes must not be invisible: the first fault and the quarantine
    are logged at WARNING, and why() reports 'quarantined' rather than the misleading 'no template
    match'. The user still gets the correct DuckDB answer."""
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0, breaker_threshold=2),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    register_engine(con, template_sql=TEMPLATE, engine=LocalCallableEngine("synno-x", {"1": _crashing}),
                    placeholders=[PlaceholderSpec("p0", "INTEGER")])
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(2):  # breaker_threshold -> quarantine on the 2nd
                assert con.execute(TEMPLATE).fetchall() == [(4,)]  # correct, via DuckDB
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "bespoke engine error" in msgs and "synno-x" in msgs   # first fault is loud
        assert "quarantined" in msgs                                   # breaker trip is loud
        assert "quarantined" in con.why(TEMPLATE)["reason"]            # why() is honest
    finally:
        con.close()


def test_cross_check_comparison_error_serves_verified_duckdb(caplog, monkeypatch):
    """FAIL-CLOSED: if the comparison itself raises, the trusted DuckDB reference is already in
    hand, so we serve THAT - never the unverified engine result. The old behavior served the
    engine's wrong answer; serving a known-wrong result while holding the correct one is the exact
    fail-open we reject. The skipped check is logged at WARNING, counted, and charged as an engine
    failure (so a persistently un-comparable engine trips the breaker)."""
    import synnodb.router.router as rr

    def _boom(*a, **k):
        raise RuntimeError("comparator boom")

    con = _con()  # the wrong engine (returns 999); DuckDB truth is 4
    monkeypatch.setattr(rr, "results_equal", _boom)
    try:
        with caplog.at_level(logging.WARNING):
            rows = con.execute(TEMPLATE).fetchall()
        assert rows == [(4,)]  # the VERIFIED DuckDB result, not the engine's unverified 999
        assert con._last["served_by"] == "duckdb"  # reported honestly, not a bogus engine serve
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "cross-check comparison failed" in msgs                 # visible, not silent
        assert con.router_stats()["session"]["cross_check_error"] >= 1
    finally:
        con.close()


def test_cross_check_reference_error_falls_back_to_duckdb(caplog, monkeypatch):
    """FAIL-CLOSED: if the DuckDB reference execution fails, we cannot verify, so we fall back and
    let the caller run DuckDB itself (surfacing any genuine error) rather than serve the engine
    result unverified. The skipped check is logged at WARNING and counted."""
    import synnodb.router.router as rr

    class _BoomBackend:
        def __init__(self, *a, **k):
            pass

        def execute_arrow(self, *a, **k):
            raise RuntimeError("reference boom")

    con = _con()  # the wrong engine (returns 999); DuckDB truth is 4
    monkeypatch.setattr(rr, "DuckDBBackend", _BoomBackend)
    try:
        with caplog.at_level(logging.WARNING):
            rows = con.execute(TEMPLATE).fetchall()
        assert rows == [(4,)]  # the caller's DuckDB fallback produced the correct answer
        assert con._last["served_by"] == "duckdb"
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "cross-check reference execution failed" in msgs
        assert con.router_stats()["session"]["cross_check_error"] >= 1
        assert con.router_stats()["session"]["fell_back"] >= 1
    finally:
        con.close()
