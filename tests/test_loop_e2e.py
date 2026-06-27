"""Full closed-loop end-to-end against the real generated q1q6byo engine: publish from the
workload, auto-discover, and route real TPC-H Q1/Q6 instantiations, cross-checked vs DuckDB.

Skipped unless the compiled engine and the SF1 parquet are present (CI without data skips it).
"""
from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest

SF_PARENT = Path("/mnt/labstore/learneddb/synno_data/workloads/tpch/tpch_parquet")
SF1 = SF_PARENT / "sf1"
Q1Q6BYO = Path("/home/teckmann/SynnoDB/q1q6byo")
TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]

pytestmark = pytest.mark.skipif(
    not ((Q1Q6BYO / "db").exists() and SF1.exists()),
    reason="requires the compiled q1q6byo engine and SF1 parquet",
)


def _provider():
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

    return OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH, base_parquet_dir=SF_PARENT,
        db_storage=DBStorage.IN_MEMORY, bespoke_ssd_storage_dir=None, query_ids=["1", "6"],
    )


def _connect(engines_dir):
    import synnodb
    from synnodb.router import RouterMode, RouterPolicy

    con = synnodb.connect(
        ":memory:", engines=str(engines_dir),
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
    )
    for t in TABLES:
        con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{SF1}/{t}.parquet')")
    return con


def test_publish_discover_route_q1_q6():
    from synnodb.workloads.engine_publish import publish_from_provider

    provider = _provider()
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        dest = publish_from_provider(
            Q1Q6BYO, provider, ["1", "6"], parquet_dir=SF1,
            scale_factor=1.0, source_run_id="test", engines_dir=str(engines),
        )
        assert dest is not None and (dest / "manifest.json").exists()

        con = _connect(engines)
        con.refresh_engines()
        assert con.router_stats()["registry"]["templates"] == 2

        gen = provider._get_query_gen_fn()
        rnd = random.Random(1)
        for qid in ["1", "6"]:
            _, sql, _ = gen(query_name=f"Q{qid}", rnd=rnd)
            assert con.why(sql)["decision"] == "would-route", f"Q{qid} should route"
            con.execute(sql).fetchall()

        session = con.router_stats()["session"]
        assert session["routed"] == 2
        assert session["cross_check_mismatch"] == 0  # bespoke results match DuckDB


def test_near_miss_constant_falls_back():
    from synnodb.workloads.engine_publish import publish_from_provider
    from synnodb.workloads.param_infer import substitute

    provider = _provider()
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        publish_from_provider(Q1Q6BYO, provider, ["1"], parquet_dir=SF1,
                              scale_factor=1.0, engines_dir=str(engines))
        con = _connect(engines)
        con.refresh_engines()
        q1 = provider.sql_dict["Q1"]
        good = substitute(q1, {"DELTA": "90"})
        near_miss = good.replace("1998-12-01", "1997-01-01")  # a constant the engine was not built for
        assert con.why(good)["decision"] == "would-route"
        assert con.why(near_miss)["decision"] == "would-fall-back"
