sf=10
benchmark="tpch"
parquet_dir="/mnt/labstore/bespoke_olap/"

tmux new-session -d -s umbra 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/umbra_service.py $benchmark --sf $sf --port 7655 --base-parquet-dir $parquet_dir'
# ssh bespoke_demo "tmux new-session -d -s clickhouse 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/clickhouse_service.py $benchmark --sf $sf --port 7656 --base-parquet-dir ~/'"
tmux new-session -d -s bespoke 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/bespoke_service.py $benchmark --sf $sf --port 7657 --base-parquet-dir $parquet_dir'
tmux new-session -d -s ui 'cd ~/bespoke_olap && source .venv/bin/activate && python demo_and_analysis/ui_template_runner/run_generated_code_service.py $benchmark --bespoke http://127.0.0.1:7657 --sf $sf --duckdb --umbra http://127.0.0.1:7655 --base-parquet-dir $parquet_dir --port 80'