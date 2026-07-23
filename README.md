<div align="center">

<img src="misc/logo-light.png" alt="SynnoDB" width="440">

<br>

**A drop-in replacement for DuckDB that transparently accelerates your SQL with auto-generated
bespoke C++ engines** - falling back to DuckDB for everything else, cross-checked for correctness.

[![PyPI](https://img.shields.io/pypi/v/synnodb.svg?color=blue)](https://pypi.org/project/synnodb/)
[![Python](https://img.shields.io/pypi/pyversions/synnodb.svg)](https://pypi.org/project/synnodb/)
[![License](https://img.shields.io/badge/license-Polyform_Non_Commercial-green.svg)](LICENSE)
[![Paper](https://img.shields.io/badge/paper-VLDB-b31b1b.svg)](https://arxiv.org/pdf/2603.02001)
[![Website](https://img.shields.io/badge/website-synnodb.com-1f6feb.svg)](https://synnodb.com)

[🌐 Website](https://synnodb.com) &nbsp;·&nbsp;
[📄 Paper](https://arxiv.org/pdf/2603.02001) &nbsp;·&nbsp;
[📦 PyPI](https://pypi.org/project/synnodb/) &nbsp;·&nbsp;
[📓 Demo Notebook](tutorials/gen_tpch_demo.ipynb)

*Required Notice: Copyright 2026 SynnoDB*
</div>


---

SynnoDB grew out of the research project [**Bespoke-OLAP**](https://github.com/DataManagementLab/BespokeOLAP)
([paper](https://arxiv.org/pdf/2603.02001)): an LLM agent that synthesizes workload-specific,
one-size-fits-one C++ query engines. SynnoDB packages that idea as a production-ready DuckDB drop-in.

Install from **[PyPI](https://pypi.org/project/synnodb/)**:

```bash
pip install synnodb              # the demo DuckDB drop-in router
pip install "synnodb[factory]"   # + the Bespoke-Agent factory that generates engines
```

New here? [`tutorials/gen_tpch_demo.ipynb`](tutorials/gen_tpch_demo.ipynb) runs the whole loop end
to end - generate TPC-H data, build an engine, and drop it in against DuckDB. See
[Installation](#installation) for the system libraries the generated engines compile against.

`SYNNO_DATA_DIR` must point at the data root (parquet, caches, logs); set it in
the environment or `.env`.

## Python API

```python
from synnodb import SynnoDB

db = SynnoDB.in_memory(workload="tpch", model="anthropic/claude-sonnet-4-6",
                       query_subset="1")  # default: every registered query

plan = db.createStoragePlan()                # -> StoragePlan
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
    workload="tpch", model="anthropic/claude-sonnet-4-6",
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

## Define your own conversation

The built-in stages are ordinary `ConversationPlan`s; you can assemble and run
your own conversation from the same primitives via `run_synthesis`, the single
entry point every stage goes through:

```python
from synnodb import (
    AssertCorrect, Benchmark, Compact, ConversationPlan, ConvContext,
    PerQueryLoop, PrepareFeatures, PromptStage, SynnoDB,
)

db = SynnoDB.in_memory(workload="tpch", query_subset="1-6")

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

## Installation

SynnoDB is published on **[PyPI](https://pypi.org/project/synnodb/)**, so a single `pip` command
pulls in every Python dependency - no need to manage them yourself:

```bash
pip install synnodb              # the DuckDB drop-in router / runtime
pip install "synnodb[factory]"   # + the LLM factory that generates engines
```

That is everything needed to `import synnodb`. Two things live outside the wheel:

### 1. System libraries

The generated engines are compiled C++ against Apache Arrow / Parquet, so a toolchain and the dev
headers must be present. [`cloc`](https://github.com/AlDanial/cloc) is optional - the factory uses
it to report generated-code size. On Debian/Ubuntu:

```bash
sudo apt install -y build-essential cloc                # C++ compiler (+ optional cloc)

# Apache Arrow + Parquet development libraries
wget https://packages.apache.org/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt install -y -V ./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb
sudo apt update
sudo apt install -y libarrow-dev libparquet-dev parquet-tools
```

(Linux x86-64, Python 3.13+.)

### 2. Configure environment

Create a `.env` in your working directory with the model credentials (and optional run tracking):

```bash
ANTHROPIC_API_KEY=...            # for the default anthropic/... models
# OPENROUTER_API_KEY=...         # for openrouter/... models
# LLM_API_BASE=http://host:PORT/v1   # a self-hosted, OpenAI-compatible endpoint
# WANDB_ENTITY=...  WANDB_PROJECT=... # optional Weights & Biases run tracking
```

Point `SYNNO_DATA_DIR` at the data root that holds the parquet, caches, and published engines.
The CLI and API require it - export it, put it in `.env`, or pass `data_dir=...` to `SynnoDB(...)`.
The [demo notebook](tutorials/gen_tpch_demo.ipynb) is self-contained: it defaults to a
project-local `.synno_data/` when the variable is unset and generates its own TPC-H parquet, so
there is nothing else to configure or download to run it.


## Development

Building from a source checkout (for contributors) uses
[`uv`](https://github.com/astral-sh/uv) instead of `pip` - it manages the virtualenv and the
optional-dependency extras:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # install uv
git clone https://github.com/JWehrstein/SynnoDB.git
cd SynnoDB
uv sync --extra factory --extra dev                # editable install: factory + test deps
```

The extras map to `pyproject.toml`: `factory` (the engine-generation stack plus the standalone
dashboard's `wandb`), `dev` (pytest), `notebook` (Jupyter kernel + nbformat), and `benchmark`
(the ClickHouse comparison). Install only what you need, e.g. `uv sync` for the runtime alone or
`uv sync --extra factory` to generate engines. Still install the [system libraries](#1-system-libraries)
above. Run the test suite with `.venv/bin/python -m pytest`.

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
