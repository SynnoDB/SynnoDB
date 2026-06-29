"""Phase 0 of workload-agnostic plan / root-cause fix for G7.

A run scoped to a query subset (e.g. just Q1) must confine the workload provider to
exactly those queries — because all scaffolding (queryX files, queries.md, query_impl,
args_parser) and the run/validate defaults iterate `provider.query_ids`. Before the fix,
`OLAPWorkloadProvider` always defaulted to the whole benchmark (_get_all_query_ids),
so a Q1 run scaffolded and validated all 22 queries.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider_olap import (
    OLAPWorkload,
    OLAPWorkloadProvider,
    _get_all_query_ids,
    _resolve_query_subset,
)


def _provider(query_ids):
    # __init__ touches no filesystem (schema/sql/tables are in-process), so a dummy
    # parquet dir is fine for testing scope resolution.
    return OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=Path("/tmp/does_not_matter"),
        db_storage=DBStorage.IN_MEMORY,
        query_ids=query_ids,
    )


# ---- pure resolver ---------------------------------------------------------------
def test_resolve_none_returns_full_catalog():
    full = ["1", "2", "3"]
    assert _resolve_query_subset(full, None, "tpch") == full
    assert _resolve_query_subset(full, [], "tpch") == full


def test_resolve_subset_keeps_canonical_order_and_dedups():
    full = [str(i) for i in range(1, 23)]
    assert _resolve_query_subset(full, ["6", "1", "6"], "tpch") == ["1", "6"]


def test_resolve_unknown_id_raises():
    with pytest.raises(ValueError, match="not valid for benchmark"):
        _resolve_query_subset(["1", "2"], ["99"], "tpch")


# ---- provider wiring -------------------------------------------------------------
def test_provider_scoped_to_single_query():
    assert _provider(["1"]).query_ids == ["1"]


def test_provider_defaults_to_full_benchmark():
    p = _provider(None)
    assert p.query_ids == _get_all_query_ids("tpch")
    assert len(p.query_ids) == 22


def test_provider_rejects_unknown_query():
    with pytest.raises(ValueError):
        _provider(["1", "tablescan"])


def test_sql_dict_stays_full_even_when_scoped():
    # sql_dict is keyed access; keeping it full is fine and lets a scoped run still
    # resolve its own SQL. Only query_ids drives scaffolding scope.
    p = _provider(["1"])
    assert "Q1" in p.sql_dict and "Q22" in p.sql_dict


# ---- scaffolding actually scopes (the user's "copy only that query" concern) -----
def test_scaffolding_only_emits_scoped_query_files(tmp_path):
    from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import (
        OLAPPrepareWorkspace,
    )

    prep = OLAPPrepareWorkspace(
        db_storage=DBStorage.IN_MEMORY,
        workload_provider=_provider(["1"]),
        workspace_dir=tmp_path,
        git_snapshotter=None,
    )
    files = prep._assemble_usecase_files()

    # exactly the in-scope query files; none of the other 21
    assert "query1.cpp" in files and "query1.hpp" in files
    assert "query2.cpp" not in files and "query22.cpp" not in files
    emitted = sorted(
        f for f in files if f[5:6].isdigit() and f.startswith("query")
    )
    assert emitted == ["query1.cpp", "query1.hpp"], emitted

    # queries.md (the doc the agent reads) only contains Q1
    assert "Query **1**" in files["queries.md"]
    assert "Query **2**" not in files["queries.md"]
