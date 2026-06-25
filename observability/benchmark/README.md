# Benchmarking

Benchmark execution is implemented in `benchmark/` and can be run directly as a module:

```bash
python -m observability.benchmark run --system duckdb --scale_factors 1,5,20 --benchmark tpch --csv logs/duckdb.csv
```

## Use-cases (`--usecase`)

The benchmarker drives two stacks through the same workload-provider plumbing,
selected with `--usecase`:

| `--usecase` | Engine (`bespoke`) | Reference systems | Benchmarks |
|---|---|---|---|
| `olap` (default) | in-DB OLAP engine | `duckdb`, `umbra`, `clickhouse` | `tpch`, `ceb` |
| `bff` | bespoke **file format** engine (reads `.bff`) | `duckdb` (on parquet) | `tpch`, `tpch_st` |

For `--usecase bff` only `bespoke` and `duckdb` are valid systems (umbra/clickhouse
cannot read the bespoke file format). BFF is always disk-backed; supply
`--memory_budget_mb` to bound the generated engine's RAM.

## Common Commands

Run Bespoke for selected query IDs (identify the engine by snapshot hash):

```bash
python -m demo_and_analysis.benchmark run --system bespoke --snapshots <hash> --scale_factors 1 --query_ids 1,2 --benchmark tpch --csv logs/bespoke.csv
```

Instead of a snapshot hash, you can point at one or more wandb run IDs; the snapshot hash is resolved from each run:

```bash
python -m demo_and_analysis.benchmark run --system bespoke --wandb_ids <run_id> --scale_factors 1 --benchmark tpch --csv logs/bespoke.csv
```

Run any system with varying thread counts (1, 4, and 8 threads):

```bash
python -m demo_and_analysis.benchmark run --system bespoke --snapshots <hash> --scale_factors 1,10 --num_threads 1,4,8 --benchmark tpch --csv logs/bespoke_mt.csv
python -m demo_and_analysis.benchmark run --system duckdb --scale_factors 1,10 --num_threads 1,4,8 --benchmark tpch --csv logs/duckdb_mt.csv
python -m demo_and_analysis.benchmark run --system umbra --scale_factors 1,10 --num_threads 1,4,8 --benchmark tpch --csv logs/umbra_mt.csv
python -m demo_and_analysis.benchmark run --system clickhouse --scale_factors 1,10 --num_threads 1,4,8 --benchmark tpch --csv logs/clickhouse_mt.csv
```

Run DuckDB (no snapshots required):

```bash
python -m demo_and_analysis.benchmark run --system duckdb --scale_factors 1,5,20 --benchmark tpch --csv logs/duckdb.csv
```

Run Umbra or ClickHouse separately:

```bash
python -m demo_and_analysis.benchmark run --system umbra --scale_factors 1,5,20 --benchmark tpch --csv logs/umbra.csv
python -m demo_and_analysis.benchmark run --system clickhouse --scale_factors 1,5,20 --benchmark tpch --csv logs/clickhouse.csv
```

Combine multiple benchmark logs into one plot (`--x` is required):

```bash
python -m demo_and_analysis.benchmark plot logs/bespoke.csv logs/duckdb.csv logs/umbra.csv --x scale_factor
```

`--x` selects what appears on the x-axis:

| `--x` value | Plot type | Use case |
|---|---|---|
| `scale_factor` | Line chart — median time vs. scale factor | Compare systems across data sizes |
| `num_threads` | Line chart — median time vs. thread count | Thread-scaling / parallelism analysis |
| `query_id` | Horizontal bar chart — per-query timings | Identify per-query winners/losers |

```bash
# scale factor on x-axis
python -m demo_and_analysis.benchmark plot logs/bespoke.csv logs/duckdb.csv --x scale_factor --output plots/tpch.png

# thread-scaling plot (requires logs captured with --num_threads)
python -m demo_and_analysis.benchmark plot logs/bespoke_mt.csv logs/duckdb_mt.csv --x num_threads --output plots/tpch_mt.png

# per-query breakdown
python -m demo_and_analysis.benchmark plot logs/*.csv --x query_id --output plots/tpch_queries.png
```

## Notes

### `run`

- Run one system per command with `--system` (e.g. `bespoke`, `duckdb`, `umbra`, `clickhouse`). `--systems` is a deprecated alias.
- For `--system bespoke`, provide either `--snapshots` (comma-separated commit hashes) or `--wandb_ids` (comma-separated wandb run IDs), but not both. Other systems need neither.
- The output CSV must not already exist. Without `--csv`, results are written to `<artifacts_dir>/benchmark_logs/<date>_<time>_<system>_<benchmark>.csv`.
- The CSV always includes a `num_threads` column. Columns: `query_id, scale_factor, benchmark, system, num_threads, time_ms, hostname, snapshot`.
- Query IDs are resolved from benchmark definitions (`tpch` or `ceb`), not from snapshot files. `--query_ids all` (or omitting it) runs every query.
- `--instantiations` controls how many distinct query parameter sets (different random seeds) are generated; `--repetitions` repeats each instantiation's SQL for timing stability.
- `--num_threads` accepts a comma-separated list (e.g. `1,4,8`). All systems support it.
  - **Bespoke**: pins to cores `0..N-1` by sending `CORE_IDS` as per-run hotpatch environment.
  - **DuckDB**: sets `PRAGMA threads=N`; single-thread runs also pin the worker process to one core.
  - **ClickHouse / Umbra**: the Docker container is restarted with `--cpus N --cpuset-cpus 0-(N-1)` for each thread count. ClickHouse uses the Memory engine (data not persisted across restarts, so it is reloaded); Umbra persists data in its volume.

### `plot`

- `--x` is required and must be one of `scale_factor`, `num_threads`, `query_id`. (`--by-query` is a deprecated alias for `--x query_id`.)
- Without `--output`, the plot is written to `plots/<date>_<time>_<x>.png`.
- Input CSVs must contain the benchmark columns written by `run`; multiple logs are concatenated before plotting.
- For `--x num_threads`: `--max-threads N` drops rows above a thread count, and `--legend-pos {up,top,bottom}` controls legend placement.
- `--product-plot` uses a larger, presentation-oriented visual style (otherwise a compact paper style is used).
