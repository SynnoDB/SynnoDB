# SynnoDB
SynnoDB Repository


## CMDs at the moment:

```
# gen storage plan
python -m synnodb.run_gen_storage_plan --model anthropic/claude-sonnet-4-6 --queries 1-22 --benchmark tpch --auto_finish --disable_openai_tracing --notify --db_storage ssd --auto_u

# run gen base
python -m synnodb.run_gen_base_impl --model anthropic/claude-sonnet-4-6 --benchmark tpch --bespoke_storage --storage_plan_run_id 8xn0t04p --queries 1-22 --auto_finish --disable_openai_tracing --notify --db_storage ssd --auto_u

# run optim
python -m synnodb.run_optim_loop --model anthropic/claude-sonnet-4-6 --benchmark tpch --bespoke_storage --base_impl_run_id q45vm9fz --queries 1-22 --disable_openai_tracing --auto_u --auto_finish --notify --db_storage ssd

# test correctness at larger SF
python -m synnodb.run_check_sf_correctness --model anthropic/claude-sonnet-4-6 --benchmark tpch --bespoke_storage --source_run_id 0br4bjqb --queries 1-22 --disable_openai_tracing --auto_u --auto_finish --notify --db_storage ssd --target_sf 50

# add multi-threading
python -m synnodb.run_add_multi_threading --model anthropic/claude-sonnet-4-6 --benchmark tpch --bespoke_storage --optim_run_id 0br4bjqb --queries 1-22 --disable_openai_tracing --auto_u --auto_finish --notify --db_storage ssd
```

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

This installs the `synnodb` package and the `synnodb-*` console scripts listed above. The Arrow/Parquet system libraries and a C++ toolchain (see Prerequisites) are still required at runtime. For local development from a source checkout, follow the steps below.

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
