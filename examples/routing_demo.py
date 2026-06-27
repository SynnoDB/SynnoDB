"""SynnoDB drop-in routing demo — Q1 and Q6 bespoke, queried the way a user would.

    .venv/bin/python examples/routing_demo.py

`synnodb.connect(...)` is a DuckDB-compatible connection that fronts a real DuckDB. A
query whose *shape* matches a registered bespoke engine routes to that engine (and is
cross-checked against DuckDB); everything else falls back to DuckDB. We register the Q1
and Q6 engines we generated in `q1q6byo` and then run ordinary inline SQL — the router
binds the inline literals to the engine's parameters by structurally matching the query
against the template, so it works even though the templates contain constants and Q6
repeats [DATE]/[DISCOUNT].
"""
import time
from pathlib import Path

import synnodb
from synnodb.router import (
    PlaceholderSpec,
    RouterMode,
    RouterPolicy,
    TemplateRegistry,
    register_engine,
)
from synnodb.router.process_engine import ProcessEngine

SF1 = "/mnt/labstore/learneddb/synno_data/workloads/tpch/tpch_parquet/sf1"
ENGINE_WS = str(Path(__file__).resolve().parent.parent / "q1q6byo")
TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]

# Templates use `?` to mark the runtime parameters (everything else is a constant the
# incoming query must match exactly).
Q1_T = (
    "select l_returnflag, l_linestatus, sum(l_quantity) as sum_qty, "
    "sum(l_extendedprice) as sum_base_price, sum(l_extendedprice*(1-l_discount)) as sum_disc_price, "
    "sum(l_extendedprice*(1-l_discount)*(1+l_tax)) as sum_charge, avg(l_quantity) as avg_qty, "
    "avg(l_extendedprice) as avg_price, avg(l_discount) as avg_disc, count(*) as count_order "
    "from lineitem where l_shipdate <= date '1998-12-01' - interval (?) day "
    "group by l_returnflag, l_linestatus order by l_returnflag, l_linestatus"
)
Q6_T = (
    "select sum(l_extendedprice * l_discount) as revenue from lineitem "
    "where l_shipdate >= ? and l_shipdate < ? + interval '1' year "
    "and l_discount between ? - 0.01 and ? + 0.01 and l_quantity < ?"
)


def _schema(con, sql):
    return [(d[0], str(d[1])) for d in con.duckdb.execute(sql + " limit 0").description]


def main() -> None:
    con = synnodb.connect(
        ":memory:",
        policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
        registry=TemplateRegistry(),
    )
    for t in TABLES:
        con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{SF1}/{t}.parquet')")

    # Two engine handles (same generated binary, different output schema per query).
    q1_concrete = Q1_T.replace("(?)", "(90)")
    q6_concrete = ("select sum(l_extendedprice * l_discount) as revenue from lineitem "
                   "where l_shipdate >= date '1994-01-01' and l_shipdate < date '1994-01-01' + interval '1' year "
                   "and l_discount between 0.05 - 0.01 and 0.05 + 0.01 and l_quantity < 24")
    eng1 = ProcessEngine("q1", ENGINE_WS, SF1, output_schema=_schema(con, q1_concrete), timeout_s=600)
    eng6 = ProcessEngine("q6", ENGINE_WS, SF1, output_schema=_schema(con, q6_concrete), timeout_s=600)
    register_engine(con, template_sql=Q1_T, engine=eng1, query_id="1",
                    placeholders=[PlaceholderSpec("DELTA", "INTEGER")])
    register_engine(con, template_sql=Q6_T, engine=eng6, query_id="6",
                    placeholders=[PlaceholderSpec("DATE", "DATE"), PlaceholderSpec("DATE", "DATE"),
                                  PlaceholderSpec("DISCOUNT", "DECIMAL(15,2)"),
                                  PlaceholderSpec("DISCOUNT", "DECIMAL(15,2)"),
                                  PlaceholderSpec("QUANTITY", "DECIMAL(15,2)")])
    print("registered bespoke Q1 + Q6\n")

    # Five queries written INLINE, exactly as a user would type them (no bound params).
    queries = [
        ("Q1 inline (DELTA=90)", q1_concrete),
        ("Q1 inline (DELTA=120, same shape)", Q1_T.replace("(?)", "(120)")),
        ("Q6 inline (date/discount/qty)", q6_concrete),
        ("Q1 near-miss: wrong constant date 1997-01-01 -> must FALL BACK",
         q1_concrete.replace("1998-12-01", "1997-01-01")),
        ("count(*) lineitem (not registered)", "select count(*) from lineitem"),
    ]

    print(f"{'query':<54}  {'served by':<16}  result")
    print("-" * 100)
    for label, sql in queries:
        t0 = time.time()
        dec = con.router.route(sql, None, con)  # params=None -> bind from inline literals
        wall = (time.time() - t0) * 1000
        if dec.routed:
            served = "SynnoDB bespoke"
            match = "match=OK" if dec.trace.results_match else "match=MISMATCH"
            row0 = dec.result.fetchall()[:1]
            extra = f"{match} bespoke={dec.trace.bespoke_ms:.1f}ms first={row0}"
        else:
            served = "DuckDB"
            extra = f"{wall:.1f}ms ({dec.trace.reason})"
        print(f"{label:<54}  {served:<16}  {extra}")

    eng1.close()
    eng6.close()


if __name__ == "__main__":
    main()
