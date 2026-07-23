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
TABLES = [
    "customer",
    "lineitem",
    "nation",
    "orders",
    "part",
    "partsupp",
    "region",
    "supplier",
]


def _fixture_present() -> bool:
    # exists() raises PermissionError (instead of returning False) when a parent
    # directory is unreadable, e.g. another user's home on a shared machine.
    try:
        return (Q1Q6BYO / "db").exists() and SF1.exists()
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _fixture_present(),
    reason="requires the compiled q1q6byo engine and SF1 parquet",
)


def _provider():
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import (
        OLAPWorkloadProvider,
    )

    return OLAPWorkloadProvider(
        benchmark="tpch",
        base_parquet_dir=SF_PARENT,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1", "6"],
    )


def _connect(engines_dir):
    import synnodb
    from synnodb.router import RouterMode, RouterPolicy

    con = synnodb.connect(
        ":memory:",
        engines=str(engines_dir),
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
    )
    for t in TABLES:
        con.duckdb.execute(
            f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{SF1}/{t}.parquet')"
        )
    return con


def test_publish_discover_route_q1_q6():
    from synnodb.workloads.engine_publish import publish_from_provider

    from receipt_helpers import passing_receipt

    provider = _provider()
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        dest = publish_from_provider(
            Q1Q6BYO,
            provider,
            ["1", "6"],
            receipt=passing_receipt(Q1Q6BYO, ["1", "6"], scale_factors=(1.0,)),
            parquet_dir=SF1,
            scale_factor=1.0,
            source_run_id="test",
            engines_dir=str(engines),
        )
        assert dest is not None and (dest / "manifest.json").exists()

        con = _connect(engines)
        con.refresh_engines()
        assert con.router_stats()["registry"]["templates"] == 2

        from synnodb.router.adapt import results_equal

        gen = provider._get_query_gen_fn()
        rnd = random.Random(1)
        for qid in ["1", "6"]:
            _, sql, _ = gen(query_name=f"Q{qid}", rnd=rnd)
            assert con.why(sql)["decision"] == "would-route", (
                f"Q{qid} should match a template"
            )
            # The served result is exactly DuckDB's - bit-for-bit when the engine routes, or via
            # the exact cross-check's fallback when it cannot reproduce DuckDB.
            got = con.execute(sql).to_arrow_table()
            assert results_equal(
                got, con.duckdb.execute(sql).to_arrow_table(), ordered=True
            )

        session = con.router_stats()["session"]
        assert session["routed"] + session["fell_back"] == 2


def test_near_miss_constant_falls_back():
    from synnodb.workloads.engine_publish import publish_from_provider
    from synnodb.workloads.query_params import substitute

    from receipt_helpers import passing_receipt

    provider = _provider()
    with tempfile.TemporaryDirectory() as tmp:
        engines = Path(tmp) / "engines"
        publish_from_provider(
            Q1Q6BYO,
            provider,
            ["1"],
            receipt=passing_receipt(Q1Q6BYO, ["1"], scale_factors=(1.0,)),
            parquet_dir=SF1,
            scale_factor=1.0,
            engines_dir=str(engines),
        )
        con = _connect(engines)
        con.refresh_engines()
        q1 = provider.sql_dict["Q1"]
        good = substitute(q1, {"DELTA": "90"})
        near_miss = good.replace(
            "1998-12-01", "1997-01-01"
        )  # a constant the engine was not built for
        assert con.why(good)["decision"] == "would-route"
        assert con.why(near_miss)["decision"] == "would-fall-back"
