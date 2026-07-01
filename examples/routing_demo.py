"""SynnoDB drop-in routing demo: the closed loop, end to end.

    .venv/bin/python examples/routing_demo.py

The idea: swap ``import duckdb`` for ``import synnodb as duckdb`` and change nothing else.
Queries run on DuckDB until a bespoke engine for that query shape exists; the moment one is
published, the same code auto-routes to it (cross-checked against DuckDB), with no extra call.

This demo plays both sides: it publishes the engine generated in ``q1q6byo`` for TPC-H Q1/Q6
(what a finished ``createBaseImpl`` does), then runs ordinary inline SQL through a normal
connection and shows which queries the router serves bespoke and which fall back.
"""

import tempfile
from pathlib import Path

import synnodb as duckdb
from synnodb.router import RouterMode, RouterPolicy
from synnodb.workloads.engine_publish import publish_from_provider
from synnodb.workloads.param_infer import substitute
from synnodb.workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider
from synnodb.utils.utils import DBStorage

SF_PARENT = Path("/mnt/labstore/learneddb/synno_data/workloads/tpch/tpch_parquet")
SF1 = SF_PARENT / "sf1"
Q1Q6BYO = Path(__file__).resolve().parent.parent / "q1q6byo"
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


def main() -> None:
    provider = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=SF_PARENT,
        db_storage=DBStorage.IN_MEMORY,
        bespoke_ssd_storage_dir=None,
        query_ids=["1", "6"],
    )

    with tempfile.TemporaryDirectory() as tmp:
        engines_dir = Path(tmp) / "engines"

        # A normal connection, pointed at an (empty) engines directory.
        con = duckdb.connect(
            ":memory:",
            engines=str(engines_dir),
            policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        )
        # Load the data the way a drop-in user would: through the connection's DuckDB.
        for t in TABLES:
            con.duckdb.execute(
                f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{SF1}/{t}.parquet')"
            )

        q1 = substitute(provider.sql_dict["Q1"], {"DELTA": "90"})
        print("Before publishing an engine:")
        print(f"  Q1 -> {con.why(q1)['decision']} ({con.why(q1)['reason']})\n")

        # A base implementation finishes: publish it for the router to discover.
        publish_from_provider(
            Q1Q6BYO,
            provider,
            ["1", "6"],
            parquet_dir=SF1,
            scale_factor=1.0,
            engines_dir=str(engines_dir),
        )
        con.refresh_engines()
        print(
            f"Published q1q6byo; registered {con.router_stats()['registry']['templates']} templates.\n"
        )

        queries = [
            ("Q1 (DELTA=90)", substitute(provider.sql_dict["Q1"], {"DELTA": "90"})),
            (
                "Q1 (DELTA=120, same shape)",
                substitute(provider.sql_dict["Q1"], {"DELTA": "120"}),
            ),
            (
                "Q6",
                substitute(
                    provider.sql_dict["Q6"],
                    {"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"},
                ),
            ),
            (
                "Q1 near-miss (other constant date) -> falls back",
                substitute(provider.sql_dict["Q1"], {"DELTA": "90"}).replace(
                    "1998-12-01", "1997-01-01"
                ),
            ),
            (
                "count(*) lineitem (no engine) -> falls back",
                "select count(*) from lineitem",
            ),
        ]

        print(f"{'query':<48}  served by")
        print("-" * 70)
        for label, sql in queries:
            decision = con.why(sql)["decision"]
            con.execute(sql).fetchall()
            served = "SynnoDB bespoke" if decision == "would-route" else "DuckDB"
            print(f"{label:<48}  {served}")

        print("\nsession:", con.router_stats()["session"])


if __name__ == "__main__":
    main()
