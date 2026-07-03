# Tutorial: from zero to a bespoke base implementation (TPC-H Q1 & Q5)

This walks you through the **whole loop** end to end, from an empty machine to a DuckDB drop-in
that transparently accelerates two TPC-H queries with bespoke C++ engines the agent factory writes
for you - and falls straight back to DuckDB for everything else, cross-checked for correctness.

Scope: **storage plan + base implementation only**, **single-threaded**, **scale factor 20**, model
**MiniMax-M3**. The later stages (optimization loop, multi-threading, large-SF correctness sweep)
are intentionally out of scope here - see [Where to go next](#7-where-to-go-next).

---

## 0. The mental model

SynnoDB is two things in one package:

| Face | What it is | Import |
|---|---|---|
| **The drop-in runtime** (light) | `import synnodb as duckdb` - a DuckDB-compatible connection that *routes* a matching `SELECT` to a bespoke engine and otherwise behaves byte-identically to DuckDB | `synnodb` |
| **The agent factory** (heavy) | the LLM pipeline that *generates* those engines from your queries | `synnodb[factory]` |

The factory pipeline is a chain of stages; **we stop after the base implementation**:

```
createStoragePlan  ->  createBaseImpl  ->  [ runOptimLoop -> addMultiThreading -> checkSfCorrectness ]
   (this tutorial)      (this tutorial)     (not covered here)
```

- **Storage plan**: the agent decides how each query's data is laid out in memory (a
  `storage_plan.txt` document).
- **Base implementation**: the agent writes single-threaded C++ that loads that layout and answers
  the queries, compiles it to a `db` binary, and validates it against DuckDB. **"Base
  implementation" is single-threaded by construction** - threading is a separate later stage
  (`addMultiThreading`), which we do not run.

When a base implementation finishes, SynnoDB **auto-publishes** the compiled engine into your
engines directory, and any drop-in connection pointed at that directory starts routing the matching
queries with no code change.

---

## 1. Prerequisites and install

System packages (Linux x86-64):

- C++ toolchain (`gcc`/`clang`), Python 3.10+, [`uv`](https://github.com/astral-sh/uv)
- Apache Arrow + Parquet dev libraries (`libarrow-dev`, `libparquet-dev`)
- [`cloc`](https://github.com/AlDanial/cloc) (the factory tracks generated code size with it)

Install them as in the repo [README](../README.md#installation), then install the package **with the
factory extra** (generation needs the LLM stack):

```bash
uv sync --extra factory          # add --extra dev if you want to run the tests
```

Create a `.env` in the repo root:

```bash
# Required so the factory can reach the model. For a LOCAL MiniMax-M3 endpoint (see step 3) the
# key is only a placeholder the OpenAI client insists on - any non-empty value works.
OPENAI_API_KEY=sk-local-placeholder

# Weights & Biases is used for run provenance AND to chain stages (the base-impl stage references
# the storage-plan run id). Sign up free at https://wandb.ai and set:
WANDB_ENTITY=your-entity
WANDB_PROJECT=synnodb-tutorial

# Shared snapshot cache (recommended): a git server the factory pushes/pulls generated-code
# snapshots to, so an identical (model, query, storage-plan) build is restored instead of
# re-generated - across machines and runs. Leave unset to use the local snapshot cache only
# (equivalently pass --disable_repo_sync). On the lab network this is:
GIT_SNAPSHOTTER_SERVER="git://c01/bespoke_cache.git"
```

> **Chaining without W&B.** The base-impl stage only needs the storage plan's **text**. The
> simplest, W&B-free path is to pass that text directly: `db.createBaseImpl(storage_plan=plan.text)`
> (CLI: `--storage_plan_text "$(cat storage_plan.txt)"`). Alternatively, if the storage-plan run was
> logged (`log_to_wandb=True`, the default), you can chain off its `run_id` with
> `db.createBaseImpl(storage_plan_wandb_id=plan.run_id)` (CLI: `--storage_plan_run_id <id>`), in which
> case the plan is recovered from W&B and its config is validated against the current run. Provide
> exactly one of the two.

Point SynnoDB at a data root. Everything (parquet, caches, logs, the published engines) lives under
it:

```bash
export SYNNO_DATA_DIR=/path/to/synno_data      # e.g. /mnt/data/synno_data
export SYNNO_ENGINES_DIR="$SYNNO_DATA_DIR/engines"   # where finished engines auto-publish
```

`SYNNO_ENGINES_DIR` defaults to `$SYNNO_DATA_DIR/engines`, so setting it is optional - but being
explicit makes step 5 obvious.

---

## 2. Get the TPC-H data (the part the README only assumes)

The README says "place TPC-H Parquet files in your artifacts directory" but not *how*, and the old
`gen_tpc_h_data.py` helper writes to a legacy hardcoded path. For a **built-in** workload like
TPC-H, the factory looks for parquet at a path derived from `SYNNO_DATA_DIR`:

```
$SYNNO_DATA_DIR/workloads/tpch/tpch_parquet/sf<N>/<table>.parquet
```

DuckDB ships a TPC-H generator (`dbgen`), so you need no external tooling. **Which scale factors?**
The TPC-H profile validates correctness during generation at **sf1** and **sf2** (cheap) and
benchmarks / ingests at **sf20** (the "scale factor 20" headline). Generate all three:

```python
# gen_tpch_for_tutorial.py  -  writes parquet where the built-in TPC-H workload looks for it.
import os
from pathlib import Path
import duckdb

DATA = Path(os.environ["SYNNO_DATA_DIR"])
TABLES = ["region", "nation", "supplier", "part", "customer", "partsupp", "orders", "lineitem"]

for sf in (1, 2, 20):                       # sf1/sf2: correctness sweep; sf20: benchmark + serving
    out = DATA / "workloads" / "tpch" / "tpch_parquet" / f"sf{sf}"
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()                  # threads here are just data-gen speed, not the engine
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute(f"CALL dbgen(sf={sf});")
    for t in TABLES:
        con.execute(f"COPY {t} TO '{out / (t + '.parquet')}' (FORMAT PARQUET);")
    con.close()
    print("wrote", out)
```

```bash
.venv/bin/python gen_tpch_for_tutorial.py
```

sf1 and sf2 take seconds; **sf20 is ~20 GB and takes a while** - make sure you have the disk. (At
larger SFs, generate on an on-disk DuckDB connection with a `temp_directory` set so it can spill;
see the repo's `gen_tpc_h_data.py` for that variant.)

---

## 3. Point SynnoDB at MiniMax-M3

`MiniMax-M3` is a self-hosted model. SynnoDB knows it as **`openai/unsloth/MiniMax-M3`** (registered
in `src/synnodb/llm/models.py`, 376K context) and talks to it over an **OpenAI-compatible** HTTP
endpoint. **You usually do not have to set the endpoint at all.** `setup_model_config`
(`src/synnodb/utils/model_setup.py`) resolves it, in priority order:

1. the `--api_base` flag,
2. `LLM_API_BASE` (the canonical generic var for a local/custom endpoint),
3. `OPENAI_API_BASE`, then `LITELLM_API_BASE`,
4. otherwise it **defaults to `http://dgx02:13505/v1`** for any non-cloud (`openai/...`) provider -
   the lab's vLLM server that hosts MiniMax-M3.

So on the lab network, MiniMax-M3 just works with no endpoint argument. Elsewhere, point it at your
own server with one line:

```bash
export LLM_API_BASE=http://your-host:13505/v1     # only if not on the default dgx02 endpoint
```

The `OPENAI_API_KEY` placeholder from step 1 is enough; a local server ignores its value.

---

## 4. Generate the storage plan + base implementation

### About `--queries`

`--queries` accepts a **single id** (`5`) or an **inclusive range** (`1-5`) - *not* a comma list
(`1,5` is silently treated as one bogus id). To cover Q1 and Q5 in one pass we use the range
`1-5` (which also builds Q2-Q4 - all of them become routable; everything outside still falls back).
To build *exactly* {1, 5}, run the pipeline twice (`--queries 1`, then `--queries 5`) into the same
`SYNNO_ENGINES_DIR`; the drop-in composes both - see the note at the end of step 5.

### Option A - Python API (recommended; chaining is automatic)

```python
# build.py
from synnodb import SynnoDB

db = SynnoDB.for_tpch(
    model="openai/unsloth/MiniMax-M3",
    db_storage="in_memory",                 # serve from RAM
    queries="1-5",                          # covers Q1 and Q5
    # No endpoint needed on the lab network (defaults to dgx02). To override, either set
    # LLM_API_BASE in the environment, or pass it through here since SynnoConfig has no
    # api_base field of its own:  extra_config={"api_base": "http://your-host:13505/v1"}
)

plan = db.createStoragePlan()               # -> StoragePlan; the agent writes storage_plan.txt
print("storage plan run:", plan.run_id)
print(plan.text[:800], "...")

impl = db.createBaseImpl(storage_plan=plan.text) # pass the plan content (W&B-free); writes + compiles the C++ engine
print("base impl run:", impl.run_id)
print("workspace:", impl.workspace)         # generated sources + the compiled `db` binary
print("files:", sorted(impl.files))         # e.g. db_loader.cpp, parquet_reader.cpp, query1.cpp, query_impl.cpp, ...
```

```bash
.venv/bin/python build.py
```

This runs **single-threaded** generation (the base-impl stage sets `needs_parallelism=False`); we
deliberately stop here and do **not** call `addMultiThreading`.

### Option B - separate runs (chain via the W&B run id)

When the two stages run in different processes (or you want each logged to W&B),
chain them by run id instead of by passing the in-memory artifact. Enabling W&B
(set `wandb_entity`/`wandb_project`) makes each stage return a `run_id`:

```python
from synnodb import SynnoDB

db = SynnoDB.for_tpch(
    model="openai/unsloth/MiniMax-M3", db_storage="in_memory", queries="1-5",
    wandb_entity="my-entity",            # presence of entity/project enables W&B logging
)

# 1) storage plan -> note its run id (e.g. persist plan.run_id, call it PLAN_ID)
plan = db.createStoragePlan()
print("storage plan run:", plan.run_id)

# 2) base implementation, recovering the plan from that run
impl = db.createBaseImpl(storage_plan_wandb_id=plan.run_id)   # or a literal "PLAN_ID"
```

(`auto_confirm`/`auto_finish` default to `True`, running the agent unattended; set
them `False` to confirm each step interactively.)

To run the base-impl stage **without W&B**, pass the plan text directly instead:
`db.createBaseImpl(storage_plan=plan.text)` (the Option A path).

### Auto-publish

Because `SYNNO_ENGINES_DIR` is set, the moment the base implementation compiles a `db` binary
SynnoDB publishes it - **at scale factor 20**, the benchmark SF - into
`$SYNNO_ENGINES_DIR`. You'll see a log line like:

```
published bespoke engine for auto-discovery -> .../engines/<engine-id>
```

That published engine is everything the drop-in needs.

---

## 5. Use it as a drop-in: Q1 & Q5 route, the rest fall back

Now the payoff. A normal program swaps `import duckdb` for `import synnodb as duckdb` and changes
nothing else:

```python
# run.py
import synnodb as duckdb
from synnodb.router import RouterMode, RouterPolicy
import os
from pathlib import Path

SF20 = Path(os.environ["SYNNO_DATA_DIR"]) / "workloads/tpch/tpch_parquet/sf20"
TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]

con = duckdb.connect(
    ":memory:",
    engines=os.environ["SYNNO_ENGINES_DIR"],            # discover the engine you just built
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),  # check EVERY routed query vs DuckDB
)

# Make the SF20 tables visible to the connection (the engine serves them; DuckDB is the oracle).
for t in TABLES:
    con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{SF20}/{t}.parquet')")

# A real TPC-H Q1 (the shape you built):
Q1 = """
SELECT l_returnflag, l_linestatus,
       sum(l_quantity) AS sum_qty, sum(l_extendedprice) AS sum_base_price,
       sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
       sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
       avg(l_quantity) AS avg_qty, avg(l_extendedprice) AS avg_price,
       avg(l_discount) AS avg_disc, count(*) AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL '90' DAY
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
"""

# Ask the router what it WILL do, without running anything:
print("Q1:", con.why(Q1)["decision"], "-", con.why(Q1)["reason"])      # -> would-route

con.execute(Q1)        # routes to your bespoke engine, cross-checked against DuckDB
con.show()             # pretty table + a "synno engine ... Nx vs DuckDB" footer with a speed emoji

# A query you did NOT build (no engine for count(*)): falls back to DuckDB, transparently.
print("count(*):", con.why("SELECT count(*) FROM lineitem")["decision"])   # -> would-fall-back
con.execute("SELECT count(*) FROM lineitem").fetchall()

# Session summary: how many routed, how many fell back, cross-check results.
print(con.router_stats()["session"])
con.close()
```

What you'll observe:

- `con.why(sql)` returns `would-route` for the shapes you built (Q1-Q5, any constants), and
  `would-fall-back` for everything else (Q6+, ad-hoc queries, or a Q1 whose *structure* differs).
- Routed queries are **cross-checked**: with `cross_check_rate=1.0` every routed result is compared
  to DuckDB's, and a divergent engine is quarantined (you'd see a loud `DIVERGED` log and the result
  served from DuckDB instead). In normal use you'd lower the rate; SynnoDB also **burns in** every
  new template by checking its first executions regardless of the rate.
- `con.show()` / `repr(con)` print the result with a timing + speedup footer; `fetchall()`, `df()`,
  `arrow()` work exactly as in DuckDB.

### Don't have a GPU/model handy? Preview the runtime now.

The repo ships a prebuilt Q1/Q6 engine and a demo that exercises this exact loop without any
generation:

```bash
.venv/bin/python examples/routing_demo.py
```

```
Before publishing an engine:
  Q1 -> would-fall-back (no engines registered)

Published q1q6byo; registered 2 templates.

query                                             served by
----------------------------------------------------------------------
Q1 (DELTA=90)                                     SynnoDB bespoke
Q1 (DELTA=120, same shape)                        SynnoDB bespoke
Q6                                                SynnoDB bespoke
Q1 near-miss (other constant date) -> falls back  DuckDB
count(*) lineitem (no engine) -> falls back       DuckDB

session: {'routed': 3, 'fell_back': 2, 'cross_checked': 3, 'cross_check_mismatch': 0, ...}
```

That is precisely the behavior your generated Q1-Q5 engine gives you - just for the queries you
built.

> **Exactly {1, 5} (not 1-5).** Run step 4 twice, once with `--queries 1` and once with
> `--queries 5`, both with `SYNNO_ENGINES_DIR` set. Each finished base impl auto-publishes into the
> same directory, and the drop-in discovers both - so Q1 and Q5 route while Q2/Q3/Q4 (and everything
> else) fall back. This shows off engine **composition**: independent builds, one transparent
> connection.

---

## 6. What just happened, and the guarantees

- You never wrote SQL twice or changed your query code - you swapped one import.
- Only the **exact query shapes you built** are accelerated. A different shape, a write, or an
  unparseable statement always falls back to plain DuckDB.
- Every routed result is **verifiable against DuckDB** and the framework is **fail-closed**: on any
  doubt (a divergence, a comparison error, or a reference it cannot compute) it serves DuckDB's
  trusted result rather than an unverified engine answer, and quarantines a misbehaving engine.
- It stayed **single-threaded**: the base implementation is one thread of bespoke C++.

---

## 7. Where to go next

Beyond the base implementation, the same `SynnoDB` object carries each query further:

```python
opt   = db.runOptimLoop(base_impl=impl)            # optimize the single-threaded engine
multi = db.addMultiThreading(optimized=opt)        # NOW it becomes multi-threaded
rep   = db.checkSfCorrectness(source=multi, target_sf=50)   # prove correctness at a larger SF
```

Each is a separate stage method (see [Running stages](../README.md#running-stages)); they chain
in-process by passing the artifact, or across runs via the W&B run id. For the runtime internals - the two
data planes, cross-check, and quarantine - see
[DESIGN_router_dataplane.md](DESIGN_router_dataplane.md) and
[production_hardening_plan.md](production_hardening_plan.md).
