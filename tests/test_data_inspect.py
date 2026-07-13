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
        # Mirrors WorkloadSpec.subset_files: the files a subset physically needs, which is how the
        # tool decides a subset is complete enough to offer the agent.
        subset_files=lambda subset_dir: (
            [subset_dir / "subset.duckdb"]
            if serve_from == ServeFrom.DUCKDB
            else [subset_dir / f"{table}.parquet" for table in _TABLES]
        ),
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


def _make_two_subsets(base: Path) -> None:
    """A root holding two DuckDB-native subsets: ``fraction0.1`` (3 nation / 3 orders rows) and
    ``fraction1``, the benchmark one (5 nation / 200 orders). The differing row counts are how a
    test tells which subset a query actually read."""
    (base / "fraction0.1").mkdir(parents=True)
    small = duckdb.connect(str(base / "fraction0.1" / "subset.duckdb"))
    small.execute("CREATE TABLE nation AS SELECT i AS n_nationkey FROM range(3) t(i)")
    small.execute("CREATE TABLE orders AS SELECT i AS o_orderkey FROM range(3) t(i)")
    small.close()
    _make_duckdb_subset(base)  # fraction1 with 5 nation / 200 orders rows


def _two_subset_tool(base: Path, **kwargs) -> DataInspectTool:
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)
    return DataInspectTool(workload_provider=provider, **kwargs)


def test_default_inspects_smallest_fast_check_rung(tmp_path):
    """By default the tool reads the smallest fast-check rung, not the benchmark subset."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)

    assert tool.sf == 0.1
    # The count reflects the small rung (3), not the benchmark subset (5).
    assert "3" in tool("SELECT count(*) AS n FROM nation")


def test_explicit_sf_reads_the_requested_subset(tmp_path):
    """The agent picks the subset per query: the same SQL answers from whichever it names."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)

    # 3 orders in the small subset, 200 in the benchmark one - so the row count identifies it.
    assert "3" in tool("SELECT count(*) AS n FROM orders", sf=0.1)
    assert "200" in tool("SELECT count(*) AS n FROM orders", sf=1)
    # Both subsets stay usable: each keeps its own connection.
    assert "3" in tool("SELECT count(*) AS n FROM orders", sf=0.1)


def test_only_materialized_subsets_are_offered(tmp_path):
    """The menu is what is complete on disk, not what the spec's SF ladder claims."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    # fraction0.5 is in the spec's fast-check ladder but was never materialized.
    assert _two_subset_tool(base).available_subsets() == [0.1, 1]


def test_unknown_subset_lists_the_available_ones(tmp_path):
    """An sf that does not exist is refused as text (never an exception), naming the real ones."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)

    out = tool("SELECT count(*) FROM orders", sf=99)
    assert out.startswith("Error: no data subset 99")
    assert "0.1" in out and "1" in out
    # The tool is still usable afterwards.
    assert "3" in tool("SELECT count(*) AS n FROM orders")


def test_subset_is_part_of_the_cache_key(tmp_path):
    """The same SQL at two subsets caches (and replays) as two distinct results."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    cache_dir = tmp_path / "cache"
    tool = _two_subset_tool(base, cache_dir=cache_dir)

    assert "3" in tool("SELECT count(*) AS n FROM orders", sf=0.1)
    assert "200" in tool("SELECT count(*) AS n FROM orders", sf=1)
    assert len(list(cache_dir.glob("*.pkl"))) == 2, (
        "one entry per subset, not one shared"
    )

    # A replay with the data gone must still tell the two subsets apart.
    import shutil

    shutil.rmtree(base)
    replay = _two_subset_tool(base, cache_dir=cache_dir, only_from_cache=True)
    assert "3" in replay("SELECT count(*) AS n FROM orders", sf=0.1)
    assert "200" in replay("SELECT count(*) AS n FROM orders", sf=1)


def test_timeout_points_at_a_smaller_subset(tmp_path, monkeypatch):
    """A query that overruns on a big subset is told which cheaper subsets it could retry on."""
    import synnodb.tools.data_inspect as di

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)

    out = tool(
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b",
        sf=1,
    )
    assert "inspection budget on subset 1" in out
    assert "smaller subset (0.1)" in out
    # The interrupt hit only that subset's throwaway cursor; the other subset is untouched.
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 15.0)
    assert "3" in tool("SELECT count(*) AS n FROM orders", sf=0.1)
    assert "200" in tool("SELECT count(*) AS n FROM orders", sf=1)


def test_timeout_on_smallest_subset_suggests_no_smaller_one(tmp_path, monkeypatch):
    """Nothing cheaper to fall back to, so the message sticks to simplifying the query."""
    import synnodb.tools.data_inspect as di

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)

    out = tool(
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b",
        sf=0.1,
    )
    assert "inspection budget on subset 0.1" in out
    assert "smaller subset" not in out


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
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b"
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
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=tmp_path / "cache",
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


def test_timeout_is_cached_and_replays(tmp_path, monkeypatch):
    """Both successful and unsuccessful executions are cached: a timeout is written to disk and
    replays from cache alone, exactly like a result set or a SQL error."""
    import synnodb.tools.data_inspect as di
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)
    slow_sql = (
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b"
    )
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=cache_dir
    )
    out = tool(slow_sql)
    assert "inspection budget" in out
    assert list(cache_dir.glob("*.pkl")), "expected the timeout to be cached"

    # A fresh tool forced to answer from cache alone must reproduce the timeout without ever
    # re-running the (now removed) query - the whole point of caching unsuccessful executions.
    import shutil

    shutil.rmtree(base)
    replay = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=cache_dir,
        only_from_cache=True,
    )
    assert replay(slow_sql) == out


def test_timeout_replay_reports_timeout_status(tmp_path, monkeypatch):
    """A cached timeout replays with the ``timeout`` status (not ``ok``), so the dashboard row
    still reads as a failure - the status is recovered purely from the rendered text."""
    import synnodb.tools.data_inspect as di
    from synnodb.tools.data_inspect import DataInspectTool

    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(di, "QUERY_TIMEOUT_S", 0.2)
    slow_sql = (
        "SELECT max(a.range * b.range) AS m FROM range(10000000) a, range(10000000) b"
    )
    DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB), cache_dir=cache_dir
    )(slow_sql)

    collector = _FakeCollector()
    DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        cache_dir=cache_dir,
        run_stats_collector=collector,
    )(slow_sql)
    row = collector.metrics[0]
    assert row["data_inspect/status"] == "timeout"
    assert row["data_inspect/error"] is True
    assert row["data_inspect/cached"] is True
    # The activity summary must stay cache-status-independent (it feeds the supervisor
    # prompt / LLM cache key); cache status lives in the metric above, not the string.
    assert collector.activity[-1] == "Data Inspect Tool called: timeout"


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

    out = asyncio.run(
        ft.on_invoke_tool(None, '{"sql": "SELECT count(*) AS n FROM orders"}')
    )
    assert "200" in out


class _FakeCollector:
    """Captures the reporting calls the live dashboard / supervisor rely on, without the real
    turn-accounting machinery of RunStatsCollector."""

    def __init__(self):
        self.metrics: list[dict] = []
        self.activity: list[str] = []

    def log_metrics_callback(self, metrics, log_and_increment=False):
        assert log_and_increment is True
        self.metrics.append(metrics)

    def add_to_activity_summary(self, entry):
        self.activity.append(entry)


def _reporting_tool(tmp_path, **kwargs):
    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)
    collector = _FakeCollector()
    tool = DataInspectTool(
        workload_provider=_provider(base, ServeFrom.DUCKDB),
        run_stats_collector=collector,
        **kwargs,
    )
    return tool, collector


def test_successful_inspection_is_reported_to_live_ui(tmp_path):
    """Every inspection must surface to the live dashboard (a metrics row of type data_inspect)
    and the supervisor activity log, exactly like the shell / compile / run tools."""
    tool, collector = _reporting_tool(tmp_path)
    tool("SELECT count(*) AS n FROM orders")

    assert len(collector.metrics) == 1
    row = collector.metrics[0]
    assert row["type"] == "data_inspect"
    assert row["data_inspect/status"] == "ok"
    assert row["data_inspect/error"] is False
    assert row["data_inspect/cached"] is False
    assert "SELECT count(*)" in row["data_inspect/sql"]
    assert collector.activity == ["Data Inspect Tool called: ok"]


def test_error_and_rejected_inspections_are_reported(tmp_path):
    """A SQL error and a rejected write both still report - flagged as errors so the dashboard row
    reads as a failure rather than going silent."""
    tool, collector = _reporting_tool(tmp_path)

    tool("SELECT * FROM does_not_exist")
    tool("DROP TABLE orders")
    tool("")

    statuses = [m["data_inspect/status"] for m in collector.metrics]
    assert statuses == ["sql_error", "rejected", "empty"]
    assert all(m["data_inspect/error"] is True for m in collector.metrics)
    assert collector.activity == [
        "Data Inspect Tool called: sql_error",
        "Data Inspect Tool called: rejected",
        "Data Inspect Tool called: empty",
    ]


def test_cache_hit_is_reported_as_cached(tmp_path):
    """A cache hit is still an inspection the user should see - reported with cached=True so the
    row is labelled, and the runtime credited back exactly as before."""
    tool, collector = _reporting_tool(tmp_path, cache_dir=tmp_path / "cache")
    tool("SELECT count(*) AS n FROM orders")
    tool("SELECT count(*) AS n FROM orders")

    assert [m["data_inspect/cached"] for m in collector.metrics] == [False, True]
    # cached=True is captured in the metric, but the supervisor-facing activity line
    # must be identical to the uncached run so the supervisor LLM cache still hits on replay.
    assert collector.activity == [
        "Data Inspect Tool called: ok",
        "Data Inspect Tool called: ok",
    ]


def test_reported_sf_is_the_queried_subset(tmp_path):
    """The dashboard row shows the subset the query actually ran on, not the tool's default."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    collector = _FakeCollector()
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)
    tool = DataInspectTool(workload_provider=provider, run_stats_collector=collector)

    tool("SELECT count(*) AS n FROM orders")
    tool("SELECT count(*) AS n FROM orders", sf=1)

    assert [m["data_inspect/sf"] for m in collector.metrics] == [0.1, 1.0]


def test_subset_menu_names_the_benchmark_subset(tmp_path):
    """The menu the agent reads (tool description + prompts) marks the default and the benchmark
    subset, and points at the benchmark one for absolute counts - the whole point of choosing."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)

    menu = subset_menu_for(provider)
    assert "`0.1` (default, smallest)" in menu
    assert "benchmark scale" in menu
    assert (
        "Measure any number you bake into the design on the benchmark subset `1`"
        in menu
    )


def test_subset_menu_forbids_extrapolating_row_counts(tmp_path):
    """Subsets are not uniform shrinks: the downscaler keeps small dimension tables whole and sizes
    joined tables by referential propagation, and generated scale factors hold reference tables
    fixed. The menu must never tell the agent to multiply a subset's counts by a ratio - an earlier
    version did, and a real run scaled a whole-kept table up by 50x and decided the subset labels
    were inverted."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)

    menu = subset_menu_for(provider)
    assert "never scale a count by a ratio" in menu
    assert "shrink unevenly per table" in menu
    # The ratio-extrapolation advice must be gone, in every spelling.
    assert "extrapolate" not in menu
    assert "of the benchmark rows" not in menu
    # And it says an unchanged count across subsets is expected, not a bug worth chasing.
    assert "an unchanged count between subsets is expected" in menu


def test_subset_menu_warns_that_ranges_and_distincts_do_not_transfer(tmp_path):
    """A subset is a *sample*: measured against the real downscaler, a 5% subset understated a
    bounded column's max (141 vs 148.5) and its distinct count (27 vs 100). Those are exactly the
    numbers a physical design is sized from, so the menu must name them as not transferring - an
    earlier version claimed "value ranges and distinct counts carry over", which would have an
    agent pick a fixed-width type that overflows on the real data."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1, 0.5)

    menu = subset_menu_for(provider)
    # A sample's extremes are contained within the true ones, so a type sized from them overflows.
    assert "min/max lie inside the true range" in menu
    assert "overflows or undersizes at full scale" in menu
    # And its distinct counts are understated, so a dictionary sized from them is far too small.
    assert "distinct counts are understated" in menu
    # The old, wrong claim must not reappear in any form.
    assert "value ranges, distinct counts, null density, distributions" not in menu


def test_subset_menu_is_empty_without_data(tmp_path):
    """No subsets on disk - the prompts simply say nothing about them rather than lying."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    base.mkdir()
    assert subset_menu_for(_provider(base, ServeFrom.DUCKDB)) == ""


def test_int_and_float_spellings_share_one_cache_entry(tmp_path):
    """`sf=1` from the agent (a JSON float) and the spec's own int `1` are the same subset, so they
    must not cache the identical inspection twice."""
    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    cache_dir = tmp_path / "cache"
    tool = _two_subset_tool(base, cache_dir=cache_dir)

    tool("SELECT count(*) AS n FROM orders", sf=1)
    tool("SELECT count(*) AS n FROM orders", sf=1.0)
    assert len(list(cache_dir.glob("*.pkl"))) == 1


def _one_subset_root(tmp_path: Path) -> Path:
    """A root where only the benchmark subset (fraction1) exists - the spec's smallest fast-check
    rung was never generated. Real for a built-in workload: the sf<N> dirs come from an
    out-of-band dbgen step, so nothing guarantees the default rung is on disk."""
    base = tmp_path / "parquet_root"
    base.mkdir()
    _make_duckdb_subset(base)  # fraction1 only
    return base


def test_unmaterialized_default_is_reported_with_what_exists(tmp_path):
    """Omitting sf when the spec's default rung was never generated must not blow up with a bare
    FileNotFoundError - the agent is told which subsets do exist, and must not be told to omit sf
    (that is what just failed)."""
    base = _one_subset_root(tmp_path)
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1,)  # default 0.1, but only fraction1 is on disk
    tool = DataInspectTool(workload_provider=provider)

    out = tool("SELECT count(*) AS n FROM orders")
    assert out.startswith("Error: no data subset 0.1")
    assert "Available subsets: 1" in out
    assert "omit sf" not in out
    # And the subset that does exist still answers.
    assert "200" in tool("SELECT count(*) AS n FROM orders", sf=1)


def test_menu_never_advertises_an_unmaterialized_default(tmp_path):
    """The prompt menu promises only what is on disk: no 'omit it for the default' when the
    default subset does not exist, and no 'default' label on any subset."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = _one_subset_root(tmp_path)
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1,)

    menu = subset_menu_for(provider)
    assert "`1` (smallest, benchmark scale" in menu
    assert "default" not in menu
    assert "omit" not in menu


def test_menu_is_empty_for_a_provider_that_cannot_back_the_tool(tmp_path):
    """main only builds query_data for providers exposing spec + benchmark_sf; the prompts must
    stay silent about subsets for anything else rather than raising at prompt-render time."""
    from synnodb.tools.data_inspect import subset_menu_for

    assert subset_menu_for(SimpleNamespace()) == ""


def test_legacy_sf_dirs_and_parquet_layout(tmp_path):
    """The other real subset shape: a built-in workload's legacy ``sf<N>/`` dirs holding one
    ``<table>.parquet`` per table (TPC-H), rather than the downscaler's ``fraction<f>/subset.duckdb``.
    Both conventions must be discoverable and selectable."""
    base = tmp_path / "parquet_root"
    for sf, n_orders in ((1, 50), (20, 900)):
        subset_dir = base / f"sf{sf}"
        subset_dir.mkdir(parents=True)
        con = duckdb.connect()
        con.execute(
            f"CREATE TABLE nation AS SELECT i AS n_nationkey FROM range({sf}) t(i)"
        )
        con.execute(
            f"CREATE TABLE orders AS SELECT i AS o_orderkey, (i % 5) AS o_nationkey, "
            f"(i * 1.5)::DECIMAL(15,2) AS o_total FROM range({n_orders}) t(i)"
        )
        for table in _TABLES:
            con.execute(
                f"COPY {table} TO '{(subset_dir / (table + '.parquet')).as_posix()}' (FORMAT PARQUET)"
            )
        con.close()

    provider = _provider(base, ServeFrom.PARQUET)
    provider.spec.fast_check_sfs = (1,)
    provider.benchmark_sf = 20
    tool = DataInspectTool(workload_provider=provider)

    assert tool.available_subsets() == [1, 20]
    assert tool.sf == 1
    # Row counts differ per subset, so they prove which directory was actually read.
    assert "50" in tool("SELECT count(*) AS n FROM orders")
    assert "900" in tool("SELECT count(*) AS n FROM orders", sf=20)

    from synnodb.tools.data_inspect import subset_menu_for

    menu = subset_menu_for(provider)
    assert "`1` (default, smallest)" in menu
    assert "`20` (benchmark scale" in menu
    assert (
        "Measure any number you bake into the design on the benchmark subset `20`"
        in menu
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SUMMARIZE nation; SUMMARIZE orders",
        "DESCRIBE nation; DESCRIBE orders",
        "SELECT count(*) FROM nation; SUMMARIZE orders",
    ],
)
def test_read_only_batch_is_refused_as_a_batch_not_as_a_write(tool, sql):
    """A batch of read-only statements must be refused for being a batch. The read-only classifier
    only passes a multi-statement batch when every statement is a plain SELECT/WITH, so these used
    to come back as "this statement would write or change state" - which is untrue, and a real run
    burned turns on an agent hunting for a write it never wrote."""
    out = tool(sql)
    assert out.startswith("Error: query_data runs one statement per call")
    assert "read-only" not in out
    # Still usable: the refusal is about shape, and the same statements work one at a time.
    assert "5" in tool("SELECT count(*) AS n FROM nation")


def test_batch_hiding_a_write_is_still_refused_as_a_write(tool):
    """The batch message must not become a way to soften a write: anything containing a write
    keeps falling through to the read-only gate."""
    out = tool("SELECT 1; DROP TABLE nation")
    assert out.startswith("Error: query_data is strictly read-only")
    assert "5" in tool("SELECT count(*) AS n FROM nation")


def test_semicolon_inside_a_string_literal_is_not_a_batch(tool):
    """DuckDB's parser does the splitting, so a semicolon in a literal is not a statement break."""
    out = tool("SELECT ';' AS semi")
    assert "semi" in out
    assert "one statement per call" not in out


def test_menu_points_at_the_largest_subset_when_benchmark_is_absent(tmp_path):
    """Absolute counts have to be measured somewhere real. When the benchmark subset was never
    materialized, the menu names the largest subset that does exist rather than pointing the agent
    at one it cannot read."""
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)  # fraction0.1 + fraction1
    provider = _provider(base, ServeFrom.DUCKDB)
    provider.spec.fast_check_sfs = (0.1,)
    provider.benchmark_sf = 20  # a benchmark subset nobody generated

    menu = subset_menu_for(provider)
    # No subset on disk may carry the benchmark label. (The prose still says "benchmark scale"
    # when warning about cost, so assert on the label itself, not the phrase.)
    assert "(benchmark scale" not in menu
    assert "on the largest available subset (`1`)" in menu


def test_no_agent_facing_text_tells_the_agent_to_extrapolate(tmp_path):
    """Three surfaces carry query_data guidance - the agent instructions (DATA_INSPECT_HINT), the
    tool description, and the subset menu in the planner prompts. They must agree that a subset is
    a *sample*: none may tell the agent to scale a small subset's numbers up to benchmark scale.
    They drifted once (the menu was rewritten and the instructions hint was left saying "row counts
    ... must be extrapolated to benchmark scale"), and the agent was getting both at once."""
    from synnodb.llm.sdk.agents_sdk.openai_make_data_inspect_tool import _description
    from synnodb.llm.sdk.agents_sdk.openai_sdk import DATA_INSPECT_HINT
    from synnodb.tools.data_inspect import subset_menu_for

    base = tmp_path / "parquet_root"
    _make_two_subsets(base)
    tool = _two_subset_tool(base)

    surfaces = {
        "agent instructions": DATA_INSPECT_HINT,
        "tool description": _description(tool),
        "subset menu": subset_menu_for(tool.workload_provider),
    }
    for name, text in surfaces.items():
        lowered = text.lower()
        assert "extrapolat" not in lowered, f"{name} tells the agent to extrapolate"
        assert "scale up" not in lowered, f"{name} tells the agent to scale numbers up"
        # Every surface steers to the smallest subset - that is the point of choosing.
        assert "smallest" in lowered, f"{name} lost the prefer-smallest rule"
