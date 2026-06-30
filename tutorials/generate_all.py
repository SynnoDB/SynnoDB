"""Headless overnight engine generation for the bring-your-own TPC-H workload.

Builds ONE storage plan covering all 22 queries in ``queries.json``, then ONE base
implementation on top of that single plan, validating against DuckDB and auto-publishing the
engine into ``SYNNO_ENGINES_DIR``.

``SYNNO_SF`` is the *target* scale factor (the engine you want). The framework derives a
small-SF-first validation ladder from it (``register_workload_from_json`` -> ``_derive_sf_ladder``):
correctness is checked cheaply at the smallest scale factors that exist under ``PARQUET_DIR``
first, escalating to the target only once those pass. That is what keeps a bug from costing a
multi-minute target-SF load on every iteration, so ``PARQUET_DIR`` must contain the small SFs
(e.g. ``sf1/``, ``sf2/``) in addition to the target.

If a generated query crashes, the engine's crash handler already prints a symbolized C++ stack
trace; for the exact faulting line + variable, re-run with ``SYNNO_SANITIZE=address`` to build
the engine under AddressSanitizer (small-SF iteration makes its overhead negligible).

Run (in tmux, unattended):
    yes | .venv/bin/python -u tutorials/generate_all.py
``yes |`` auto-confirms the occasional interactive prompt (e.g. model/workspace
confirmations) so the run never blocks on stdin.
"""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

# --- environment ------------------------------------------------------------------------
# MiniMax-M3 is served on 13506; set it explicitly so the run does not depend on the code
# default.
os.environ.setdefault("LLM_API_BASE", "http://dgx02:13506/v1")

DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR", "/mnt/labstore/learneddb/synno_data"))
# Parquet root holding <PARQUET_DIR>/sf<N>/<table>.parquet. Must contain the small SFs used
# for fast validation (sf1/, sf2/) AND the target SF. The default tree below spans the full
# SF range; the canonical learneddb tree currently only has sf1/2/20, so it cannot serve an
# SF50 target until its sf50/ parquet is materialised - override SYNNO_TPCH_PARQUET to a tree
# that has the target SF.
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)

MODEL = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
# Keep an integral SF an int so the parquet dir resolves to "sf50", not "sf50.0".
_SF_RAW = os.environ.get("SYNNO_SF", "50")
SF = int(_SF_RAW) if float(_SF_RAW).is_integer() else float(_SF_RAW)

HERE = Path(__file__).parent
QUERIES_JSON = HERE / "queries.json"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    from synnodb import SynnoDB
    from synnodb.workloads.byo_workload import register_workload_from_json

    log(f"model={MODEL} sf={SF} parquet={PARQUET_DIR} engines={ENGINES_DIR}")

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

    db = SynnoDB(workload="tpch_byo", model=MODEL, db_storage="in_memory", queries=queries)

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
