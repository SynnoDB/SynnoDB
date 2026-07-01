"""One-shot script to write tpch_byo.ipynb next to this file."""

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
cells.append(
    md("""
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
""")
)

# ── Config ──────────────────────────────────────────────────────────────────
cells.append(md("## Setup\n\nAdjust the paths below if your data lives elsewhere."))

cells.append(
    code("""
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
""")
)

# ── Step 1 header ───────────────────────────────────────────────────────────
cells.append(
    md("""
---
## Step 1 - DuckDB Baseline

We run **Q1-Q5 on vanilla DuckDB** at SF20: 10 instantiations per query
(different placeholder values, drawn from the actual data), total wall-clock time recorded.
These exact SQL strings will be reused in Step 3 so the comparison is apples-to-apples.
""")
)

# ── Workload registration + param generation ─────────────────────────────────
cells.append(
    md("""
### Register the BYO workload

The workload is described by a single self-describing JSON file. Each entry carries its SQL
template **and** a typed **spec** for each `[PLACEHOLDER]` slot - declaring its value *space*,
which is sampled at run time. A scalar placeholder is an `int`/`float`/`date`/`categorical`
spec; correlated or distinct placeholders share a `param_groups` spec:

```json
"6": {
  "sql": "... l_discount between [DISCOUNT] - 0.01 ... l_quantity < [QUANTITY] ...",
  "params": {
    "DATE":     { "type": "date",  "min": "1993-01-01", "max": "1997-01-01" },
    "DISCOUNT": { "type": "float", "min": 0.02, "max": 0.09, "step": 0.01 },
    "QUANTITY": { "type": "int",   "min": 24, "max": 25 }
  }
},
"7": {
  "sql": "... n1.n_name = '[NATION1]' ... n2.n_name = '[NATION2]' ...",
  "param_groups": [
    { "type": "sample", "placeholders": ["NATION1", "NATION2"], "domain": ["GERMANY", "CHINA", ...], "distinct": true }
  ]
}
```

`register_workload_from_json` reads it and derives the schema from the parquet via DuckDB.
Each placeholder's spec is sampled with the run's seeded RNG (a range → a uniform draw, a
`categorical` → a choice, a group → one joint row), so correlated placeholders stay aligned.
The typed spec is exactly what a BI dashboard renders as a slider (`int`/`float`), a dropdown
(`categorical`), or a date-picker (`date`).
""")
)

cells.append(
    code("""
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
""")
)

cells.append(
    md("""
Here is what the queries look like - SQL templates with `[PLACEHOLDER]` slots, plus the typed
specs that define each slot's value space:
""")
)

cells.append(
    code("""
queries = json.loads(QUERIES_JSON.read_text())
for qid, entry in queries.items():
    print(f"=== Q{qid} ===")
    print(entry["sql"][:240], "...")
    print("params      :", entry.get("params", {}))
    print("param_groups:", entry.get("param_groups", []))
    print()
""")
)

cells.append(
    md("""
### Draw parameter instantiations

`query_gen_factory` fills the templates by sampling each placeholder's spec. We draw with a
fixed seed so the instantiations are **identical** for the DuckDB and SynnoDB runs.
""")
)

cells.append(
    code("""
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
""")
)

# ── DuckDB baseline run ──────────────────────────────────────────────────────
cells.append(md("### Run on DuckDB"))

cells.append(
    code("""
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
""")
)

# ── Step 2 ───────────────────────────────────────────────────────────────────
cells.append(
    md("""
---
## Step 2 - Generate the SynnoDB Engine

You hand SynnoDB the same `queries.json` and a scale factor. It:

1. **Creates a storage plan** - decides how each query's columns are laid out in memory.
2. **Implements the engine** - writes single-threaded C++, compiles it, validates every output
   against DuckDB, then **auto-publishes** the binary into `ENGINES_DIR`.

This is a one-time cost. Once published the engine is discovered automatically across sessions.
""")
)

cells.append(md("### Storage plan"))

cells.append(
    code("""
from synnodb import SynnoDB

db   = SynnoDB(workload="tpch_byo", model=MODEL, db_storage="in_memory", queries="1-5")
plan = db.createStoragePlan()

print("Run :", plan.run_id)
print()
print(plan.text[:600], "...")
""")
)

cells.append(
    md(
        "### Base implementation\n\n"
        "We feed the plan **content** straight in via `storage_plan=plan.text`, so this step needs\n"
        "no W&B. If you instead chain off a logged storage-plan run, pass its run id with\n"
        "`db.createBaseImpl(storage_plan_wandb_id=plan.run_id)`. Provide exactly one of the two."
    )
)

cells.append(
    code("""
impl = db.createBaseImpl(storage_plan=plan.text)  # pass the plan content directly (W&B-free)

print("Workspace :", impl.workspace)
print("Files     :", sorted(impl.files))
print()
print(f"Engine published to: {ENGINES_DIR}")
""")
)

# ── Step 3 ───────────────────────────────────────────────────────────────────
cells.append(
    md("""
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
""")
)

cells.append(md("### Open the drop-in connection"))

cells.append(
    code("""
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
""")
)

cells.append(
    md("""
### Run the same queries with the same parameter values

We iterate over `instantiations` - the exact SQL strings from Step 1.
""")
)

cells.append(
    code("""
synno_times = {}
for qid, insts in instantiations.items():
    times = []
    for _, sql, _ in insts:
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        times.append((time.perf_counter() - t0) * 1_000)
    synno_times[qid] = times
""")
)

cells.append(md("### Speedup"))

cells.append(
    code("""
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
""")
)

cells.append(
    md("""
### Correctness guarantee

Every routed result was compared against DuckDB (`cross_check_rate=1.0`).
The mismatch count must be 0.
""")
)

cells.append(
    code("""
stats = con.router_stats()["session"]
print(f"Routed:         {stats['routed']}")
print(f"Cross-checked:  {stats['cross_checked']}")
print(f"Mismatches:     {stats['cross_check_mismatch']}")
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
print("\\nAll results match DuckDB exactly.")
""")
)

cells.append(md("### Q1 result (with timing footer)"))

cells.append(
    code("""
_, q1_sql, _ = instantiations["1"][0]
con.execute(q1_sql)
con.show()
con.close()
""")
)

# ── Next steps ───────────────────────────────────────────────────────────────
cells.append(
    md("""
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
""")
)

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
