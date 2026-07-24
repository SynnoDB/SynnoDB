"""One-shot script to write gen_clickbench_demo.ipynb into the parent tutorials/ folder."""

from pathlib import Path
import nbformat
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell

TUTORIAL_DIR = Path(
    __file__
).parent.parent  # tutorials/, alongside clickbench_queries.json


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
# SynnoDB: From DuckDB to Bespoke in One Import (ClickBench)

SynnoDB is a drop-in replacement for DuckDB that transparently accelerates your SQL queries
with auto-generated bespoke C++ engines - while falling back to DuckDB for everything else.
No schema changes. No query rewrites. One import.

This notebook walks through the full journey for **10 templated queries** derived from the
official ClickBench suite, against the single 100M-row `hits` table:

1. **Baseline** - run all 10 queries on vanilla DuckDB, 10 parameter instantiations each
   (different literal values drawn per query - see "Why templated?" below), wall-clock and
   server-side latency recorded.
2. **Generate** - point SynnoDB at a `clickbench_queries.json` file and let it write the engine.
3. **Drop in** - replace one import, re-run the identical queries, compare the numbers.

### Why templated?

The *official* 43 ClickBench queries are all static SQL with no placeholders. That is a poor fit
for demonstrating a *generated* engine: with a fixed, finite query set the engine could in
principle memoize each of the 43 answers instead of implementing the actual filter/aggregate logic
- and there would be no way to tell the difference from the outside. TPC-H and CEB avoid this
because their queries are parameterized, so a correct engine must implement the predicate/aggregate
generically. This tutorial instead uses **10 of our own queries**, derived from ten of the official
43 but with representative literals replaced by typed `[PLACEHOLDER]` specs (thresholds,
`LIMIT`/`OFFSET`, date windows, a text token, an IN-list) - the same `params`/`param_groups` shape
TPC-H's `queries.json` uses. See `tutorials/assemble_tutorial/_gen_clickbench_queries.py` for the
full derivation and the reasoning behind each template (including why `CounterID = 62` stays a
literal rather than a templated value - see that file's docstring).

It is **self-contained**: point it at a fresh (non-existent) data root and it downloads the
official ClickBench `hits.parquet` (~14.8 GB) and loads it into a typed local DuckDB itself - no
manual dataset prep. Unlike TPC-H, ClickBench is real recorded data (not something `dbgen` can
synthesize), so the notebook downloads it once and reuses the local copy afterwards.

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

**Disk + time** - the raw parquet is ~14.8 GB and the typed local DuckDB it is loaded into is a
further several GB; budget ~20-30 GB free and however long a ~15 GB sequential download takes on
your connection (one-time - subsequent runs reuse the local file). Generating the engine itself
(10 queries against one table) is the other big time cost; see Step 2 below.
""")
)

# ── Config ──────────────────────────────────────────────────────────────────
cells.append(
    md("""
## Setup

One knob - the **data root**. Set `SYNNO_DATA_DIR` (env or `.env`) to where the ClickBench data
should live; unset, it defaults to a project-local `.synno_data/`. It does **not** need to exist
yet: the next cell downloads + loads `hits` into it on first run.
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

# The data root holds everything: the local hits.duckdb, engines, workspace. Honor SYNNO_DATA_DIR
# if set, else default to a project-local .synno_data/. It need not exist yet - the next cell
# materializes hits.duckdb into it.
DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR") or repo_root() / ".synno_data")
CLICKBENCH_PARQUET = DATA_ROOT / "clickbench_hits.parquet"  # the raw download
CLICKBENCH_DB = DATA_ROOT / "clickbench_hits.duckdb"        # typed, query-ready

MODEL = os.environ.get(
    "SYNNO_MODEL", "anthropic/claude-sonnet-5"
)  # e.g. "anthropic/claude-sonnet-4-6", "gpt-5.4", "openrouter/z-ai/glm-5.2"
MODEL_EXTRA_BODY = json.loads(os.environ.get("SYNNO_MODEL_EXTRA_BODY", "null"))

# Degree of parallelism the engine is generated, validated, AND served at (the DuckDB-style
# config={'threads': N}). The same knob is used for the DuckDB baseline below, so both sides are
# compared at equal parallelism.
NUM_THREADS = os.cpu_count()
assert NUM_THREADS is not None, "os.cpu_count() returned None"

print("Data root       :", DATA_ROOT)
print("Parquet download :", CLICKBENCH_PARQUET)
print("Local DuckDB     :", CLICKBENCH_DB)
print("Model            :", MODEL)
print("Threads          :", NUM_THREADS)
""")
)

# ── Download + load ClickBench ────────────────────────────────────────────
cells.append(
    md("""
### Download + load ClickBench (first run only)

ClickBench ships real recorded data, not a synthetic generator like TPC-H's `dbgen`, so this
downloads the official `hits.parquet` from ClickHouse's public dataset host and loads it into a
typed local DuckDB using ClickBench's own column typing (the raw parquet stores dates/timestamps
as bare integers).

Both steps are idempotent - a file already present is reused - so re-running this cell after the
first successful run is instant. The download is a plain sequential GET rather than a
range-seeking stream: the host answers ranged requests with a full `200 OK` instead of `206
Partial Content`, which breaks streaming readers (including DuckDB's own
`read_parquet('https://...')`) that rely on real partial content.
""")
)

cells.append(
    code("""
from synnodb.workloads.dataset.gen_clickbench_data import (
    ensure_clickbench_parquet,
    ensure_clickbench_duckdb,
)

ensure_clickbench_parquet(CLICKBENCH_PARQUET)
ensure_clickbench_duckdb(CLICKBENCH_DB, CLICKBENCH_PARQUET)
""")
)

# ── Step 1 header ───────────────────────────────────────────────────────────
cells.append(
    md("""
---
## Step 1 - Workload Registration

We run **our 10 templated ClickBench-derived queries on vanilla DuckDB**: 10 instantiations per
query (different placeholder values, drawn from each template's declared value space), total
wall-clock time recorded. These exact SQL strings are reused in Step 3 so the comparison is
apples-to-apples.
""")
)

cells.append(
    md("""
### Register the workload from a live DuckDB connection

`db.sync_from_duckdb` is the bring-your-own entry point for data you already hold in DuckDB (the
sibling of `register_workload_from_json`, which takes pre-scaled parquet instead). It reads the
schema and queries through the connection once, freezes a consistent point-in-time snapshot it
owns, and derives small downscaled subsets (`0.02`, `0.1` of the table by default) as cheap
correctness rungs - no `sf1`/`sf2` parquet to hand-generate, unlike TPC-H. The full table
(`fraction1.0`) is both the correctness ceiling and the benchmark scale - ClickBench has no larger
synthetic scale factor to validate against beyond the real data.

Each entry in `clickbench_queries.json` carries its SQL template **and** a typed **spec** for each
`[PLACEHOLDER]` slot - declaring its value *space*, which is sampled at run time - exactly the
shape TPC-H's `queries.json` uses:

```json
"7": {
  "sql": "... WHERE CounterID = 62 AND EventDate >= date '[DATE_FROM]' AND EventDate < date '[DATE_FROM]' + interval '[WINDOW_DAYS]' day ... LIMIT [TOPK];",
  "params": {
    "DATE_FROM":    { "type": "date", "min": "2013-07-01", "max": "2013-07-24" },
    "WINDOW_DAYS":  { "type": "int",  "min": 1, "max": 7, "step": 1 },
    "TOPK":         { "type": "int",  "min": 5, "max": 25, "step": 5 }
  }
}
```
""")
)

cells.append(
    code("""
import duckdb
from synnodb import SynnoDB

QUERIES_JSON = Path("clickbench_queries.json")  # lives next to this notebook

# Constructing the SynnoDB driver spawns an in-process live-UI dashboard and prints its URL (e.g.
# http://localhost:8765). Open it in a browser to watch generation unfold in real time - input
# tokens, code size, per-query speedups, cost/runtime, and an activity log, all refreshing every
# few seconds. Every stage chained on this same `db` streams onto one continuous timeline.
db = SynnoDB(
    model=MODEL,
    model_extra_body=MODEL_EXTRA_BODY,
    db_storage="in_memory",
    queries="1-10",
    data_dir=DATA_ROOT,
    threads=NUM_THREADS,
    max_turns=450,  # double the default per-stage LLM turn budget (225) for the base implementation
)

# Open read-only: a read-write connection takes an exclusive OS lock on the file, which would
# block SynnoDB's own read-only access below. Read-only openers coexist.
duckdb_con = duckdb.connect(str(CLICKBENCH_DB), read_only=True)

spec = db.sync_from_duckdb(
    duckdb_con,
    name="clickbench_byo",
    queries_json=QUERIES_JSON,
    schema_example_table="hits",
)

print("Workload :", spec.name)
print("Tables   :", spec.tables)
print("Queries  :", spec.all_query_ids)
print("Subsets  :", spec.exhaustive_sfs, "(benchmark:", spec.benchmark_sf, ")")
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
raw_queries = json.loads(QUERIES_JSON.read_text())
for qid, entry in raw_queries.items():
    print(f"=== Q{qid} ===")
    print(entry["sql"][:200], "...")
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
import random

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

Same `db` object. It:

1. **Creates a storage plan** - decides how the `hits` table's columns are laid out in memory.
2. **Implements the engine** - writes multi-threaded C++, compiles it, validates every output
   against DuckDB (cheap downscaled rungs first, then the full table), then **auto-publishes** the
   binary into `DATA_ROOT/engines`.

This is a one-time cost. Once published the engine is discovered automatically across sessions.
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
print(f"Engine published to: {DATA_ROOT / 'engines'}")
""")
)

# ── Step 3a - DuckDB baseline run ────────────────────────────────────────────
cells.append(
    md(
        "# Step 3a - Benchmark DuckDB\n### Run all 10 queries on DuckDB as comparison baseline"
    )
)

cells.append(
    code("""
import tempfile
from tqdm import tqdm

duck = duckdb.connect(":memory:", config={"threads": NUM_THREADS})
duck.execute("PRAGMA disable_progress_bar")
duck.execute("PRAGMA enable_profiling='json'")  # EXPLAIN ANALYZE returns its profile as JSON
# enable_profiling also makes DuckDB dump a JSON profile to the console after *every* statement -
# a wall of text in the notebook. Send those automatic dumps to a throwaway file; the EXPLAIN
# ANALYZE result set still carries the profile analyze_ms() reads, so the timings are unaffected.
duck.execute(f"PRAGMA profiling_output='{Path(tempfile.gettempdir()) / 'duckdb_profile.json'}'")
# Materialize hits fully in memory (CREATE TABLE, not a VIEW) by copying it out of the local typed
# DuckDB built above, so the measured query time is in-memory execution - not a fresh scan of the
# on-disk file on every run.
duck.execute(f"ATTACH '{CLICKBENCH_DB}' AS src (READ_ONLY)")
duck.execute("CREATE TABLE hits AS SELECT * FROM src.hits")
duck.execute("DETACH src")


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
+     engines=str(DATA_ROOT / "engines"),
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
    engines=str(DATA_ROOT / "engines"),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
)

# Same in-memory materialization as the DuckDB baseline (CREATE TABLE, not a VIEW), so both
# systems query fully-resident data.
con.duckdb.execute(f"ATTACH '{CLICKBENCH_DB}' AS src (READ_ONLY)")
con.duckdb.execute("CREATE TABLE hits AS SELECT * FROM src.hits")
con.duckdb.execute("DETACH src")

con.refresh_engines()
n = con.router_stats()["registry"]["templates"]
print(f"Discovered {n} engine template(s) under {DATA_ROOT / 'engines'}")

# Load the engine's data now - start its process, read the snapshot, build its in-memory
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
engine's **own execution latency** (`engine_ms`, as measured by the router), the same server-side
basis the DuckDB baseline uses via `EXPLAIN ANALYZE` - so the two columns compare like with like
rather than one wall-clock against another.
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

cells.append(
    md(
        "### Q7 result (top URLs by pageviews in a sampled date window, with timing footer)"
    )
)

cells.append(
    code("""
_, q7_sql, q7_params = instantiations["7"][0]
print("params:", q7_params)
con.execute(q7_sql)
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
record, so it chains onwards.
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
    publishes_engine=True,                  # tuned engine is re-published for the router
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

The base implementation is single-threaded per query unless a per-query multi-threading pass
already fixed it up at `NUM_THREADS`. The same `SynnoDB` object carries the engine further:

```python
opt   = db.runOptimLoop(base_impl=impl)           # single-threaded SIMD / cache optimization
multi = db.addMultiThreading(optimized=opt)        # parallel execution across cores
```

Unlike TPC-H, ClickBench has no larger synthetic scale factor to validate against -
`fraction1.0` **is** the full real dataset - so there is no `checkSfCorrectness(target_sf=...)`
step here; correctness at the benchmark scale is already established by `createBaseImpl`.

These chain **without W&B**: each stage restores the previous one's engine straight from
its git snapshot (`impl.snapshot_hash`, `opt.snapshot_hash`, ...) in the local workspace, so
the whole pipeline runs for non-W&B users out of the box. (This works within one session /
workspace; to chain across machines, log to W&B and pass the run id instead, e.g.
`db.runOptimLoop(base_impl_wandb_id=impl.run_id)`.)

> **Want W&B logging?** Pass `wandb_project="..."` (and/or `wandb_entity="..."`) to
> `SynnoDB(...)`. W&B is off unless one of them is set - nothing logs in, initializes, or
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

out = TUTORIAL_DIR / "gen_clickbench_demo.ipynb"
with open(out, "w", encoding="utf-8") as f:
    nbformat.write(nb, f)

print(f"Written: {out}")
