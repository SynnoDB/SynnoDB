"""The query_data data-inspection tool: read-only SQL against the benchmark subset.

Covers both subset shapes (a DuckDB-native ``subset.duckdb`` and a parquet layout), the
strict read-only gate, row-cap/truncation, and error surfacing - plus that the OpenAI-SDK
FunctionTool wrapper (shared by both the OpenAI and litellm model paths) invokes it correctly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")

from synnodb.tools.data_inspect import DataInspectTool
from synnodb.utils.utils import ServeFrom

_TABLES = ("nation", "orders")


def _build_source(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE nation AS SELECT i AS n_nationkey FROM range(5) t(i)")
    con.execute(
        "CREATE TABLE orders AS "
        "SELECT i AS o_orderkey, (i % 5) AS o_nationkey, (i * 1.5)::DECIMAL(15,2) AS o_total "
        "FROM range(200) t(i)"
    )
    con.close()


def _make_duckdb_subset(base: Path) -> None:
    subset_dir = base / "fraction1"
    subset_dir.mkdir(parents=True)
    _build_source(subset_dir / "subset.duckdb")


def _make_parquet_subset(base: Path) -> None:
    subset_dir = base / "fraction1"
    subset_dir.mkdir(parents=True)
    src = base / "_src.duckdb"
    _build_source(src)
    con = duckdb.connect(str(src), read_only=True)
    for table in _TABLES:
        out = (subset_dir / f"{table}.parquet").as_posix()
        con.execute(f"COPY {table} TO '{out}' (FORMAT PARQUET)")
    con.close()


def _provider(base: Path, serve_from: ServeFrom) -> SimpleNamespace:
    spec = SimpleNamespace(
        name="testwl",
        dataset_name="testds",
        dataset_version=None,
        serve_from=serve_from,
        tables=_TABLES,
        fast_check_sfs=(),
    )
    return SimpleNamespace(
        spec=spec,
        benchmark_sf=1.0,
        base_parquet_dir=base,
        prepare=lambda: None,
    )


@pytest.fixture(params=[ServeFrom.DUCKDB, ServeFrom.PARQUET])
def tool(request, tmp_path):
    base = tmp_path / "parquet_root"
    base.mkdir()
    if request.param == ServeFrom.DUCKDB:
        _make_duckdb_subset(base)
    else:
        _make_parquet_subset(base)
    return DataInspectTool(workload_provider=_provider(base, request.param))


def test_select_resolves_bare_table_names(tool):
    out = tool("SELECT count(*) AS n FROM orders")
    assert "n" in out
    assert "200" in out


def test_join_across_tables(tool):
    out = tool(
        "SELECT count(*) AS n FROM orders o JOIN nation n ON o.o_nationkey = n.n_nationkey"
    )
    assert "200" in out


def test_describe_and_aggregate(tool):
    out = tool("SELECT min(o_total) AS lo, max(o_total) AS hi FROM orders")
    assert "lo" in out and "hi" in out


def test_row_cap_and_truncation_marker(tool):
    out = tool("SELECT o_orderkey FROM orders ORDER BY o_orderkey", max_rows=5)
    # Only the capped rows are rendered, and the footer flags that more exist.
    assert "more rows exist" in out
    assert "o_orderkey" in out
    # The 6th value must not appear (cap is 5, ordered from 0).
    assert "\n5 " not in out


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE nation",
        "CREATE TEMP TABLE t AS SELECT 1",
        "INSERT INTO nation VALUES (99)",
        "UPDATE orders SET o_total = 0",
        "SELECT 1; DROP TABLE nation",  # write hidden behind a leading SELECT
    ],
)
def test_writes_are_rejected(tool, sql):
    out = tool(sql)
    assert out.startswith("Error: query_data is strictly read-only")
    # The source is untouched: the tables still hold their original rows.
    assert "200" in tool("SELECT count(*) AS n FROM orders")
    assert "5" in tool("SELECT count(*) AS n FROM nation")


def test_bad_sql_returns_error_not_raise(tool):
    out = tool("SELECT * FROM does_not_exist")
    assert out.startswith("SQL error:")


def test_empty_sql(tool):
    assert tool("   ") == "Error: empty SQL query."


def test_default_inspects_smallest_fast_check_rung(tmp_path):
    """By default the tool reads the smallest fast-check rung, not the benchmark subset."""
    base = tmp_path / "parquet_root"
    (base / "fraction0.1").mkdir(parents=True)
    small = duckdb.connect(str(base / "fraction0.1" / "subset.duckdb"))
    small.execute("CREATE TABLE nation AS SELECT i AS n_nationkey FROM range(3) t(i)")
    small.execute("CREATE TABLE orders AS SELECT i AS o_orderkey FROM range(3) t(i)")
    small.close()
    _make_duckdb_subset(base)  # fraction1 with 5 nation / 200 orders rows

    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)
    tool = DataInspectTool(workload_provider=provider)

    assert tool.sf == 0.1
    # The count reflects the small rung (3), not the benchmark subset (5).
    assert "3" in tool("SELECT count(*) AS n FROM nation")


def test_no_fast_check_ladder_falls_back_to_benchmark_sf(tmp_path):
    """A workload without a fast-check ladder inspects the benchmark subset."""
    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)  # fraction1
    tool = DataInspectTool(workload_provider=_provider(base, ServeFrom.DUCKDB))
    assert tool.sf == 1.0
    assert "200" in tool("SELECT count(*) AS n FROM orders")


def test_expensive_query_hits_wallclock_budget(tool, monkeypatch):
    """A query that overruns the budget is interrupted and reported, not left to spin."""
    import synnodb.tools.data_inspect as di

    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)
    out = tool(
        "SELECT max(a.range * b.range) AS m "
        "FROM range(10000000) a, range(10000000) b"
    )
    assert "inspection budget" in out and "cancelled" in out
    # The cached connection survives: the interrupt hit only the throwaway cursor.
    assert "200" in tool("SELECT count(*) AS n FROM orders")


def test_cache_hit_replays_without_touching_data(tmp_path):
    """A repeated query is served from the disk cache: deleting the subset afterwards proves the
    second call never re-opened the data."""
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=cache_dir
    )

    first = tool("SELECT count(*) AS n FROM orders")
    assert "200" in first
    assert list(cache_dir.glob("*.pkl")), "expected a cache entry to be written"

    # A fresh tool (no live connection) forced to answer from cache alone must reproduce it,
    # even though the underlying database is now gone.
    import shutil

    shutil.rmtree(base)
    replay = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=cache_dir,
        only_from_cache=True,
    )
    assert replay("SELECT count(*) AS n FROM orders") == first


def test_only_from_cache_raises_on_miss(tmp_path):
    """Under only_from_cache an uncached query cannot run - it raises rather than touch data."""
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=tmp_path / "cache",
        only_from_cache=True,
    )
    with pytest.raises(ValueError, match="only_from_cache"):
        tool("SELECT count(*) AS n FROM orders")


def test_row_cap_is_part_of_cache_key(tmp_path):
    """The row cap participates in the key, so the same SQL at a different cap is not served the
    wrong cached rendering."""
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=tmp_path / "cache"
    )
    # nation has 5 rows: a cap of 2 truncates, a cap of 100 shows them all. If the row cap were
    # not in the key, the second call would wrongly replay the first call's truncated rendering.
    capped = tool("SELECT n_nationkey FROM nation ORDER BY n_nationkey", max_rows=2)
    wide = tool("SELECT n_nationkey FROM nation ORDER BY n_nationkey", max_rows=100)
    assert "more rows exist" in capped
    assert "more rows exist" not in wide


def test_do_not_cache_never_writes(tmp_path):
    """do_not_cache runs live but leaves no cache files behind."""
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=cache_dir,
        do_not_cache=True,
    )
    assert "200" in tool("SELECT count(*) AS n FROM orders")
    assert not list(cache_dir.glob("*.pkl"))


def test_timeout_is_not_cached(tmp_path, monkeypatch):
    """A timeout is a host-dependent guard, not a property of the data, so it is never cached."""
    import synnodb.tools.data_inspect as di
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=cache_dir
    )
    out = tool(
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b"
    )
    assert "inspection budget" in out
    assert not list(cache_dir.glob("*.pkl"))


def test_runtime_tracker_credited_on_cache_hit(tmp_path):
    """A cache hit credits the original query's wall-clock back to the runtime tracker as skipped
    time, matching the shell tool's accounting."""
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    skipped: list[float] = []
    tracker = SimpleNamespace(add_skipped_time=lambda t: skipped.append(t))

    DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=cache_dir
    )("SELECT count(*) AS n FROM orders")
    assert not skipped  # first run executed live - nothing skipped

    DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=cache_dir,
        runtime_tracker=tracker,
    )("SELECT count(*) AS n FROM orders")
    assert len(skipped) == 1 and skipped[0] >= 0.0


def test_factory_invokes_tool(tmp_path):
    """The FunctionTool wrapper (used identically by the OpenAI and litellm paths) runs the tool."""
    from synnodb.llm.sdk.agents_sdk.openai_make_data_inspect_tool import (
        make_openai_data_inspect_tool,
    )

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    tool = DataInspectTool(workload_provider=_provider(base, ServeFrom.DUCKDB))
    ft = make_openai_data_inspect_tool(tool)

    assert ft.name == "query_data"
    schema = ft.params_json_schema
    assert "sql" in schema["properties"]
    assert "max_rows" in schema["properties"]

    out = asyncio.run(ft.on_invoke_tool(None, '{"sql": "SELECT count(*) AS n FROM orders"}'))
    assert "200" in out
