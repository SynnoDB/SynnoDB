"""Routing observability: ``why()`` dry-runs the decision, and ``router_stats`` tallies it."""
from __future__ import annotations

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

SNAPSHOT = [1, 2, 3, 4, 5]
TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= 2"


def _engine(ph):
    x = int(ph["p0"])
    return pa.table({"c": pa.array([sum(1 for a in SNAPSHOT if a >= x)], pa.int64())})


def _con():
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    register_engine(con, template_sql=TEMPLATE, engine=LocalCallableEngine("e", {"1": _engine}),
                    placeholders=[PlaceholderSpec("p0", "INTEGER")])
    return con


def test_why_would_route():
    con = _con()
    w = con.why("SELECT count(*) AS c FROM t WHERE a >= 4")
    assert w["decision"] == "would-route"
    assert w["template"] == "e::1"
    assert w["placeholders"] == {"p0": 4}
    assert all(g["ok"] for g in w["guards"])
    # why() does not execute: the engine never ran.
    assert con.router_stats()["session"]["routed"] == 0


def test_why_no_template_match():
    con = _con()
    w = con.why("SELECT a FROM t ORDER BY a")  # valid, but a different shape
    assert w["decision"] == "would-fall-back"
    assert w["reason"] == "no template match"


def test_why_constant_mismatch_explains_binding_failure():
    con = _con()
    w = con.why("SELECT count(*) AS c FROM t WHERE a >= 2 AND b = 'nope'")
    assert w["decision"] == "would-fall-back"  # structural key differs / binding fails


def test_why_blocked_for_write():
    con = _con()
    w = con.why("INSERT INTO t VALUES (9, 'q')")
    assert w["decision"] == "blocked"
    assert "writes" in w["reason"]


def test_session_counters_track_routed_and_fallback():
    con = _con()
    con.execute("SELECT count(*) AS c FROM t WHERE a >= 4")      # routes
    con.execute("SELECT a FROM t ORDER BY a")                    # valid, falls back (no match)
    s = con.router_stats()["session"]
    assert s["routed"] == 1
    assert s["fell_back"] == 1
    assert s["cross_checked"] == 1
    assert s["fallback_reasons"].get("no template match") == 1
