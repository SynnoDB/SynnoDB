"""One-shot script to write 01_quickstart.ipynb next to this file.

The quickstart is deliberately tiny: ONE query (Q5) at SF2, single-threaded. It exists to show
the whole SynnoDB loop - generate a bespoke engine, drop it in for DuckDB, watch it run faster -
in the fewest possible cells. Tutorials 02 and 03 add threads, more queries, and bigger scale.
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
# SynnoDB Quickstart: DuckDB to Bespoke in One Import

SynnoDB is a drop-in replacement for DuckDB. You hand it a `queries.json`, it writes and compiles
a bespoke C++ engine for those queries, validates every result against DuckDB, and then serves
them - falling back to DuckDB for everything else. **One import, no query rewrites.**

This is the 5-minute taste: **one query (Q5) at scale factor 2, single-threaded**. When you want
more, go to `02_explore_threads.py` (five queries, the `threads` knob) and `03_full_run.py`
(all 22 queries at SF50).

> **Prerequisites** - installation, TPC-H parquet, and the model endpoint are covered in
> [`docs/TUTORIAL_base_implementation.md`](../docs/TUTORIAL_base_implementation.md).
"""))

cells.append(md("## 1. Setup\n\nAdjust the paths if your data lives elsewhere."))
cells.append(code("""
import os, time, statistics
from pathlib import Path

DATA_ROOT   = Path(os.environ.get("SYNNO_DATA_DIR", "/mnt/labstore/learneddb/synno_data"))
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
MODEL       = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
SF          = 2
QUERY       = "5"          # Q5 has a large speedup, so the payoff is obvious
TABLES      = ["customer", "lineitem", "nation", "orders", "part",
               "partsupp", "region", "supplier"]

ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)
print("Parquet:", PARQUET_DIR, "| Engines:", ENGINES_DIR, "| Model:", MODEL)
"""))

cells.append(md("""
## 2. Register the workload

The workload is one self-describing JSON file: each query is a SQL template plus a typed spec for
each `[PLACEHOLDER]`. `register_workload_from_json` reads it and derives the schema from the parquet.
"""))
cells.append(code("""
import random
from synnodb.workloads.byo_workload import register_workload_from_json

QUERIES_JSON = Path("queries.json")   # lives next to this notebook

spec = register_workload_from_json(
    name="tpch_byo",
    queries_json=QUERIES_JSON,
    parquet_dir=PARQUET_DIR,
    scale_factors=(1, SF),            # small SFs let the framework validate cheaply
    schema_example_table="lineitem",
)

# Draw 5 seeded instantiations of Q5 (same values reused for DuckDB and SynnoDB below).
gen  = spec.query_gen_factory(None)
rng  = random.Random(42)
q5   = [gen(f"Q{QUERY}", rng) for _ in range(5)]   # each is (name, sql_with_values, params)
print("Registered", spec.name, "- timing Q%s with %d instantiations" % (QUERY, len(q5)))
"""))

cells.append(md("""
## 3. Generate the engine

Hand SynnoDB the same workload and scale factor. It creates a storage plan, implements and
compiles the engine, validates every output against DuckDB, and auto-publishes the binary into
`ENGINES_DIR`. This is a one-time cost (a few minutes for a single query at SF2).
"""))
cells.append(code("""
from synnodb import SynnoDB

db   = SynnoDB(workload="tpch_byo", model=MODEL, db_storage="in_memory", queries=QUERY)
plan = db.createStoragePlan()
impl = db.createBaseImpl(storage_plan=plan.text)   # pass the plan content directly (W&B-free)
print("Published engine to:", ENGINES_DIR, "| workspace:", impl.workspace)
"""))

cells.append(md("""
## 4. Drop in SynnoDB and compare

The only change from a DuckDB script is the import line plus two keyword arguments to `connect()`.
Everything else - views, `execute()`, `fetchall()` - is identical.
"""))
cells.append(code("""
sf_dir = PARQUET_DIR / f"sf{SF}"

# --- DuckDB baseline ---
import duckdb
duck = duckdb.connect(":memory:")
for t in TABLES:
    duck.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")
duck_ms = []
for _, sql, _ in q5:
    t0 = time.perf_counter(); duck.execute(sql).fetchall(); duck_ms.append((time.perf_counter()-t0)*1e3)
duck.close()

# --- SynnoDB drop-in (one import) ---
import synnodb as duckdb                                    # <- the only change
from synnodb.router import RouterMode, RouterPolicy
con = duckdb.connect(
    ":memory:",
    engines=str(ENGINES_DIR),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),   # verify every result vs DuckDB
)
for t in TABLES:
    con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")
con.refresh_engines()
synno_ms = []
for _, sql, _ in q5:
    t0 = time.perf_counter(); con.execute(sql).fetchall(); synno_ms.append((time.perf_counter()-t0)*1e3)

d, s = statistics.median(duck_ms), statistics.median(synno_ms)
print(f"Q{QUERY} median:  DuckDB {d:.1f} ms   SynnoDB {s:.1f} ms   ->  {d/s:.1f}x faster")

stats = con.router_stats()["session"]
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
print(f"Correctness: {stats['routed']} routed, {stats['cross_check_mismatch']} mismatches (every result matched DuckDB).")
con.close()
"""))

cells.append(md("""
## Where to go next

- **`02_explore_threads.py`** - five queries at SF20, and the `threads` knob (`0` = all cores,
  `8` = eight). Run it in tmux.
- **`03_full_run.py`** - the real thing: all 22 queries at SF50, designed and served at 64 threads.
"""))

nb = new_notebook(cells=cells)
out = HERE / "01_quickstart.ipynb"
nbformat.write(nb, out)
print("wrote", out)
