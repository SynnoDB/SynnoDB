# BespokeOLAP GUI

Serves the generated DBMS, runs queries on it, and compares results against baseline engines (DuckDB, Umbra, ClickHouse).

## Deploying to a remote server (recommended)

Use the deploy script to transfer code and restart all services in one step:

```bash
# Upload code to bespoke_demo (sets wandb_id and snapshot_hash in the script first)
bash demo_and_analysis/ui_template_runner/deploy_code/deploy.sh --transfer

# Tear down existing sessions and start all services on bespoke_demo
bash demo_and_analysis/ui_template_runner/deploy_code/deploy.sh --start

# Or do both at once
bash demo_and_analysis/ui_template_runner/deploy_code/deploy.sh --transfer --start
```

`--transfer` zips the generated code and copies it to `bespoke_demo:~/bespoke_olap/demo_and_analysis/ui_template_runner/output/`.

`--start` kills any existing tmux sessions and starts four named sessions: `umbra`, `bespoke`, `ui`, and `telemetry`. Each session's output is written to `~/bespoke_olap_logs/<datetime>_<service>.log` on the remote. The exact log path is printed at the end.

`deploy.sh` automatically downloads the [DB-IP City Lite](https://db-ip.com/db/lite.php#how-to-use) database (free, no account required) into `output/GeoLite2-City.mmdb` for local IP geolocation in the telemetry dashboard.

To attach to a running session: `ssh bespoke_demo -t tmux attach -t ui`

To tail logs: `ssh bespoke_demo "tail -f ~/bespoke_olap_logs/*_ui.log"`

## Prerequisites

### Parquet data

The services expect a folder structure like:

```
base_parquet_dir/
    tpch_parquet/
        sf10/
            lineitem.parquet
            part.parquet
            ...
```

### Code snapshot (if no access to the git backup server)

Run `prepare_code_zip.py` to package the generated code from a W&B run:

```bash
python demo_and_analysis/ui_template_runner/prepare_code_zip.py tpch <wandb_run_id>
```

If the git backup server is reachable, the services can load a snapshot directly via `--start_snapshot`.

## Starting services manually

Run each service in a separate terminal (or tmux pane):

```bash
# 1. Umbra baseline
python demo_and_analysis/ui_template_runner/umbra_service.py tpch --sf 10 --port 7655 --base-parquet-dir ~/
```
add `--disk_based` if you want to use disk-based mode.


# 2. BespokeOLAP engine
python demo_and_analysis/ui_template_runner/bespoke_service.py tpch --sf 10 --port 7657 --base-parquet-dir ~/

# 3. ClickHouse baseline (optional — slow)
python demo_and_analysis/ui_template_runner/clickhouse_service.py tpch --sf 10 --port 7656 --base-parquet-dir ~/

# 4. UI + query runner (enable engines with flags)
python demo_and_analysis/ui_template_runner/run_generated_code_service.py tpch \
    --bespoke http://127.0.0.1:7657 \
    --umbra http://127.0.0.1:7655 \
    --duckdb \
    --sf 10 \
    --base-parquet-dir ~/ \
    --port 80

# 5. System telemetry (optional — logs CPU/mem to stdout every 10 s)
python demo_and_analysis/ui_template_runner/telemetry_service.py
```

Open the GUI at `http://<IP>:80/ui`.

## Logs and telemetry

All services emit structured log lines to stdout. Key prefixes:

| Prefix | Where | What |
|---|---|---|
| `TELEMETRY` | `bespoke_service`, `umbra_service`, `run_generated_code_service` | Per-query latency: `run_id=`, `engine=`, `query=`, `time_ms=`, `sf=` |
| `SYSSTAT` | `telemetry_service` | Memory and load average every 10 s |
| `PROCSTAT` | `telemetry_service` | Per-process CPU/mem for each service process |

Quick grep examples:

```bash
# All query latencies from a run
grep TELEMETRY ~/bespoke_olap_logs/<datetime>_*.log

# Bespoke vs DuckDB latencies side by side
grep 'TELEMETRY engine=bespoke\|TELEMETRY engine=duckdb' ~/bespoke_olap_logs/<datetime>_ui.log

# Memory over time
grep SYSSTAT ~/bespoke_olap_logs/<datetime>_telemetry.log
```
