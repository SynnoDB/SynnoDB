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

    from synnodb.workloads.query_params import configure_byo_debug

    monkeypatch.setenv("SYNNODB_BYO_DEBUG", "1")
    assert configure_byo_debug() is True
    assert logging.getLogger("synnodb.workloads.query_params").level == logging.DEBUG
    assert logging.getLogger("synnodb.workloads.byo_workload").level == logging.DEBUG
    monkeypatch.delenv("SYNNODB_BYO_DEBUG")
    assert configure_byo_debug() is False


def _orders_parquet(tmp_path):
    """A small parquet workload with a few typed columns for the param tests."""
    pa = pytest.importorskip("pyarrow")
    import datetime

    import pyarrow.parquet as pq

    sf1 = tmp_path / "data" / "sf1"
    sf1.mkdir(parents=True)
    pq.write_table(
        pa.table({
            "o_key": [1, 2, 3, 4, 5],
            "o_orderdate": [datetime.date(y, 6, 1) for y in (1993, 1994, 1995, 1996, 1995)],
            "o_seg": ["BUILDING", "AUTOMOBILE", "BUILDING", "MACHINERY", "AUTOMOBILE"],
            "o_amt": [25.0, 11.0, 40.0, 7.0, 33.0],
        }),
        sf1 / "orders.parquet",
    )
    return tmp_path / "data"


def _register(tmp_path, queries, name="byo"):
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_json

    parquet = _orders_parquet(tmp_path)
    qjson = tmp_path / "queries.json"
    qjson.write_text(_json.dumps(queries))
    return register_workload_from_json(name, qjson, parquet)


def test_templated_inline_params(tmp_path):
    """A templated query carries its values inline; an int is coerced to a string."""
    spec = _register(tmp_path, {
        "1": {"sql": "SELECT * FROM orders WHERE o_amt > [MINAMT]",
              "params": {"MINAMT": [10, 20, 30]}},
    })
    gen = spec.query_gen_factory(None)
    import random as _random
    name, sql, params = gen("Q1", _random.Random(1))
    assert "[MINAMT]" not in sql
    assert params["MINAMT"] in {"10", "20", "30"}


def test_templated_correlated_zip(tmp_path):
    """Per-placeholder lists are index-zipped, so a correlated pair stays aligned."""
    spec = _register(tmp_path, {
        "7": {"sql": "SELECT * FROM orders WHERE o_seg = '[A]' OR o_seg = '[B]'",
              "params": {"A": ["BUILDING", "MACHINERY"], "B": ["MACHINERY", "BUILDING"]}},
    })
    ph = spec.placeholders_factory(None)
    first = ph("Q7")
    assert (first["A"], first["B"]) == ("BUILDING", "MACHINERY")  # index 0 stays paired


def test_templated_length_one_broadcast(tmp_path):
    spec = _register(tmp_path, {
        "1": {"sql": "SELECT * FROM orders WHERE o_seg = '[SEG]' AND o_amt > [MIN]",
              "params": {"SEG": ["BUILDING"], "MIN": [5, 15]}},
    })
    # broadcast SEG across both instantiations of MIN
    gen = spec.query_gen_factory(None)
    seen = {gen("Q1")[2]["SEG"] for _ in range(5)}
    assert seen == {"BUILDING"}


def test_templated_in_list_roundtrips_unquoted(tmp_path):
    from synnodb.workloads.workload_provider import format_args_element

    spec = _register(tmp_path, {
        "9": {"sql": "SELECT * FROM orders WHERE o_seg IN [SEGS]",
              "params": {"SEGS": [["BUILDING", "AUTOMOBILE"]]}},
    })
    ph = spec.placeholders_factory(None)
    val = ph("Q9")["SEGS"]
    assert val == "('BUILDING', 'AUTOMOBILE')"
    args = format_args_element("9", ph("Q9"))
    assert f" {val}" in args and f'"{val}"' not in args  # leading '(' -> left unquoted


def test_missing_params_raises(tmp_path):
    with pytest.raises(ValueError, match="have no parameter values"):
        _register(tmp_path, {"1": {"sql": "SELECT * FROM orders WHERE o_amt > [MINAMT]"}})


def test_plain_string_entry_is_static(tmp_path):
    spec = _register(tmp_path, {"1": "SELECT count(*) FROM orders"})
    _, sql, params = spec.query_gen_factory(None)("Q1")
    assert params == {} and "count(*)" in sql


def test_plain_string_entry_with_placeholder_raises(tmp_path):
    with pytest.raises(ValueError, match="have no parameter values"):
        _register(tmp_path, {"1": "SELECT * FROM orders WHERE o_amt > [MINAMT]"})


def test_mixed_templated_and_static(tmp_path):
    spec = _register(tmp_path, {
        "1": {"sql": "SELECT * FROM orders WHERE o_amt > [MINAMT]",
              "params": {"MINAMT": ["10"]}},
        "2": "SELECT count(*) FROM orders",
    })
    _, _, p1 = spec.query_gen_factory(None)("Q1")
    _, _, p2 = spec.query_gen_factory(None)("Q2")
    assert p1 == {"MINAMT": "10"} and p2 == {}


def test_inconsistent_param_lengths_raise(tmp_path):
    with pytest.raises(ValueError, match="inconsistent number of values"):
        _register(tmp_path, {
            "1": {"sql": "SELECT * FROM orders WHERE o_seg='[A]' AND o_amt>[B]",
                  "params": {"A": ["BUILDING", "MACHINERY"], "B": [1, 2, 3]}},
        })


def test_dir_sidecar_params(tmp_path):
    """register_workload_from_dir reads SQL files + a sidecar params.json."""
    import json as _json

    from synnodb.workloads.byo_workload import register_workload_from_dir

    parquet = _orders_parquet(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "1.sql").write_text("SELECT * FROM orders WHERE o_amt > [MINAMT]")
    (sql_dir / "params.json").write_text(_json.dumps({"1": {"MINAMT": ["10", "20"]}}))

    spec = register_workload_from_dir("dirp", sql_dir, parquet)
    gen = spec.query_gen_factory(None)
    assert {gen("Q1")[2]["MINAMT"] for _ in range(8)} <= {"10", "20"}


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
