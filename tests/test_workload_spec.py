"""Phase 1: the WorkloadSpec descriptor must reproduce exactly what the old
per-benchmark enum switches returned, for both built-in workloads. This guards the
refactor that collapsed ~9 `if benchmark == TPCH/CEB else raise` branches into spec
reads.
"""

from __future__ import annotations

import pytest

from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_spec import (
    get_workload_spec,
    is_registered,
    register_workload,
    registered_workloads,
)


def test_builtins_registered():
    assert set(registered_workloads()) >= {"tpch", "ceb"}
    assert is_registered("tpch") and is_registered("ceb")


def test_unknown_workload_raises():
    with pytest.raises(ValueError, match="Unknown workload"):
        get_workload_spec("does-not-exist")


def test_tpch_spec_parity():
    s = get_workload_spec("tpch")
    assert s.dataset_name == "tpch"
    assert s.tables == (
        "customer",
        "lineitem",
        "nation",
        "orders",
        "part",
        "partsupp",
        "region",
        "supplier",
    )
    assert s.all_query_ids == tuple(str(i) for i in range(1, 23))
    assert s.benchmark_sf == 20
    assert s.scale_factors_for(RunToolMode.FAST_CHECK) == [1, 2]
    assert s.scale_factors_for(RunToolMode.EXHAUSTIVE) == [1, 2, 20]
    assert s.scale_factors_for(RunToolMode.INGEST) == [20]
    assert s.scale_factors_for(RunToolMode.BENCHMARK) == [20]
    assert s.example_query == "Q42" and s.example_query_params == "42"
    assert s.schema_example_table == "lineitem"
    assert "Q1" in s.sql_dict() and "Q22" in s.sql_dict()
    assert isinstance(s.schema(), str) and len(s.schema()) > 0


def test_ceb_spec_parity():
    s = get_workload_spec("ceb")
    assert s.dataset_name == "imdb"
    assert len(s.tables) == 21 and "title" in s.tables and "cast_info" in s.tables
    assert s.all_query_ids[:4] == ("1a", "2a", "2b", "2c")
    assert s.benchmark_sf == 5
    assert s.scale_factors_for(RunToolMode.FAST_CHECK) == [0.25, 0.5]
    assert s.scale_factors_for(RunToolMode.EXHAUSTIVE) == [0.25, 0.5, 5]
    assert s.example_query == "Q42a"


def test_register_is_idempotent_and_overrides_by_name():
    s = get_workload_spec("tpch")
    register_workload(s)  # re-registering the same spec must not raise
    assert get_workload_spec("tpch") is s
