#!/bin/bash

set -e

wandb_id="m67to2p5"
snapshot_hash="5e2bd438c17b5800feb3daeebab1430846a5ecf5"

do_transfer=false
do_start=false
do_local=false
ssh_target="bespoke_demo"
LOG_DIR="~/bespoke_olap/webrunner_logs"

for arg in "$@"; do
    case "$arg" in
        --transfer) do_transfer=true ;;
        --start)    do_start=true ;;
        --local)    do_local=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

if ! $do_transfer && ! $do_start; then
    echo "WARNING: No action specified. Use --transfer to upload code, --start to restart services." >&2
    exit 0
fi

# Run a shell command either locally (eval) or on the remote host (ssh).
run_cmd() {
    if $do_local; then
        eval "$1"
    else
        ssh $ssh_target "$1"
    fi
}

setup_geoip() {
    run_cmd "cd ~/bespoke_olap && uv sync"
    run_cmd "cd ~/bespoke_olap && mkdir -p demo_and_analysis/ui_template_runner/output && ym=\$(date +%Y-%m) && curl -fsSL \"https://download.db-ip.com/free/dbip-city-lite-\${ym}.mmdb.gz\" | gunzip > demo_and_analysis/ui_template_runner/output/GeoLite2-City.mmdb && echo 'Updated $GEOIP_DB (DB-IP Lite)'"
}

if $do_transfer; then
    if $do_local; then
        read -rp "Install code locally? [y/N] " confirm
    else
        read -rp "Upload code to $ssh_target? [y/N] " confirm
    fi
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

    set -x

    # create zip file
    python "$(dirname "$0")/prepare_code_zip.py" tpch "$wandb_id" --snapshot_hash "$snapshot_hash"

    # create target dir if it doesn't exist
    target_dir="~/bespoke_olap/demo_and_analysis/ui_template_runner/output/"
    run_cmd "mkdir -p $target_dir"

    if $do_local; then
        cp "$(dirname "$0")/$wandb_id.zip" "$(dirname "$0")/code_metadata.json" ~/bespoke_olap/demo_and_analysis/ui_template_runner/output/
    else
        scp "$(dirname "$0")/$wandb_id.zip" "$(dirname "$0")/code_metadata.json" "$ssh_target:$target_dir"
    fi

    # unzip
    run_cmd "unzip -o ${target_dir}${wandb_id}.zip -d $target_dir"

    setup_geoip

    set +x
fi

if $do_start; then
    if $do_local; then
        read -rp "Tear down existing tmux sessions and start services locally? [y/N] " confirm
    else
        read -rp "Tear down existing sessions and start services on $ssh_target? [y/N] " confirm
    fi
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

    set -x

    sf=10
    benchmark="tpch"
    DATETIME=$(date +%Y%m%d_%H%M%S)

    # teardown all tmux sessions (if any)
    run_cmd "tmux kill-server 2>/dev/null || true"

    # wait for 5s
    sleep 5

    # create log directory
    run_cmd "mkdir -p $LOG_DIR"
    setup_geoip

    # start all services in separate tmux sessions, piping output to log files
    run_cmd "tmux new-session -d -s umbra 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/umbra_service.py $benchmark --sf $sf --port 7655 --base-parquet-dir ~/ 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_umbra.log'"
    # run_cmd "tmux new-session -d -s clickhouse 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/clickhouse_service.py $benchmark --sf $sf --port 7656 --base-parquet-dir ~/ 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_clickhouse.log'"
    run_cmd "tmux new-session -d -s bespoke 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/bespoke_service.py $benchmark --sf $sf --port 7657 --base-parquet-dir ~/ 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_bespoke.log'"
    # Profiled bespoke instance: a dedicated workspace copy compiled with -DTRACE
    # (sources only — build artifacts/results are rebuilt) serving the PROFILE breakdown.
    run_cmd "rsync -a --delete --exclude build/ --exclude db --exclude results/ ~/bespoke_olap/demo_and_analysis/ui_template_runner/output/ ~/bespoke_olap/demo_and_analysis/ui_template_runner/output_trace/"
    run_cmd "tmux new-session -d -s bespoke_profiled 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/bespoke_service.py $benchmark --sf $sf --port 7658 --trace --workspace-dir demo_and_analysis/ui_template_runner/output_trace --base-parquet-dir ~/ 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_bespoke_profiled.log'"
    run_cmd "tmux new-session -d -s ui 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/run_generated_code_service.py $benchmark --bespoke http://127.0.0.1:7657 --bespoke_profiled http://127.0.0.1:7658 --sf $sf --duckdb --umbra http://127.0.0.1:7655 --base-parquet-dir ~/ --port 80 --cert /etc/letsencrypt/live/130.83.40.96.sslip.io/fullchain.pem --key /etc/letsencrypt/live/130.83.40.96.sslip.io/privkey.pem 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_ui.log'"
    run_cmd "tmux new-session -d -s telemetry 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/telemetry/telemetry_service.py 2>&1 | tee -a ${LOG_DIR}/${DATETIME}_telemetry.log'"

    if $do_local; then
        echo "Logs → $LOG_DIR/${DATETIME}_*.log"
    else
        echo "Logs → $ssh_target:$LOG_DIR/${DATETIME}_*.log"
    fi

    set +x
fi
