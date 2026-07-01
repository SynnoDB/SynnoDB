"""Tutorial 3 / full run: generate a bespoke engine for ALL 22 TPC-H queries at SF50, 64 threads.

This is the top of the tutorial ladder. Tutorials 1 and 2 build up to it:
  - 01_quickstart.ipynb  - one query (Q5) at SF2, single-threaded, in a notebook. The 5-minute taste.
  - 02_explore_threads.py - Q1-5 at SF20, introduces the `threads` knob (0 = all cores, 8 = eight).
  - 03_full_run.py (this) - the real thing: all 22 queries, SF50, designed and served at 64 threads.

Unlike the first two, this is a long, unattended run (hours). Start it in tmux and walk away:

    tmux new -s synno
    yes | .venv/bin/python -u tutorials/03_full_run.py
    # detach with Ctrl-b d ; reattach with: tmux attach -t synno

`yes |` auto-confirms the occasional interactive prompt (model / workspace confirmation) so the
run never blocks on stdin. `-u` keeps stdout unbuffered so the tmux log stays live.

It builds ONE storage plan covering all 22 queries, then ONE base implementation on top of that
plan, validating every query's output against DuckDB on a small-scale-factor-first ladder before
the expensive SF50 pass, and auto-publishes the engine into SYNNO_ENGINES_DIR.
"""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

# --- environment ------------------------------------------------------------------------
# MiniMax-M3 is served on 13506; set it explicitly so the run does not depend on the code default.
os.environ.setdefault("LLM_API_BASE", "http://dgx02:13506/v1")

DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR", "/mnt/labstore/learneddb/synno_data"))
# Parquet root holding <PARQUET_DIR>/sf<N>/<table>.parquet. Must contain the small SFs used for
# fast validation (sf1/, sf2/) AND the target SF (sf50/). The framework validates cheaply at the
# small scale factors first and only escalates to the target once those pass.
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)

MODEL = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
# Keep an integral SF an int so the parquet dir resolves to "sf50", not "sf50.0".
_SF_RAW = os.environ.get("SYNNO_SF", "50")
SF = int(_SF_RAW) if float(_SF_RAW).is_integer() else float(_SF_RAW)

# Generation knobs (DuckDB-style config with defaults) - edit these two lines to change them.
# threads: the degree of parallelism the engine is designed, validated, and served at.
#   None/unset -> 1 (single-threaded), 0 -> every usable core on this machine, N -> N threads.
# max_turns: per-stage LLM turn budget for every stage that does not set its own override.
THREADS = 64
MAX_TURNS = 500

HERE = Path(__file__).parent
QUERIES_JSON = HERE / "queries.json"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    from synnodb import SynnoDB
    from synnodb.workloads.byo_workload import register_workload_from_json

    log(f"model={MODEL} sf={SF} threads={THREADS} max_turns={MAX_TURNS} "
        f"parquet={PARQUET_DIR} engines={ENGINES_DIR}")

    # Pass only the target SF; the framework derives the small-SF-first validation ladder
    # (e.g. (1, 2, 50)) from the scale factors available under PARQUET_DIR.
    spec = register_workload_from_json(
        name="tpch_byo",
        queries_json=QUERIES_JSON,
        parquet_dir=PARQUET_DIR,
        scale_factors=(SF,),
        schema_example_table="lineitem",
    )
    query_ids = list(spec.all_query_ids)
    queries = f"{query_ids[0]}-{query_ids[-1]}"  # "1-22"
    log(f"registered tpch_byo with {len(query_ids)} queries: {query_ids}")

    db = SynnoDB(
        workload="tpch_byo",
        model=MODEL,
        db_storage="in_memory",
        queries=queries,
        threads=THREADS,
        max_turns=MAX_TURNS,
    )

    t_start = time.time()
    try:
        log(f"===== STORAGE PLAN for queries {queries} START =====")
        t0 = time.time()
        plan = db.createStoragePlan()
        log(f"===== STORAGE PLAN DONE in {time.time() - t0:.0f}s  run={plan.run_id} =====")

        log("===== BASE IMPL (all queries on the one plan) START =====")
        t0 = time.time()
        impl = db.createBaseImpl(storage_plan=plan.text)
        log(f"===== BASE IMPL DONE in {time.time() - t0:.0f}s  workspace={impl.workspace} =====")

        log(f"ALL DONE in {(time.time() - t_start) / 3600:.1f}h")
        log(f"engine published under: {ENGINES_DIR}")
    except Exception:
        log(f"FAILED after {(time.time() - t_start) / 3600:.1f}h")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
