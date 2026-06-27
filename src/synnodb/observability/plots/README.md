# Plot Classification Scripts

This directory contains the LLM-backed classifiers used to summarize strategies
in generated Bespoke OLAP engines:

- `classify_bespoke_storage/classify_storage.py`
- `classify_bespoke_execution/classify_execution.py`
- `classify_bespoke_multi_threading/classify_multi_threading.py`

Each script takes a W&B run id, resolves the run's `code/snapshot_hash`,
restores that code snapshot into `demo_and_analysis/output`, asks an LLM to
classify the generated C++ files, and writes timestamped result files next to
the script.

## Prerequisites

Run commands from the repository root.

```bash
uv sync
source .venv/bin/activate
export PYTHONPATH="$PWD"
```

Set API credentials in the environment or in the repo-root `.env` file:

```bash
OPENAI_API_KEY=...
WANDB_API_KEY=...
```

The scripts currently use the W&B defaults in
`utils/logging_and_reporting/wandb_api_helper.py`:

- entity: `learneddb`
- project: `bespoke-olap-internal`

They also restore snapshots from `git://c01/bespoke_cache.git`, so that cache
must be reachable from the machine running the classifiers.

## Run One Classifier

Replace `<wandb_run_id>` with the run that produced the engine snapshot you want
to analyze. Set `--benchmark` to the benchmark name used in the W&B run name;
the default is `ceb`.

```bash
python demo_and_analysis/plots/classify_bespoke_storage/classify_storage.py \
  --wandb_id <wandb_run_id> \
  --benchmark tpch
```

```bash
python demo_and_analysis/plots/classify_bespoke_execution/classify_execution.py \
  --wandb_id <wandb_run_id> \
  --benchmark tpch
```

```bash
python demo_and_analysis/plots/classify_bespoke_multi_threading/classify_multi_threading.py \
  --wandb_id <wandb_run_id> \
  --benchmark tpch
```

Although `--wandb_id` is marked optional by `argparse`, it is required in normal
use because the scripts fetch the snapshot hash from W&B before classifying.

## Run All Three

```bash
export RUN_ID=<wandb_run_id>
export BENCHMARK=tpch

python demo_and_analysis/plots/classify_bespoke_storage/classify_storage.py \
  --wandb_id "$RUN_ID" --benchmark "$BENCHMARK"

python demo_and_analysis/plots/classify_bespoke_execution/classify_execution.py \
  --wandb_id "$RUN_ID" --benchmark "$BENCHMARK"

python demo_and_analysis/plots/classify_bespoke_multi_threading/classify_multi_threading.py \
  --wandb_id "$RUN_ID" --benchmark "$BENCHMARK"
```

## Outputs

Storage classification writes to
`demo_and_analysis/plots/classify_bespoke_storage/results/`:

- `<timestamp>_<benchmark>_column_strategies.json`
- `<timestamp>_<benchmark>_query_strategies.json`
- `<timestamp>_<benchmark>_strategy_usage.csv`
- `<timestamp>_<benchmark>_strategy_usage.png`
- `<timestamp>_<benchmark>_strategy_usage.pdf`

Execution classification writes to
`demo_and_analysis/plots/classify_bespoke_execution/results/`:

- `<timestamp>_<benchmark>_execution_strategies.json`
- `<timestamp>_<benchmark>_execution_strategy_usage.csv`
- `<timestamp>_<benchmark>_execution_strategy_usage.png`
- `<timestamp>_<benchmark>_execution_strategy_usage.pdf`

Multi-threading classification writes to
`demo_and_analysis/plots/classify_bespoke_multi_threading/results/`:

- `<timestamp>_<benchmark>_multi_threading_strategies.json`
- `<timestamp>_<benchmark>_multi_threading_strategy_usage.csv`
- `<timestamp>_<benchmark>_multi_threading_strategy_usage.png`
- `<timestamp>_<benchmark>_multi_threading_strategy_usage.pdf`

LLM responses are cached. The storage classifier sets its cache to
`demo_and_analysis/plots/classify_bespoke_storage/llm_cache`; the execution and
multi-threading classifiers use `llm_cache` relative to the current working
directory unless changed in code.

## What The Scripts Read

After restoring the snapshot, the scripts expect generated C++ files under
`demo_and_analysis/output`, including:

- `db_loader.hpp`
- `db_loader.cpp`
- `storage_plan.txt` for storage classification
- `thread_pool.hpp` for multi-threading classification
- `query_q*.cpp` query files

If a restored snapshot uses a different query filename convention, the scripts
will not find queries until `read_query_file()` and the `OUTPUT_DIR.glob(...)`
pattern are updated to match.

## Cost Prompts

Each classifier estimates token usage before calling the model. Calls under the
per-call budget in the script run without confirmation; larger estimated calls
pause and ask for confirmation in the terminal. Cached responses do not call the
model again.
