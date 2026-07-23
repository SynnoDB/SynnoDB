"""One-shot script to write gen_tpch_demo.ipynb into the parent tutorials/ folder."""

from pathlib import Path
import nbformat
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell

TUTORIAL_DIR = Path(__file__).parent.parent  # tutorials/, alongside queries.json


def md(src: str, trailing_nl: bool = False):
    s = src.strip()
    return new_markdown_cell(s + "\n" if trailing_nl else s)


def code(src: str, trailing_nl: bool = False):
    s = src.strip()
    return new_code_cell(s + "\n" if trailing_nl else s)


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

It is **self-contained**: point it at a fresh (non-existent) data root and it generates the
TPC-H parquet itself before running anything - no manual download or external tooling.

### Prerequisites

**Install SynnoDB** - it lives on [PyPI](https://pypi.org/project/synnodb/), so a single
`pip install` pulls in every Python dependency. This demo *generates* an engine, so add the
`factory` extra:

```bash
pip install "synnodb[factory]"
```

(Working from a source checkout instead? `uv sync --extra factory` builds the same environment -
see the repo's Development section.)

**System libraries** - a C++ toolchain and the Arrow/Parquet dev headers the generated engine is
compiled against; `cloc` (optional) lets the factory report generated-code size. On Debian/Ubuntu:

```bash
sudo apt install -y build-essential cloc                # C++ compiler (+ optional cloc)

# Apache Arrow + Parquet development libraries
wget https://packages.apache.org/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt install -y -V ./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt update
sudo apt install -y libarrow-dev libparquet-dev parquet-tools
```

**Model access** - the factory calls `MODEL` over an OpenAI-compatible API. Put the provider key
in a repo-root `.env` (loaded below): `ANTHROPIC_API_KEY=...` for the default
`anthropic/claude-sonnet-5`, or `OPENROUTER_API_KEY=...` for an `openrouter/...` model. For a
self-hosted model, point at its endpoint with `LLM_API_BASE=http://your-host:PORT/v1` and set
`OPENAI_API_KEY` to any non-empty placeholder.
""")
)

# ── Config ──────────────────────────────────────────────────────────────────
cells.append(
    md("""
## Setup

One knob - the **data root**. Set `SYNNO_DATA_DIR` (env or `.env`) to where your TPC-H data
should live; unset, it defaults to a project-local `.synno_data/`. It does **not** need to
exist yet: the next cell generates the TPC-H parquet into it on first run.
""")
)

cells.append(
    code("""
import logging
import os, json, time, statistics
from pathlib import Path

from dotenv import load_dotenv

from synnodb.utils.path_utils import repo_root
from synnodb.observability.logging.logger import setup_logging
load_dotenv()  # let SYNNO_DATA_DIR / SYNNO_ENGINES_DIR / SYNNO_WORKSPACE come from a .env

# Surface the library's INFO logs in the notebook. setup_logging installs a stdout handler, so
# log lines interleave with print() output in the same cell instead of showing as a separate
# stderr block; without it Jupyter only surfaces print statements and the logs stay hidden.
setup_logging(logging.INFO)

# The data root holds everything: parquet, engines, workspace. Honor SYNNO_DATA_DIR if set,
# else default to a project-local .synno_data/. It need not exist yet - the next cell
# materializes the TPC-H parquet into it.
DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR") or repo_root() / ".synno_data")
PARQUET_DIR = DATA_ROOT / "workloads" / "tpch" / "tpch_parquet"
ENGINES_DIR = DATA_ROOT / "engines"

SF = 5
SCALE_FACTORS = (1, 2, SF)        # sf1/sf2: cheap correctness rungs; sf20: benchmark + serving
SF_DIR = PARQUET_DIR / f"sf{SF}"  # the benchmark scale factor's parquet

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

MODEL = os.environ.get(
    "SYNNO_MODEL", "anthropic/claude-sonnet-5"
)  # e.g. "anthropic/claude-sonnet-4-6", "gpt-5.4", "openrouter/z-ai/glm-5.2"
MODEL_EXTRA_BODY = json.loads(os.environ.get("SYNNO_MODEL_EXTRA_BODY", "null"))

print("Data root   :", DATA_ROOT)
print("Parquet dir :", PARQUET_DIR)
print("Engines dir :", ENGINES_DIR)
print("Model       :", MODEL)
""")
)

# ── Generate TPC-H parquet ───────────────────────────────────────────────────
cells.append(
    md("""
### Generate the TPC-H parquet (first run only)

To keep the notebook self-contained we materialize the TPC-H tables ourselves with DuckDB's
built-in `dbgen` - no external download, no `dbgen` binary. Parquet is written where the
framework looks for a built-in-style workload:

```
<DATA_ROOT>/workloads/tpch/tpch_parquet/sf<N>/<table>.parquet
```

We generate **sf1** and **sf2** (the cheap rungs the engine build validates correctness
against) and **sf20** (the benchmark / serving scale). The step is idempotent - tables already
on disk are skipped - and a one-time cost. **sf20 is ~15-20 GB and takes a while**; make sure
you have the disk. Generation caps DuckDB's memory and spills to a temp directory so it does
not OOM at the larger scale factor.
""")
)

cells.append(
    code("""
from synnodb.workloads.dataset.gen_tpc_h_data import ensure_tpch_parquet

ensure_tpch_parquet(PARQUET_DIR, SCALE_FACTORS, TABLES)
""")
)

# ── Step 1 header ───────────────────────────────────────────────────────────
cells.append(
    md("""
---
## Step 1 - Workload Registration

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

QUERIES_JSON = Path("tpch_queries.json")  # lives next to this notebook

spec = register_workload_from_json(
    name="tpch_byo",
    queries_json=QUERIES_JSON,
    parquet_dir=PARQUET_DIR,
    scale_factors=SCALE_FACTORS,
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
rng = random.Random(42)
gen = spec.query_gen_factory(None)

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

cells.append(
    md(
        """
### Start the SynnoDB engine

Constructing the `SynnoDB` driver spawns an in-process **live-UI dashboard** and prints its
URL (e.g. `http://localhost:8765`). Open it in a browser to watch generation unfold in real
time - input tokens, code size, per-query speedups, cost/runtime, and an activity log, all
refreshing every few seconds.

Every stage you chain on this same `db` - storage plan → base implementation →
`runOptimLoop` → `addMultiThreading` → `checkSfCorrectness` - streams onto **one continuous
timeline**, so the whole journey shows up on a single page instead of resetting per stage.
The dashboard stays up for the lifetime of this kernel; the URL is also available as
`db.dashboard_url`.
""",
        trailing_nl=True,
    )
)

cells.append(
    code("""
from synnodb import SynnoDB

db = SynnoDB(
    workload="tpch_byo",
    model=MODEL,
    model_extra_body=MODEL_EXTRA_BODY,
    db_storage="in_memory",
    queries="1-5",
    data_dir=DATA_ROOT,
)
""")
)

cells.append(md("### Storage plan"))

cells.append(
    code("""
plan = db.createStoragePlan()

print(plan.text[:600], "...")
""")
)

cells.append(
    md("""
### Base implementation

We feed the plan **content** straight in via `storage_plan=plan.text`, so this step needs
no W&B. If you instead chain off a logged storage-plan run, pass its run id with
`db.createBaseImpl(storage_plan_wandb_id=plan.run_id)`. Provide exactly one of the two.
""")
)

cells.append(
    code("""
impl = db.createBaseImpl(storage_plan=plan.text)

print("Workspace :", impl.workspace)
print("Files     :", sorted(impl.files))
print()
print(f"Engine published to: {ENGINES_DIR}")
""")
)

# ── Step 3a - DuckDB baseline run ────────────────────────────────────────────
cells.append(
    md("# Step 3a - Benchmark DuckDB\n### Run queries on DuckDB as comparison baseline")
)

cells.append(
    code("""
import duckdb
import tempfile
from tqdm import tqdm

# Threads both engines run at, so DuckDB and SynnoDB are compared at the same parallelism.
NUM_THREADS = os.cpu_count()  # 1 for demo, else os.cpu_count() for benchmark
assert NUM_THREADS is not None, "os.cpu_count() returned None"

duck = duckdb.connect(":memory:", config={"threads": NUM_THREADS})
duck.execute("PRAGMA disable_progress_bar")
duck.execute("PRAGMA enable_profiling='json'")  # EXPLAIN ANALYZE returns its profile as JSON
# enable_profiling also makes DuckDB dump a JSON profile to the console after *every* statement
# (the CREATE TABLEs below, every EXPLAIN ANALYZE) - a wall of text in the notebook. Send those
# automatic dumps to a throwaway file; the EXPLAIN ANALYZE result set still carries the profile
# analyze_ms() reads, so the timings are unaffected.
duck.execute(f"PRAGMA profiling_output='{Path(tempfile.gettempdir()) / 'duckdb_profile.json'}'")
# Materialize each table fully in memory (CREATE TABLE, not a VIEW over the parquet), so the
# measured query time is in-memory execution - not a fresh parquet scan on every run.
for t in TABLES:
    duck.execute(
        f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{SF_DIR}/{t}.parquet')"
    )


def analyze_ms(con, sql):
    \"\"\"DuckDB's own execution latency for `sql`, via EXPLAIN ANALYZE - the server-side query
    time, excluding the Python call and result-fetch overhead a wall-clock timer would include.
    The json profile (whose `latency` is in seconds) is the second column of the result row.\"\"\"
    profile = json.loads(con.execute("EXPLAIN ANALYZE " + sql).fetchone()[1])
    return profile["latency"] * 1_000


baseline_times = {}
for qid, insts in tqdm(instantiations.items(), desc="Measuring DuckDB performance"):
    baseline_times[qid] = [analyze_ms(duck, sql) for _, sql, _ in insts]

duck.close()

total_duck = sum(sum(v) / len(v) for v in baseline_times.values())
print(f"{'Query':<8} {'Avg (ms)':>12} {'Median (ms)':>14}")
print("-" * 38)
for qid in spec.all_query_ids:
    t = baseline_times[qid]
    print(f"Q{qid:<7} {sum(t) / len(t):>12.1f} {statistics.median(t):>14.1f}")
print("-" * 38)
print(f"{'TOTAL':<8} {total_duck:>12.1f}")
""")
)

# ── Step 3 ───────────────────────────────────────────────────────────────────
cells.append(
    md("""
---
## Step 3 - Drop In SynnoDB

The only change is **one import line** and a few extra keyword arguments to `connect()`:

```diff
- import duckdb
+ import synnodb
+ from synnodb.router import RouterMode, RouterPolicy

-  con = duckdb.connect(
+  con = synnodb.connect(
      ":memory:",
+     config={"threads": NUM_THREADS},   # same knob DuckDB got - fixes the engine's parallelism too
+     engines=str(ENGINES_DIR),
+     policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
  )
```

Every other line - the table setup, the `execute()` calls, `fetchall()` - is identical.
""")
)

cells.append(md("### Open the drop-in connection"))

cells.append(
    code("""
import synnodb
from synnodb.router import RouterMode, RouterPolicy

con = synnodb.connect(
    ":memory:",
    config={"threads": NUM_THREADS},  # same thread budget as the DuckDB baseline above
    engines=str(ENGINES_DIR),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
)

# Same in-memory materialization as the DuckDB baseline (CREATE TABLE, not a VIEW), so both
# systems query fully-resident data.
for t in TABLES:
    con.duckdb.execute(
        f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{SF_DIR}/{t}.parquet')"
    )

con.refresh_engines()
n = con.router_stats()["registry"]["templates"]
print(f"Discovered {n} engine template(s) under {ENGINES_DIR}")

# Load each engine's data now - start its process, read the snapshot, build its in-memory
# database - so this one-time cost is paid here as an explicit step instead of landing on the
# first query. Without it the first query below would carry the whole warm-up (seconds at
# scale) and read as a spurious slowdown; with it, every measured query is served warm.
n_warmed = con.synno_ingest_data()
print(f"Preloaded {n_warmed} engine(s) - first query is served warm")
""")
)

cells.append(
    md("""
### Run the same queries with the same parameter values

We iterate over `instantiations` - the exact SQL strings from Step 1. For each we record the
engine's **own execution latency** (`engine_ms`, as measured by the router), the same
server-side basis the DuckDB baseline uses via `EXPLAIN ANALYZE` - so the two columns compare
like with like rather than one wall-clock against another.
""")
)

cells.append(
    code("""
synno_times = {}
routed_to = {}  # per query: "SynnoDB" if the bespoke engine served it, else "DuckDB"
for qid, insts in instantiations.items():
    times = []
    backends = []
    for _, sql, _ in insts:
        con.execute(sql).fetchall()
        # `engine_ms` is present when the bespoke engine served the query;
        # a query that falls back to DuckDB exposes `duckdb_ms` instead.
        last = con._last
        served_bespoke = "engine_ms" in last
        times.append(last["engine_ms"] if served_bespoke else last["duckdb_ms"])
        backends.append(served_bespoke)
    synno_times[qid] = times
    routed_to[qid] = "SynnoDB" if all(backends) else "DuckDB"
""")
)

cells.append(md("### Speedup"))

cells.append(
    code("""
total_synno = sum(sum(v)/len(v) for v in synno_times.values())

print(f"{'Query':<8} {'Routing':>8} {'DuckDB (ms)':>12} {'SynnoDB (ms)':>14} {'Speedup':>9}")
print("-" * 55)
for qid in spec.all_query_ids:
    avg_d = sum(baseline_times[qid])/len(baseline_times[qid])
    avg_s = sum(synno_times[qid])/len(synno_times[qid])
    speedup = avg_d / avg_s if avg_s > 0 else float("inf")
    # ⚡ marks queries the bespoke SynnoDB engine served; the rest fell back to DuckDB.
    routing = routed_to[qid]
    mark = " ⚡" if routing == "SynnoDB" else ""
    print(f"Q{qid:<7} {routing:>8} {avg_d:>12.1f} {avg_s:>14.1f} {speedup:>8.2f}x{mark}")
print("-" * 55)
overall = total_duck / total_synno if total_synno > 0 else float("inf")
print(f"{'TOTAL':<8} {'':>8} {total_duck:>12.1f} {total_synno:>14.1f} {overall:>8.2f}x")
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
    code(r"""
stats = con.router_stats()["session"]
print(f"Routed:         {stats['routed']}")
print(f"Cross-checked:  {stats['cross_checked']}")
print(f"Mismatches:     {stats['cross_check_mismatch']}")
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
print("\nAll results match DuckDB exactly.")
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

# ── Bonus - custom conversation ──────────────────────────────────────────────
cells.append(
    md(
        """
---
## Bonus - Define Your Own Conversation

Every built-in stage above is an ordinary `ConversationPlan` - and you can assemble
your own from the same primitives. A plan names the run (for logging and caching),
states *what the prepared workspace must provide* (`PrepareFeatures`), and supplies a
`stages` callable that turns a `ConvContext` into a flat list of stage items:

- `PromptStage` - one declarative LLM task with measurement/revert flags; its prompt
  callback receives the freshly measured runtime (and trace data, if requested).
- `PerQueryLoop` - one conversation branch per query, stages executed ring by ring.
- markers (`Compact`, `Benchmark`, `ValidateOn`, ...) and checks (`AssertCorrect`).

`db.run_synthesis(plan, start=...)` is the single entry point every stage goes
through; `start` chains off any earlier artifact (here: the base implementation).
The returned artifact carries the final snapshot hash and the workspace's prepare
record, so it chains onwards (e.g. into `db.checkSfCorrectness(result, target_sf=100)`).
""",
        trailing_nl=True,
    )
)

cells.append(
    code(
        r"""
from synnodb import (
    AssertCorrect, Benchmark, Compact, ConversationPlan, ConvContext,
    PerQueryLoop, PrepareFeatures, PromptStage,
)

def my_stages(ctx: ConvContext):
    return [
        AssertCorrect(),
        PromptStage(
            descriptor="inspect hot loops",
            get_prompt=lambda _exec_settings, _rt: (
                f"Profile {ctx.filenames.query_impl_path} and summarize the hot loops."),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
        ),
        Compact(),
        PerQueryLoop(lambda qid, ctx: [
            PromptStage(
                descriptor=f"tune {qid}",
                get_prompt_with_tracing=lambda _exec_settings, rt, trace: (
                    f"Query {qid} currently runs in {rt:.0f} ms.\n"
                    f"Trace:\n{trace}\nOptimize it."),
                max_turns=125,
                # defaults: measure after stage, auto-revert on regression
            ),
        ]),
        Benchmark(),
    ]

tuning_plan = ConversationPlan(
    name="myTuningPass",                    # run identity: naming, logging, caching
    prepare=PrepareFeatures(tracing=True),  # the workspace needs tracing instrumentation
    stages=my_stages,
)

# Uncomment to run the custom pass on top of the base implementation:
# tuned = db.run_synthesis(tuning_plan, start=impl)
""",
        trailing_nl=True,
    )
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

These chain **without W&B**: each stage restores the previous one's engine straight from
its git snapshot (`impl.snapshot_hash`, `opt.snapshot_hash`, ...) in the local workspace, so
the whole pipeline runs for non-W&B users out of the box. (This works within one session /
workspace; to chain across machines, log to W&B and pass the run id instead, e.g.
`db.runOptimLoop(base_impl_wandb_id=impl.run_id)`.)

> **Want W&B logging?** Pass `wandb_project="..."` (and/or `wandb_entity="..."`) to
> `SynnoDB(...)`. W&B is off unless one of them is set — nothing logs in, initializes, or
> requires credentials otherwise.
""")
)

# ── Assemble and write ───────────────────────────────────────────────────────
# Sequential cell ids keep regeneration deterministic (no churn from random ids).
for i, cell in enumerate(cells):
    cell["id"] = str(i)

nb = new_notebook(cells=cells)
nb.metadata["kernelspec"] = {
    "display_name": "synnodb",
    "language": "python",
    "name": "python3",
}
nb.metadata["language_info"] = {
    "codemirror_mode": {"name": "ipython", "version": 3},
    "file_extension": ".py",
    "mimetype": "text/x-python",
    "name": "python",
    "nbconvert_exporter": "python",
    "pygments_lexer": "ipython3",
    "version": "3.13.11",
}

out = TUTORIAL_DIR / "gen_tpch_demo.ipynb"
with open(out, "w", encoding="utf-8") as f:
    nbformat.write(nb, f)

print(f"Written: {out}")
