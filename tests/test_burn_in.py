"""Burn-in verification.

Sampling alone (``cross_check_rate``) leaves a brand-new engine's earliest results unverified - a
systematically wrong engine could serve many wrong answers before a sampled check happens to hit
one. Burn-in closes that: a template's first ``verify_first_n`` executions are ALWAYS cross-checked,
so a wrong engine is caught and quarantined on its first queries and never serves a wrong result.
``cross_check_rate == 0`` is an explicit opt-out of all verification and disables burn-in too.

Run: .venv/bin/python -m pytest tests/test_burn_in.py -q
"""

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

TEMPLATE = "SELECT count(*) AS c FROM t WHERE a >= 2"


def _con(**policy_kw):
    con = synnodb.connect(
        policy=RouterPolicy(mode=RouterMode.SAMPLED, **policy_kw),
        registry=TemplateRegistry(),
    )
    con.duckdb.execute("CREATE TABLE t(a INTEGER)")
    con.duckdb.execute(
        "INSERT INTO t VALUES (1),(2),(3),(4),(5)"
    )  # DuckDB count(a>=2) = 4
    return con


def _never_sample(con):
    # Make random sampling deterministically never fire, so only burn-in can trigger a check.
    con._router._rng.random = lambda: 1.0  # type: ignore[assignment]


def _wrong(ph):
    return pa.table({"c": pa.array([999], pa.int64())})


def _right(ph):
    return pa.table({"c": pa.array([4], pa.int64())})


def test_burn_in_catches_a_wrong_engine_on_the_first_query():
    """Even with sampling effectively off, a wrong engine is verified on execution #1 (burn-in),
    quarantined, and never serves its wrong answer."""
    con = _con(cross_check_rate=1e-9, verify_first_n=5)
    _never_sample(con)
    register_engine(
        con,
        template_sql=TEMPLATE,
        engine=LocalCallableEngine("synno-x", {"1": _wrong}),
        placeholders=[PlaceholderSpec("p0", "INTEGER")],
    )
    try:
        assert con.execute(TEMPLATE).fetchall() == [
            (4,)
        ]  # correct DuckDB answer, not 999
        assert con._last["served_by"] == "duckdb"
        assert (
            con.why(TEMPLATE)["decision"] == "would-fall-back"
        )  # quarantined after one query
        assert con.router_stats()["session"]["cross_check_mismatch"] >= 1
    finally:
        con.close()


def test_burn_in_checks_exactly_the_first_n_then_samples():
    """With sampling forced off, exactly the first ``verify_first_n`` executions are cross-checked;
    later ones route without a check."""
    con = _con(cross_check_rate=1e-9, verify_first_n=3)
    _never_sample(con)
    register_engine(
        con,
        template_sql=TEMPLATE,
        engine=LocalCallableEngine("synno-x", {"1": _right}),
        placeholders=[PlaceholderSpec("p0", "INTEGER")],
    )
    try:
        for _ in range(10):
            assert con.execute(TEMPLATE).fetchall() == [(4,)]
        sess = con.router_stats()["session"]
        assert sess["routed"] == 10  # every query served by the (correct) engine
        assert sess["cross_checked"] == 3  # only the burn-in window was verified
    finally:
        con.close()


def test_rate_zero_opts_out_of_all_verification_including_burn_in():
    """``cross_check_rate == 0`` is an explicit, total opt-out: no checks at all, so burn-in does
    not run and an unverified engine result is served (the operator's stated choice)."""
    con = _con(cross_check_rate=0.0, verify_first_n=50)
    register_engine(
        con,
        template_sql=TEMPLATE,
        engine=LocalCallableEngine("synno-x", {"1": _wrong}),
        placeholders=[PlaceholderSpec("p0", "INTEGER")],
    )
    try:
        assert con.execute(TEMPLATE).fetchall() == [
            (999,)
        ]  # unverified engine result is served
        assert con.router_stats()["session"]["cross_checked"] == 0
    finally:
        con.close()


def test_verify_first_n_env_override(monkeypatch):
    monkeypatch.setenv("SYNNODB_VERIFY_FIRST_N", "7")
    assert RouterPolicy.from_env().verify_first_n == 7
