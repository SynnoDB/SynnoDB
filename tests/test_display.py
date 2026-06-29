"""Interactive display: the result table renderer, the query-time / speedup footer with a speed
emoji, and the performance-safe spinner (a no-op off a TTY so the hot path pays nothing)."""
from __future__ import annotations

import time

import pyarrow as pa
import pytest

import synnodb
from synnodb.duckdb_compat.display import (
    QueryTiming,
    Spinner,
    _NullSpinner,
    format_footer,
    render_table,
    speed_badge,
)
from synnodb.router import (
    LocalCallableEngine,
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)


# ---- renderer --------------------------------------------------------------
def test_render_table_has_header_types_and_rows():
    t = pa.table({"name": pa.array(["a", "b"]), "n": pa.array([1, 2], pa.int64())})
    out = render_table(t)
    assert "name" in out and "n" in out and "int64" in out and "string" in out
    assert "a" in out and "2" in out
    assert out.startswith("┌") and "│" in out and out.rstrip().endswith("┘")


def test_render_table_truncates_rows_and_long_cells():
    t = pa.table({"x": pa.array(list(range(100)), pa.int64())})
    out = render_table(t, max_rows=5)
    assert "95 more row(s)" in out and "100 total" in out
    long = pa.table({"s": pa.array(["z" * 200])})
    assert "…" in render_table(long, max_col_width=20)


def test_render_table_handles_nulls_and_empty():
    t = pa.table({"v": pa.array([1, None], pa.int64())})
    assert "NULL" in render_table(t)
    assert "0 columns" in render_table(pa.table({}))


# ---- speedup badge + footer ------------------------------------------------
def test_speed_badge_tiers():
    assert speed_badge(100) == "\U0001f525"   # fire
    assert speed_badge(20) == "\U0001f680"    # rocket
    assert speed_badge(5) == "⚡"
    assert speed_badge(1.5) == "\U0001f642"
    assert speed_badge(0.5) == "\U0001f422"   # turtle


def test_footer_engine_with_real_speedup():
    f = format_footer(QueryTiming("engine", engine_ms=2.0, duckdb_ms=20.0))
    assert "synno engine" in f and "2.0 ms" in f and "10.0× vs DuckDB" in f and "\U0001f680" in f


def test_footer_engine_with_estimate_and_without_data():
    est = format_footer(QueryTiming("engine", engine_ms=2.0, duckdb_ms_estimated=18.0))
    assert "~9.0× vs DuckDB" in est and "(est.)" in est
    bare = format_footer(QueryTiming("engine", engine_ms=2.0))
    assert "routed (cross-check sampled)" in bare


def test_footer_duckdb():
    assert "DuckDB" in format_footer(QueryTiming("duckdb", duckdb_ms=12.3)) and "12.3 ms" in _f("duckdb", 12.3)


def _f(served, dms):
    return format_footer(QueryTiming(served, duckdb_ms=dms))


# ---- spinner: performance-safe --------------------------------------------
class _FakeTTY:
    def __init__(self, tty: bool = True):
        self._tty = tty
        self.writes = []

    def isatty(self):
        return self._tty

    def write(self, s):
        self.writes.append(s)

    def flush(self):
        pass


def test_spinner_is_noop_off_tty():
    # The hot path: a non-interactive stream yields a no-op (no thread, no output).
    s = Spinner.for_stream(_FakeTTY(tty=False))
    assert isinstance(s, _NullSpinner)
    with s:
        pass


def test_spinner_does_not_draw_for_fast_work():
    tty = _FakeTTY(tty=True)
    with Spinner.for_stream(tty, delay=0.2):
        pass  # finishes well within the delay -> never draws
    assert tty.writes == []


def test_spinner_draws_then_erases_for_slow_work():
    tty = _FakeTTY(tty=True)
    with Spinner.for_stream(tty, delay=0.02, interval=0.02):
        time.sleep(0.12)  # outlives the delay -> draws frames
    drew = "".join(tty.writes)
    assert "running query" in drew and "\r" in drew  # drew, and erased the line on exit


# ---- end to end through the connection -------------------------------------
def test_connection_repr_shows_table_and_duckdb_footer():
    con = synnodb.connect()
    try:
        con.duckdb.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
        con.duckdb.execute("INSERT INTO t VALUES (1,'x'),(2,'y')")
        con.execute("SELECT * FROM t ORDER BY a")
        out = repr(con)
        assert "│" in out and "DuckDB" in out
        assert con._last["served_by"] == "duckdb" and con._last["duckdb_ms"] is not None
        # Fetching still works after display (the fallback result was materialized once).
        assert con.fetchall() == [(1, "x"), (2, "y")]
    finally:
        con.close()


def test_connection_routed_footer_shows_engine_speedup_and_caches_duckdb_time():
    def eng(ph):
        return pa.table({"c": pa.array([3], pa.int64())})

    con = synnodb.connect(policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
                          registry=TemplateRegistry())
    try:
        con.duckdb.execute("CREATE TABLE t(a INTEGER)")
        con.duckdb.execute("INSERT INTO t VALUES (1),(2),(3)")
        binding = register_engine(
            con, template_sql="SELECT count(*) AS c FROM t WHERE a >= 1",
            engine=LocalCallableEngine("synno-x", {"1": eng}), placeholders=[PlaceholderSpec("p0", "INTEGER")],
        )
        con.execute("SELECT count(*) AS c FROM t WHERE a >= 1")
        out = repr(con)
        assert "synno engine" in out and "vs DuckDB" in out
        assert con._last["served_by"] == "engine"
        # The cross-check populated the per-template DuckDB-time cache (for later estimates).
        assert con._router.last_duckdb_ms(binding.template_id) is not None
    finally:
        con.close()
