"""One-shot script to write tpch_byo.ipynb next to this file."""
import json
from pathlib import Path
import nbformat
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell

HERE = Path(__file__).parent


def md(src: str):
    return new_markdown_cell(src.strip())


def code(src: str):
    return new_code_cell(src.strip())


cells = []

# ── Hero ────────────────────────────────────────────────────────────────────
cells.append(md("""
# SynnoDB: From DuckDB to Bespoke in One Import

SynnoDB is a drop-in replacement for DuckDB that transparently accelerates your SQL queries
with auto-generated bespoke C++ engines - while falling back to DuckDB for everything else.
No schema changes. No query rewrites. One import.

This notebook walks through the full journey for **TPC-H Q1-Q5**:

1. **Baseline** - run Q1-Q5 on vanilla DuckDB at SF20, 10 parameter instantiations each.
2. **Generate** - point SynnoDB at a `queries.json` file and let it write the engine.
3. **Drop in** - replace one import, re-run the identical queries, compare the numbers.

> **Prerequisites** - see [`docs/TUTORIAL_base_implementation.md`](../docs/TUTORIAL_base_implementation.md)
> for installation, TPC-H data generation, and model endpoint setup.
"""))

# ── Config ──────────────────────────────────────────────────────────────────
cells.append(md("## Setup\n\nAdjust the paths below if your data lives elsewhere."))

cells.append(code("""
import os, json, time, statistics
from pathlib import Path

DATA_ROOT   = Path(os.environ.get("SYNNO_DATA_DIR",  "/mnt/labstore/learneddb/synno_data"))
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET",
                   DATA_ROOT / "workloads/tpch/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
MODEL       = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
SF          = 20
TABLES      = ["customer", "lineitem", "nation", "orders", "part",
               "partsupp", "region", "supplier"]

ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("SYNNO_ENGINES_DIR", str(ENGINES_DIR))
print("Parquet root :", PARQUET_DIR)
print("Engines dir  :", ENGINES_DIR)
print("Model        :", MODEL)
"""))

# ── Step 1 header ───────────────────────────────────────────────────────────
cells.append(md("""
---
## Step 1 - DuckDB Baseline

We run **Q1-Q5 on vanilla DuckDB** at SF20: 10 instantiations per query
(different placeholder values, drawn from the actual data), total wall-clock time recorded.
These exact SQL strings will be reused in Step 3 so the comparison is apples-to-apples.
"""))

# ── Workload registration + param generation ─────────────────────────────────
cells.append(md("""
### Register the BYO workload

The workload is described by a single self-describing JSON file. Each entry carries its SQL
template **and** the values for its `[PLACEHOLDER]` slots - one key per placeholder mapping
to the list of values it should take across the sweep:

```json
"7": {
  "sql": "... n1.n_name = '[NATION1]' ... n2.n_name = '[NATION2]' ...",
  "params": { "NATION1": ["GERMANY", "CHINA"], "NATION2": ["ROMANIA", "UNITED STATES"] }
}
```

`register_workload_from_json` reads it and derives the schema from the parquet via DuckDB.
Per-placeholder lists are index-zipped into instantiations (so correlated placeholders stay
aligned); a length-1 list broadcasts across the sweep. This is the shape a BI dashboard would
fill - a slider per placeholder predefining its values.
"""))

cells.append(code("""
import random
from synnodb.workloads.byo_workload import register_workload_from_json

QUERIES_JSON = Path("queries.json")   # lives next to this notebook

spec = register_workload_from_json(
    name="tpch_byo",
    queries_json=QUERIES_JSON,
    parquet_dir=PARQUET_DIR,
    scale_factors=(1, 2, SF),
    schema_example_table="lineitem",
)

print("Workload :", spec.name)
print("Tables   :", spec.tables)
print("Queries  :", spec.all_query_ids)
"""))

cells.append(md("""
Here is what the queries look like - SQL templates with `[PLACEHOLDER]` slots, plus the
values supplied for them:
"""))

cells.append(code("""
queries = json.loads(QUERIES_JSON.read_text())
for qid, entry in queries.items():
    print(f"=== Q{qid} ===")
    print(entry["sql"][:240], "...")
    print("params:", entry.get("params", {}))
    print()
"""))

cells.append(md("""
### Draw parameter instantiations

`query_gen_factory` fills the templates from the supplied value lists. We draw with a fixed
seed so the instantiations are **identical** for the DuckDB and SynnoDB runs.
"""))

cells.append(code("""
N_REPS = 10
rng    = random.Random(42)
gen    = spec.query_gen_factory(None)

# gen(query_name, rng) -> (query_name, sql_with_values, params_dict)
instantiations = {}
for qid in spec.all_query_ids:
    instantiations[qid] = [gen(f"Q{qid}", rng) for _ in range(N_REPS)]

print(f"Drew {N_REPS} instantiations per query.")
for qid, insts in instantiations.items():
    sample_params = [i[2] for i in insts[:2]]
    print(f"  Q{qid}: {sample_params} ...")
"""))

# ── DuckDB baseline run ──────────────────────────────────────────────────────
cells.append(md("### Run on DuckDB"))

cells.append(code("""
import duckdb

sf_dir = PARQUET_DIR / f"sf{SF}"

duck = duckdb.connect(":memory:")
for t in TABLES:
    duck.execute(
        f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')"
    )

baseline_times = {}
for qid, insts in instantiations.items():
    times = []
    for _, sql, _ in insts:
        t0 = time.perf_counter()
        duck.execute(sql).fetchall()
        times.append((time.perf_counter() - t0) * 1_000)
    baseline_times[qid] = times

duck.close()

total_duck = sum(sum(v) for v in baseline_times.values())
print(f"{'Query':<8} {'Total (ms)':>12} {'Median (ms)':>14}")
print("-" * 38)
for qid in spec.all_query_ids:
    t = baseline_times[qid]
    print(f"Q{qid:<7} {sum(t):>12.1f} {statistics.median(t):>14.1f}")
print("-" * 38)
print(f"{'TOTAL':<8} {total_duck:>12.1f}")
"""))

# ── Step 2 ───────────────────────────────────────────────────────────────────
cells.append(md("""
---
## Step 2 - Generate the SynnoDB Engine

You hand SynnoDB the same `queries.json` and a scale factor. It:

1. **Creates a storage plan** - decides how each query's columns are laid out in memory.
2. **Implements the engine** - writes single-threaded C++, compiles it, validates every output
   against DuckDB, then **auto-publishes** the binary into `ENGINES_DIR`.

This is a one-time cost. Once published the engine is discovered automatically across sessions.
"""))

cells.append(md("### Storage plan"))

cells.append(code("""
from synnodb import SynnoDB

db   = SynnoDB(workload="tpch_byo", model=MODEL, db_storage="in_memory", queries="1-5")
plan = db.createStoragePlan()

print("Run :", plan.run_id)
print()
print(plan.text[:600], "...")
"""))

cells.append(md("### Base implementation"))

cells.append(code("""
impl = db.createBaseImpl(storage_plan=plan)

print("Workspace :", impl.workspace)
print("Files     :", sorted(impl.files))
print()
print(f"Engine published to: {ENGINES_DIR}")
"""))

# ── Step 3 ───────────────────────────────────────────────────────────────────
cells.append(md("""
---
## Step 3 - Drop In SynnoDB

The only change is **one import line** and two extra keyword arguments to `connect()`:

```diff
- import duckdb
+ import synnodb as duckdb
+ from synnodb.router import RouterMode, RouterPolicy

  con = duckdb.connect(
      ":memory:",
+     engines=str(ENGINES_DIR),
+     policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
  )
```

Every other line - the view setup, the `execute()` calls, `fetchall()` - is identical.
"""))

cells.append(md("### Open the drop-in connection"))

cells.append(code("""
import synnodb as duckdb                          # <- only change
from synnodb.router import RouterMode, RouterPolicy

con = duckdb.connect(
    ":memory:",
    engines=str(ENGINES_DIR),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
)

sf_dir = PARQUET_DIR / f"sf{SF}"
for t in TABLES:
    con.duckdb.execute(
        f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')"
    )

con.refresh_engines()
n = con.router_stats()["registry"]["templates"]
print(f"Discovered {n} engine template(s) under {ENGINES_DIR}")
"""))

cells.append(md("""
### Run the same queries with the same parameter values

We iterate over `instantiations` - the exact SQL strings from Step 1.
"""))

cells.append(code("""
synno_times = {}
for qid, insts in instantiations.items():
    times = []
    for _, sql, _ in insts:
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        times.append((time.perf_counter() - t0) * 1_000)
    synno_times[qid] = times
"""))

cells.append(md("### Speedup"))

cells.append(code("""
total_synno = sum(sum(v) for v in synno_times.values())

print(f"{'Query':<8} {'DuckDB (ms)':>12} {'SynnoDB (ms)':>14} {'Speedup':>9}")
print("-" * 48)
for qid in spec.all_query_ids:
    d = sum(baseline_times[qid])
    s = sum(synno_times[qid])
    speedup = d / s if s > 0 else float("inf")
    print(f"Q{qid:<7} {d:>12.1f} {s:>14.1f} {speedup:>8.2f}x")
print("-" * 48)
overall = total_duck / total_synno if total_synno > 0 else float("inf")
print(f"{'TOTAL':<8} {total_duck:>12.1f} {total_synno:>14.1f} {overall:>8.2f}x")
"""))

cells.append(md("""
### Correctness guarantee

Every routed result was compared against DuckDB (`cross_check_rate=1.0`).
The mismatch count must be 0.
"""))

cells.append(code("""
stats = con.router_stats()["session"]
print(f"Routed:         {stats['routed']}")
print(f"Cross-checked:  {stats['cross_checked']}")
print(f"Mismatches:     {stats['cross_check_mismatch']}")
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
print("\\nAll results match DuckDB exactly.")
"""))

cells.append(md("### Q1 result (with timing footer)"))

cells.append(code("""
_, q1_sql, _ = instantiations["1"][0]
con.execute(q1_sql)
con.show()
con.close()
"""))

# ── Next steps ───────────────────────────────────────────────────────────────
cells.append(md("""
---
## Where to go next

The base implementation is single-threaded. The same `SynnoDB` object carries each engine further:

```python
opt   = db.runOptimLoop(base_impl=impl)           # single-threaded SIMD / cache optimization
multi = db.addMultiThreading(optimized=opt)        # parallel execution across cores
rep   = db.checkSfCorrectness(source=multi, target_sf=50)  # correctness at larger SF
```

CLI equivalents and step-by-step commentary are in
[`docs/TUTORIAL_base_implementation.md`](../docs/TUTORIAL_base_implementation.md).
"""))

# ── Assemble and write ───────────────────────────────────────────────────────
nb = new_notebook(cells=cells)
nb.metadata["kernelspec"] = {
    "display_name": "SynnoDB",
    "language": "python",
    "name": "synnodb",
}
nb.metadata["language_info"] = {"name": "python", "version": "3.11"}

out = HERE / "tpch_byo.ipynb"
with open(out, "w", encoding="utf-8") as f:
    nbformat.write(nb, f)

print(f"Written: {out}")
