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


def test_named_parameters_fall_back_and_never_quarantine():
    # DuckDB-style named parameters (`$name` + dict) share a structural key with a `?` template,
    # but router binding is positional: a dict cannot be lined up with the specs. The guard must
    # refuse it so DuckDB serves it natively - never bind the dict itself as a value, which would
    # crash the engine repeatedly and quarantine a healthy template.
    con, calls, binding = _setup(_correct, breaker_threshold=3)
    sql = "SELECT count(*) AS c FROM t WHERE a >= $p"
    for _ in range(4):  # more calls than breaker_threshold
        result = con.execute(sql, {"p": 3}).fetchall()
        assert result == _duckdb_answer(con, sql, {"p": 3})
    assert calls["n"] == 0  # the engine never saw the dict
    assert not con.router.registry.is_quarantined(binding.template_id)


def test_trace_reports_cross_check_and_speedup():
    con, _, _ = _setup(_correct, cross_check_rate=1.0)
    dec = con.router.route("SELECT count(*) AS c FROM t WHERE a >= 2", None, con)
    assert dec.routed is True
    assert dec.trace.cross_checked is True
    assert dec.trace.results_match is True
    assert dec.trace.bespoke_ms is not None and dec.trace.duckdb_ms is not None


def test_duckdb_backend_execute_arrow_timed():
    # The shared server-side DuckDB timer returns the Arrow result *and* DuckDB's own profiler
    # latency (the EXPLAIN ANALYZE number) from a single execution, and leaves profiling disabled
    # so the router's plain fallback path pays no profiling overhead.
    import duckdb

    from synnodb.router.backend import DuckDBBackend

    raw = duckdb.connect()
    raw.execute("CREATE TABLE t(a INTEGER)")
    raw.execute("INSERT INTO t SELECT * FROM range(1000)")
    backend = DuckDBBackend(raw)

    table, server_ms = backend.execute_arrow_timed("SELECT count(*) AS c FROM t")
    assert table.column("c").to_pylist() == [1000]  # correct result materialized
    assert isinstance(server_ms, float) and server_ms >= 0.0  # real server-side latency

    # Profiling was disabled again: the 'json' mode we enabled is undone (DuckDB reports the
    # disabled setting as NULL/false), so the router's plain fallback path stays unprofiled.
    assert raw.execute("SELECT current_setting('enable_profiling')").fetchone()[0] not in (
        "json",
        "true",
        True,
    )
    # A second call still works (enable/disable is idempotent) and the parameterized path is honored.
    table2, _ = backend.execute_arrow_timed("SELECT count(*) AS c FROM t WHERE a >= ?", [500])
    assert table2.column("c").to_pylist() == [500]


def test_engine_server_side_time_is_used_for_bespoke_ms():
    # An engine that reports its own server-side execution time (as the C++ ProcessEngine does via
    # the kernel's elapsed_ms) has that number recorded as bespoke_ms, in preference to the router's
    # external wall clock - so the reported speedup reflects pure engine execution, not IPC/Python
    # overhead. A distinctive sentinel proves the reported time is the engine's, not the wall clock.
    SENTINEL_MS = 0.000123

    class TimedEngine(LocalCallableEngine):
        def run(self, query_id, placeholders):
            table, _ = super().run(query_id, placeholders)
            return table, SENTINEL_MS

    policy = RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0)
    con = synnodb.connect(policy=policy, registry=TemplateRegistry())
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'y'),(4,'z'),(5,'z')")
    register_engine(
        con,
        template_sql=TEMPLATE,
        engine=TimedEngine("timed", {"1": _correct}),
        placeholders=[PlaceholderSpec("p0", "INTEGER")],
    )
    dec = con.router.route("SELECT count(*) AS c FROM t WHERE a >= 2", None, con)
    assert dec.routed is True
    assert dec.trace.bespoke_ms == SENTINEL_MS  # the engine's own time, not the wall clock


def test_cross_check_rate_zero_skips_duckdb():
    con, _, _ = _setup(_correct, cross_check_rate=0.0)
    dec = con.router.route("SELECT count(*) AS c FROM t WHERE a >= 2", None, con)
    assert dec.routed is True
    assert dec.trace.cross_checked is False
    assert dec.trace.duckdb_ms is None


# --------------------------------------------------------------------------- #
# LIKE parameter embedded in a string literal (`b LIKE '%[TYPE]'`)
# --------------------------------------------------------------------------- #
def _like_setup(cross_check_rate=1.0):
    """A bespoke engine bound to `SELECT count(*) FROM t WHERE b LIKE '%<TYPE>'`, where the
    parameter lives inside the literal and reaches the engine with the `%` peeled off."""
    policy = RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=cross_check_rate)
    con = synnodb.connect(policy=policy, registry=TemplateRegistry())
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'yy'),(3,'ay'),(4,'z'),(5,'z')")
    b_values = ["x", "yy", "ay", "z", "z"]
    calls = {"n": 0}

    def fn(ph):
        calls["n"] += 1
        # The engine sees the bare parameter ("y"), never the "%y" pattern; it applies the
        # `%<suffix>` semantics itself, exactly as the compiled TPC-H Q2 binary does.
        suffix = ph["TYPE"]
        n = sum(1 for v in b_values if v.endswith(suffix))
        return pa.table({"c": pa.array([n], pa.int64())})

    engine = LocalCallableEngine("elike", {"2": fn})
    binding = register_engine(
        con,
        template_sql="SELECT count(*) AS c FROM t WHERE b LIKE ?",
        engine=engine,
        placeholders=[PlaceholderSpec("TYPE", "VARCHAR", "%", "")],
        query_id="2",
    )
    return con, calls, binding


def test_like_affix_query_routes_and_equals_duckdb():
    con, calls, _ = _like_setup()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE '%y'"
    result = con.execute(sql).fetchall()
    assert calls["n"] == 1  # the engine ran (parameter bound as "y", not "%y")
    assert result == _duckdb_answer(con, sql)  # and matches DuckDB's LIKE exactly


def test_like_without_wildcard_is_a_different_query_and_falls_back():
    # `b LIKE 'y'` shares the coarse structural key with the template but is a different query
    # (exact match, no wildcard). The affix guard must refuse it so DuckDB serves it.
    con, calls, _ = _like_setup()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE 'y'"
    result = con.execute(sql).fetchall()
    assert calls["n"] == 0  # engine never ran
    assert result == _duckdb_answer(
        con, sql
    )  # DuckDB's answer (0 rows exactly equal "y")


def test_like_with_wildcard_in_core_falls_back():
    # `b LIKE '%y%'` carries the template's `%` prefix, but its core (`y%`) still contains a
    # wildcard: SQL treats it as a pattern while the engine would take it as a literal word.
    # Binding must refuse so DuckDB serves it (and the healthy engine is never cross-checked
    # against a query outside its contract, which would quarantine it).
    con, calls, _ = _like_setup()
    for sql in (
        "SELECT count(*) AS c FROM t WHERE b LIKE '%y%'",
        "SELECT count(*) AS c FROM t WHERE b LIKE '%_y'",
    ):
        result = con.execute(sql).fetchall()
        assert calls["n"] == 0  # engine never ran
        assert result == _duckdb_answer(con, sql)


def _q13_setup(cross_check_rate=1.0):
    """Q13 shape: one literal packs two words (`b LIKE '%[W1]%[W2]%'`), so the template has a
    single SQL `?` bound to two engine parameters. The engine receives both, peeled and split
    out of the single bound pattern."""
    from synnodb.workloads.engine_publish import derive_template

    policy = RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=cross_check_rate)
    con = synnodb.connect(policy=policy, registry=TemplateRegistry())
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute(
        "INSERT INTO t VALUES (1,'redcrab'),(2,'bluecrab'),(3,'redfish')"
    )
    rows = ["redcrab", "bluecrab", "redfish"]
    calls = {"n": 0}

    def fn(ph):
        calls["n"] += 1
        w1, w2 = ph["W1"], ph["W2"]  # bare words, not the '%w1%w2%' pattern

        def matches(v):
            i = v.find(w1)
            return i >= 0 and v.find(w2, i + len(w1)) >= 0

        return pa.table({"c": pa.array([sum(matches(v) for v in rows)], pa.int64())})

    marker, specs = derive_template(
        "SELECT count(*) AS c FROM t WHERE b LIKE '%[W1]%[W2]%'",
        [{"W1": "red", "W2": "crab"}],
    )
    register_engine(
        con,
        template_sql=marker,
        engine=LocalCallableEngine("e13", {"13": fn}),
        placeholders=list(specs),
        query_id="13",
    )
    return con, calls


def test_multi_param_like_routes_and_equals_duckdb():
    con, calls = _q13_setup()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE '%red%crab%'"
    result = con.execute(sql).fetchall()
    assert calls["n"] == 1
    assert result == _duckdb_answer(con, sql)


def test_multi_param_like_parameterized_routes_and_equals_duckdb():
    # The packed pattern arrives as ONE bound value for the template's single `?`; the arity
    # guard must count binding groups (1), not engine parameters (2), and binding then splits
    # the words back out for the engine.
    con, calls = _q13_setup()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE ?"
    params = ["%red%crab%"]
    result = con.execute(sql, params).fetchall()
    assert calls["n"] == 1
    assert result == _duckdb_answer(con, sql, params)


def test_multi_param_like_parameterized_wrong_pattern_falls_back():
    # A bound pattern missing the template's constants (no leading `%`) or carrying a wildcard
    # inside a word is a different query: SQL treats it as a pattern while the engine would take
    # the words as literals. Binding must refuse both so DuckDB serves them.
    con, calls = _q13_setup()
    sql = "SELECT count(*) AS c FROM t WHERE b LIKE ?"
    for params in (["red%crab"], ["%red_%crab%"]):
        result = con.execute(sql, params).fetchall()
        assert calls["n"] == 0  # engine never ran
        assert result == _duckdb_answer(con, sql, params)


def test_in_list_query_routes_and_equals_duckdb():
    # Q22 shape: an IN-list of parameters. Exercises the `:synpN` rewrite that lets sqlglot parse
    # placeholders inside `IN (...)`.
    from synnodb.workloads.engine_publish import derive_template

    policy = RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0)
    con = synnodb.connect(policy=policy, registry=TemplateRegistry())
    con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
    con.duckdb.execute("INSERT INTO t VALUES (1,'17'),(2,'29'),(3,'18'),(4,'99')")
    rows = ["17", "29", "18", "99"]
    calls = {"n": 0}

    def fn(ph):
        calls["n"] += 1
        wanted = {ph["I1"], ph["I2"], ph["I3"]}
        return pa.table({"c": pa.array([sum(v in wanted for v in rows)], pa.int64())})

    marker, specs = derive_template(
        "SELECT count(*) AS c FROM t WHERE b IN ('[I1]','[I2]','[I3]')",
        [{"I1": "17", "I2": "29", "I3": "18"}],
    )
    register_engine(
        con,
        template_sql=marker,
        engine=LocalCallableEngine("e22", {"22": fn}),
        placeholders=list(specs),
        query_id="22",
    )
    sql = "SELECT count(*) AS c FROM t WHERE b IN ('29','18','99')"
    result = con.execute(sql).fetchall()
    assert calls["n"] == 1
    assert result == _duckdb_answer(con, sql)


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
