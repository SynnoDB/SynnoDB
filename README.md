# SynnoDB
SynnoDB Repository


`SYNNO_DATA_DIR` must point at the data root (parquet, caches, logs); set it in
the environment or `.env`.

## Python API

```python
from synnodb import SynnoDB

db = SynnoDB.in_memory(workload="tpch", model="anthropic/claude-sonnet-4-6")

plan = db.createStoragePlan(queries="1")     # -> StoragePlan
print(plan.text)                             # the storage_plan.txt document
print(plan.path, plan.run_id)                # on disk + wandb provenance

impl = db.createBaseImpl(storage_plan=plan.text)  # pass the plan content (W&B-free)
print(impl.files["db_loader.cpp"])           # -> BaseImplementation (generated C++)

opt = db.runOptimLoop(base_impl=impl)        # -> OptimizedImplementation
```

Each stage returns a domain object (`StoragePlan`, `BaseImplementation`,
`OptimizedImplementation`, `MultiThreadedImplementation`, `CorrectnessReport`)
that carries the produced artifact and chains into the next stage.
`SynnoDB(...)` takes enums or strings (`db_storage="ssd"`), alternative
constructors (`in_memory`/`on_ssd`/`for_tpch`/`for_ceb`/`from_env`), and
`with_(...)` for per-call overrides.

## Running stages

Every stage is a method on `SynnoDB`; there are no per-stage scripts. Each call
runs one stage to completion and returns an artifact that chains into the next.
Stages chain in-process (pass the artifact) or across runs via the W&B run id
(`*_wandb_id=`, requires `wandb_entity`/`wandb_project` on the producing run):

```python
from synnodb import SynnoDB

db = SynnoDB.on_ssd(
    workload="tpch", queries="1-22", model="anthropic/claude-sonnet-4-6",
    notify=True, wandb_entity="my-entity",   # presence of entity/project enables W&B
)

plan = db.createStoragePlan()                          # -> StoragePlan
impl = db.createBaseImpl(storage_plan_wandb_id="8xn0t04p")   # or storage_plan=plan
opt  = db.runOptimLoop(base_impl_wandb_id="q45vm9fz")        # or base_impl=impl
mt   = db.addMultiThreading(optimized_wandb_id="0br4bjqb")   # or optimized=opt
rep  = db.checkSfCorrectness(source_wandb_id="0br4bjqb", target_sf=50)
```

The run output dir defaults to a local `./output`; set `workspace=` (or
`SYNNO_WORKSPACE`). Any `RunConfig` setting the typed config does not model can be
forced through the `extra_config={...}` escape hatch.

For low-level debugging there is still `python -m synnodb.main manual
--conv_mode <mode> …` (explicit args; not the supported entry point).

## Define your own conversation

The built-in stages are ordinary `ConversationPlan`s; you can assemble and run
your own conversation from the same primitives via `run_synthesis`, the single
entry point every stage goes through:

```python
from synnodb import (
    AssertCorrect, Benchmark, Compact, ConversationPlan, ConvContext,
    PerQueryLoop, PrepareFeatures, PromptStage, SynnoDB,
)

db = SynnoDB.in_memory(workload="tpch", queries="1,4,6")

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
                # runtime and tracing data arrive exactly as in the built-in stages
                get_prompt_with_tracing=lambda _exec_settings, rt, trace: (
                    f"Query {qid} currently runs in {rt:.0f} ms.\n"
                    f"Trace:\n{trace}\nOptimize it."),
                max_turns=125,
                # defaults: measure after stage, auto-revert on regression
            ),
        ]),
        Benchmark(),
    ]

plan = ConversationPlan(
    name="myTuningPass",                    # run identity: naming, logging, caching
    prepare=PrepareFeatures(tracing=True),  # what the workspace must provide
    stages=my_stages,
)
result = db.run_synthesis(plan, start=base_impl)  # start: artifact | snapshot hash | None
```

- `prepare` states *what the workspace must have* (scaffold, tracing
  instrumentation, MT helpers, ...) as independent feature flags; the features
  actually applied are recorded in a git-tracked `.synnodb_prepare.json` inside
  every snapshot, so chained runs know what they start from.
- `stages` receives a `ConvContext` (queries, workspace filenames, run tool,
  lazy helpers like `ctx.reference_plans(source="umbra")`) and returns a flat
  list of stage items. `PerQueryLoop` runs one conversation branch per query,
  feeding each stage the freshly measured runtime and trace data.
- The returned artifact carries the final snapshot hash and the prepare record,
  so it chains into `db.checkSfCorrectness(result, target_sf=100)` or further
  custom plans with no extra ceremony.

Install: `uv sync` (add extras as needed: `uv sync --extra dev --extra viz`).

## Prerequisites

- Linux (x86-64)
- C++ toolchain (`gcc` / `clang`)
- Python 3.10+
- [`uv`](https://github.com/astral-sh/uv) package manager
- Apache Arrow and Parquet development libraries
- [`cloc`](https://github.com/AlDanial/cloc) (used to track generated code size)

## Installation

### Install from PyPI

```bash
pip install synnodb          # or: uv pip install synnodb
```

This installs the `synnodb` package (the pipeline is driven through the `SynnoDB` Python API; see [Running stages](#running-stages)). The Arrow/Parquet system libraries and a C++ toolchain (see Prerequisites) are still required at runtime. For local development from a source checkout, follow the steps below.

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

*Optional:* Setup Weights & Biases (wandb) account and API key for experiment tracking. You can sign up for free at [https://wandb.ai/](https://wandb.ai/).

### 2. Install Arrow and Parquet libraries

```bash
wget https://packages.apache.org/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt install -y -V ./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt update
sudo apt install -y libarrow-dev libparquet-dev parquet-tools
```

### 3. Install `cloc`

```bash
sudo apt install -y cloc
```

### 4. Install Python dependencies

```bash
uv sync
```

Add extras as needed. The engine factory and observability stack (including
`wandb`, required by the standalone dashboard) live in the `factory` extra:

```bash
uv sync --extra factory --extra dev
```

### 5. Configure environment

Create a `.env` file with your API keys:

```bash
OPENAI_API_KEY=...
WANDB_ENTITY=... # Optional, e.g. "my-team"
WANDB_PROJECT=... # Optional, e.g. "bespoke-olap"
```

### 6. Prepare Parquet data

Place TPC-H or CEB Parquet files in your artifacts directory (default: `/mnt/labstore/bespoke_olap/`). The path can be overridden with `--base_parquet_dir`.


## Development

### Inspect running engine processes

```bash
watch -n1 -d ./misc/get_db_procs.sh
```

### Remote snapshot cache (optional)

To share snapshots across machines, set up a bare git repository and start a git daemon:

```bash
git init --bare synno_cache.git
touch synno_cache.git/git-daemon-export-ok

git daemon \
    --base-path=./ \
    --export-all \
    --enable=receive-pack \
    --reuseaddr \
    --verbose
```

The cache URL is `git://<hostname>/synno_cache.git`. Pass it via the `.env` file, or leave it unset to use only the local snapshot cache (with `--disable_repo_sync`).


Delete snapshot:
```
git -C /home/jwehrstein/bespoke_olap/output --git-dir=/home/jwehrstein/bespoke_olap/output/.git update-ref -d
      refs/snapshots/snapshot-<hash>
```
