"""Phase 2: a brand-new workload registered purely from DATA (a directory of SQL +
existing parquet) must flow through the whole input side — spec, provider, scaffolding —
with no source edits and no enum member. Proves workload-agnosticism end to end on the
generation-input path (no LLM needed).
"""
from __future__ import annotations

import pytest

from synnodb.utils.utils import DBStorage
from synnodb.workloads.byo_workload import register_workload_from_dir
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider


@pytest.fixture
def myshop(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    sf1 = tmp_path / "data" / "sf1"
    sf1.mkdir(parents=True)
    pq.write_table(pa.table({"u_id": [1, 2, 3], "u_name": ["a", "b", "c"]}), sf1 / "users.parquet")
    pq.write_table(pa.table({"e_id": [1, 2, 3, 4], "e_user": [1, 1, 2, 3]}), sf1 / "events.parquet")

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "1.sql").write_text("SELECT count(*) AS n FROM events\n")

    spec = register_workload_from_dir("myshop", sql_dir, tmp_path / "data")
    return tmp_path, spec


def test_spec_built_from_data(myshop):
    _, spec = myshop
    assert spec.name == "myshop"
    assert spec.tables == ("events", "users")  # inferred from parquet, sorted
    assert spec.all_query_ids == ("1",)
    assert spec.sql_dict()["Q1"].strip() == "SELECT count(*) AS n FROM events"
    # schema derived from parquet (DuckDB DESCRIBE), not hand-written
    schema = spec.schema()
    assert "CREATE TABLE events" in schema and "CREATE TABLE users" in schema
    assert "e_id" in schema and "u_name" in schema
    # identity query generator for static SQL
    name, sql, placeholders = spec.query_gen_factory(None)("Q1")
    assert name == "Q1" and "FROM events" in sql and placeholders == {}


def test_provider_resolves_byo_by_name(myshop):
    tmp_path, _ = myshop
    prov = OLAPWorkloadProvider(
        benchmark="myshop",  # a plain string, not an enum member
        base_parquet_dir=tmp_path / "data",
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    assert prov.benchmark.value == "myshop"
    assert prov.query_ids == ["1"]
    assert prov.dataset_tables == ["events", "users"]


def test_scaffolding_from_byo_data(myshop):
    from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import (
        OLAPPrepareWorkspace,
    )

    tmp_path, _ = myshop
    ws = tmp_path / "ws"
    ws.mkdir()
    prov = OLAPWorkloadProvider(
        benchmark="myshop",
        base_parquet_dir=tmp_path / "data",
        db_storage=DBStorage.IN_MEMORY,
        query_ids=["1"],
    )
    prep = OLAPPrepareWorkspace(
        db_storage=DBStorage.IN_MEMORY,
        workload_provider=prov,
        workspace_dir=ws,
        git_snapshotter=None,
    )
    files = prep._assemble_usecase_files()

    # the BYO query scaffolded from its SQL; no phantom queries
    assert "query1.cpp" in files and "query2.cpp" not in files
    assert "FROM events" in files["query1.cpp"]
    # table defs generated for the BYO tables (read from parquet by name)
    assert "events" in files["parquet_reader.hpp"]
    assert "users" in files["parquet_reader.hpp"]
    assert "Query **1**" in files["queries.md"]


def test_validation_ground_truth_is_agnostic(myshop):
    """Correctness needs no precomputed answers — DuckDB runs the workload's own SQL."""
    tmp_path, spec = myshop
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    events = (tmp_path / "data" / "sf1" / "events.parquet").as_posix()
    con.execute(f"CREATE VIEW events AS SELECT * FROM read_parquet('{events}')")
    got = con.execute(spec.sql_dict()["Q1"]).fetchone()[0]
    assert got == 4


def _write_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    sf1 = tmp_path / "data" / "sf1"
    sf1.mkdir(parents=True)
    pq.write_table(pa.table({"e_id": [1, 2, 3, 4]}), sf1 / "events.parquet")
    return tmp_path / "data"


def test_queries_json_loader(tmp_path):
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = _write_parquet(tmp_path)
    qjson = tmp_path / "queries.json"
    qjson.write_text(_json.dumps({"1": "SELECT count(*) AS n FROM events",
                                  "7": "SELECT max(e_id) AS m FROM events"}))
    spec = register_workload_from_json("shopjson", qjson, parquet)
    assert spec.all_query_ids == ("1", "7")
    assert spec.sql_dict()["Q7"].startswith("SELECT max")


@pytest.mark.parametrize(
    "keys,expected",
    [
        (["1", "7"], ["1", "7"]),
        (["q1", "q7"], ["1", "7"]),
        (["Q1", "Q7"], ["1", "7"]),
        (["query1", "query7"], ["1", "7"]),
        (["2b", "11a"], ["2b", "11a"]),
    ],
)
def test_key_normalization(tmp_path, keys, expected):
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = _write_parquet(tmp_path)
    qjson = tmp_path / "q.json"
    qjson.write_text(_json.dumps({k: "SELECT 1 FROM events" for k in keys}))
    spec = register_workload_from_json("kn_" + "_".join(keys), qjson, parquet)
    assert sorted(spec.all_query_ids) == sorted(expected)


def test_key_collision_raises(tmp_path):
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = _write_parquet(tmp_path)
    qjson = tmp_path / "q.json"
    qjson.write_text(_json.dumps({"q1": "SELECT 1 FROM events", "1": "SELECT 2 FROM events"}))
    with pytest.raises(ValueError, match="collision"):
        register_workload_from_json("collide", qjson, parquet)


def test_unparseable_key_raises(tmp_path):
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = _write_parquet(tmp_path)
    qjson = tmp_path / "q.json"
    qjson.write_text(_json.dumps({"total_revenue": "SELECT 1 FROM events"}))
    with pytest.raises(ValueError, match="Cannot parse a query id"):
        register_workload_from_json("bad", qjson, parquet)


TPCH_SF1 = "/mnt/labstore/learneddb/synno_data/workloads/tpch/tpch_parquet/sf1"


def test_templated_byo_q1_q7(tmp_path):
    """A TEMPLATED queries.json (Q1's embedded [DELTA], Q7's correlated nations) must
    register with valid, data-inferred instantiations — no hand-written generator."""
    import json as _json
    import os

    if not os.path.isdir(TPCH_SF1):
        pytest.skip("TPC-H SF1 parquet not present")
    pytest.importorskip("duckdb")

    from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h
    from synnodb.workloads.byo_workload import register_workload_from_json

    qjson = tmp_path / "queries.json"
    qjson.write_text(_json.dumps({"1": tpc_h["Q1"], "7": tpc_h["Q7"]}))

    spec = register_workload_from_json(
        "tpch_byo", qjson, TPCH_SF1.rsplit("/sf1", 1)[0], scale_factors=(1,)
    )
    assert spec.all_query_ids == ("1", "7")

    # the generator yields instantiated SQL with placeholders filled + a params dict
    gen = spec.query_gen_factory(None)
    import random as _random

    name, sql, params = gen("Q1", _random.Random(1))
    assert "[DELTA]" not in sql and "DELTA" in params
    name7, sql7, params7 = gen("Q7", _random.Random(1))
    assert "[NATION1]" not in sql7 and "NATION1" in params7 and "NATION2" in params7


def test_preflight_catches_nonstring_placeholder():
    """Registration-time self-check rejects a non-string placeholder value (which would
    otherwise fail deep in a run with 'failed to parse <name>')."""
    from synnodb.workloads.byo_workload import _preflight_workload

    with pytest.raises(ValueError, match="failed to parse|must be strings"):
        _preflight_workload(
            "w",
            {"Q1": "select * from lineitem where x = [DELTA]"},
            ["1"],
            ["lineitem"],
            {"1": [{"DELTA": 2520}]},  # int, not str -> would be quoted in args but parsed unquoted
        )


def test_preflight_passes_for_string_placeholders():
    from synnodb.workloads.byo_workload import _preflight_workload

    # strings (the correct convention) pass
    _preflight_workload(
        "w",
        {"Q1": "select * from lineitem where x = [DELTA]"},
        ["1"],
        ["lineitem"],
        {"1": [{"DELTA": "2520"}]},
    )


def test_byo_debug_toggle(monkeypatch):
    import logging

    from synnodb.workloads.param_infer import configure_byo_debug

    monkeypatch.setenv("SYNNODB_BYO_DEBUG", "1")
    assert configure_byo_debug() is True
    assert logging.getLogger("synnodb.workloads.param_infer").level == logging.DEBUG
    assert logging.getLogger("synnodb.workloads.byo_workload").level == logging.DEBUG
    monkeypatch.delenv("SYNNODB_BYO_DEBUG")
    assert configure_byo_debug() is False


def test_resolve_workload(myshop):
    """The CLI/entry-point resolution primitive: enum for builtins, WorkloadId for BYO."""
    from synnodb.workloads.workload_provider import WorkloadId
    from synnodb.workloads.workload_provider_olap import OLAPWorkload
    from synnodb.workloads.workload_spec import resolve_workload

    # builtins keep their enum identity (preserves cache keys)
    assert resolve_workload("tpch") is OLAPWorkload.TPCH
    assert resolve_workload("ceb") is OLAPWorkload.CEB
    # a registered BYO workload resolves to a WorkloadId (no enum member needed)
    r = resolve_workload("myshop")
    assert isinstance(r, WorkloadId) and r.value == "myshop"
    with pytest.raises(ValueError, match="Unknown workload"):
        resolve_workload("not-registered")
