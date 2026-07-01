"""One-shot script to write 01_quickstart.ipynb next to this file.

The quickstart tells the SynnoDB story in the fewest cells: DuckDB already runs the whole TPC-H
suite beautifully; SynnoDB is an *accelerator* that sits on top and makes the ONE query you care
about (here Q5) dramatically faster - on the same DuckDB database, with everything else still
served by DuckDB. It is not a replacement for DuckDB.
"""
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

HERE = Path(__file__).parent


def md(src: str):
    return new_markdown_cell(src.strip())


def code(src: str):
    return new_code_cell(src.strip())


cells = []

cells.append(md("""
# SynnoDB Quickstart: Accelerate One DuckDB Query

[DuckDB](https://duckdb.org) is wonderful. You point it at Parquet and it runs the entire TPC-H
suite - every query, no schema setup, no tuning. For most analytics, you never need anything else.

**SynnoDB does not replace DuckDB - it accelerates it.** When one particular query runs on a hot
path and you want it several times faster, SynnoDB generates a bespoke compiled C++ engine for
*just that query* and serves it transparently. Every other query still runs on DuckDB, exactly as
before. You keep DuckDB; you add speed where it matters.

This is the 5-minute taste: **DuckDB runs Q5 at scale factor 2, then we accelerate Q5 on the same
database.** For more, see `02_explore_threads.py` (five queries, the `threads` knob) and
`03_full_run.py` (all 22 queries at SF50).

> **Prerequisites** - installation, TPC-H parquet, and the model endpoint are covered in
> [`docs/TUTORIAL_base_implementation.md`](../docs/TUTORIAL_base_implementation.md).
"""))

cells.append(md("## Setup\n\nAdjust the paths if your data lives elsewhere."))
cells.append(code("""
import os, time, statistics
from pathlib import Path

DATA_ROOT   = Path(os.environ.get("SYNNO_DATA_DIR", "/mnt/labstore/learneddb/synno_data"))
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
MODEL       = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
SF          = 2
QUERY       = "5"          # Q5 is our hot query - a large win, so the payoff is obvious
TABLES      = ["customer", "lineitem", "nation", "orders", "part",
               "partsupp", "region", "supplier"]

ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)
print("Parquet:", PARQUET_DIR, "| Engines:", ENGINES_DIR, "| Model:", MODEL)
"""))

cells.append(md("""
## 1. DuckDB already does it all

Load the TPC-H tables straight from Parquet and run Q5. No schema, no load step - DuckDB reads the
files in place and runs the full SQL surface. This is the baseline we want to keep.

We register the workload only to draw realistic `[PLACEHOLDER]` values from a `queries.json`; the
SQL we run is plain TPC-H.
"""))
cells.append(code("""
import random, duckdb
from synnodb.workloads.byo_workload import register_workload_from_json

QUERIES_JSON = Path("queries.json")   # SQL templates + typed placeholder specs, next to this notebook

spec = register_workload_from_json(
    name="tpch_byo",
    queries_json=QUERIES_JSON,
    parquet_dir=PARQUET_DIR,
    scale_factors=(1, SF),
    schema_example_table="lineitem",
)

# Draw a few seeded instantiations of Q5 (same values reused everywhere below).
gen = spec.query_gen_factory(None)
rng = random.Random(42)
q5  = [gen(f"Q{QUERY}", rng) for _ in range(5)]   # each is (name, sql_with_values, params)

sf_dir = PARQUET_DIR / f"sf{SF}"
duck = duckdb.connect(":memory:")
for t in TABLES:
    duck.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")

duck_ms = []
for _, sql, _ in q5:
    t0 = time.perf_counter(); duck.execute(sql).fetchall(); duck_ms.append((time.perf_counter()-t0)*1e3)
print(f"DuckDB runs Q{QUERY} in {statistics.median(duck_ms):.1f} ms (median).")
print("DuckDB also runs the other 21 TPC-H queries here, untouched - it is a superb general engine.")
duck.close()
"""))

cells.append(md("""
## 2. But you want Q5 much faster

Say Q5 sits behind a dashboard that hits it thousands of times a day, and you want it several times
faster - *without* giving up DuckDB for everything else. That is exactly SynnoDB's job: hand it the
query and a scale factor, and it writes, compiles, and **validates against DuckDB** a bespoke engine
for Q5, then publishes it into `ENGINES_DIR`. One-time cost, a few minutes at SF2.
"""))
cells.append(code("""
from synnodb import SynnoDB

db   = SynnoDB(workload="tpch_byo", model=MODEL, db_storage="in_memory", queries=QUERY)
plan = db.createStoragePlan()
impl = db.createBaseImpl(storage_plan=plan.text)   # pass the plan content directly (W&B-free)
print("Accelerator for Q%s published to %s" % (QUERY, ENGINES_DIR))
"""))

cells.append(md("""
## 3. Same DuckDB database - only Q5 is accelerated

`synnodb.connect(...)` gives you a **real DuckDB connection** (`con.duckdb`) with an acceleration
layer on top. Same tables, same SQL. When you run Q5, SynnoDB routes it to the compiled engine;
when you run anything else, it falls straight through to DuckDB. Note: we import `synnodb` under its
own name - we are not shadowing `duckdb`.
"""))
cells.append(code("""
import synnodb
from synnodb.router import RouterMode, RouterPolicy

con = synnodb.connect(
    ":memory:",
    engines=str(ENGINES_DIR),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),  # verify every routed result vs DuckDB
)
for t in TABLES:
    con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")
con.refresh_engines()

# Same database, same Q5 SQL: DuckDB path (con.duckdb) vs the accelerated path (con.execute).
base_ms, acc_ms = [], []
for _, sql, _ in q5:
    t0 = time.perf_counter(); con.duckdb.execute(sql).fetchall(); base_ms.append((time.perf_counter()-t0)*1e3)
    t0 = time.perf_counter(); con.execute(sql).fetchall();        acc_ms.append((time.perf_counter()-t0)*1e3)

b, a = statistics.median(base_ms), statistics.median(acc_ms)
print(f"Q{QUERY} on this database:  DuckDB {b:.1f} ms   ->   SynnoDB {a:.1f} ms   ({b/a:.1f}x faster)")
"""))

cells.append(md("""
### Everything else is still DuckDB

Run a *different* query on the very same connection - there is no accelerator for it, so SynnoDB
serves it with DuckDB, transparently. Nothing about your DuckDB workflow changes.
"""))
cells.append(code("""
_, q1_sql, _ = spec.query_gen_factory(None)("Q1", random.Random(1))
con.execute(q1_sql).fetchall()          # no Q1 engine -> handled by DuckDB, correct as always

stats = con.router_stats()["session"]
print(f"Routed to accelerator: {stats['routed']}   Cross-checked vs DuckDB: {stats['cross_checked']}   "
      f"Mismatches: {stats['cross_check_mismatch']}")
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
print("Every accelerated Q%s result matched DuckDB exactly; Q1 (and the rest) stayed on DuckDB." % QUERY)
con.close()
"""))

cells.append(md("""
## Where to go next

- **`02_explore_threads.py`** - accelerate five queries at SF20, and meet the `threads` knob
  (`0` = all cores, `8` = eight). Run it in tmux.
- **`03_full_run.py`** - accelerate all 22 queries at SF50, served on 64 threads.

DuckDB stays your engine for everything; SynnoDB just adds a fast lane for the queries you choose.
"""))

nb = new_notebook(cells=cells)
out = HERE / "01_quickstart.ipynb"
nbformat.write(nb, out)
print("wrote", out)
