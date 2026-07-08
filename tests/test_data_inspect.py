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
    spec = SimpleNamespace(serve_from=serve_from, tables=_TABLES)
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
