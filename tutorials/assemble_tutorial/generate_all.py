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
    yes | .venv/bin/python -u tutorials/assemble_tutorial/generate_all.py
``yes |`` auto-confirms the occasional interactive prompt (e.g. model/workspace
confirmations) so the run never blocks on stdin.

Optional serving-plane check:
    SYNNO_SHM_IPC_CHECK=1 yes | .venv/bin/python -u tutorials/assemble_tutorial/generate_all.py

This runs every generated query through the production ``ShmHotLoadEngine`` after base
implementation: Arrow tables are staged under ``/dev/shm`` (``SYNNODB_SHM_INGEST``), the
compiled ``./db`` is driven through the warm HotpatchProc IPC protocol, and each Arrow IPC
result is compared to DuckDB. Use ``SYNNO_SHM_IPC_INSTANTIATIONS`` to test more concrete
parameter bindings per query.
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
PARQUET_DIR = Path(
    os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet")
)
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)

MODEL = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
# Keep an integral SF an int so the parquet dir resolves to "sf50", not "sf50.0".
_SF_RAW = os.environ.get("SYNNO_SF", "50")
SF = int(_SF_RAW) if float(_SF_RAW).is_integer() else float(_SF_RAW)
SHM_IPC_CHECK = os.environ.get("SYNNO_SHM_IPC_CHECK", "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
    "off",
)
SHM_IPC_INSTANTIATIONS = int(os.environ.get("SYNNO_SHM_IPC_INSTANTIATIONS", "1"))

# Generation knobs, passed as SynnoConfig fields (DuckDB-style config options with
# defaults) - edit these two lines to change them.
# threads: target degree of parallelism the generated engine is designed, validated, and
# served at (config default without this is "all usable cores of this machine").
# max_turns: per-stage LLM turn budget for every stage that doesn't set its own explicit
# override (config default without this is each conversation's own default).
THREADS = 64
MAX_TURNS = 500

HERE = Path(__file__).parent
QUERIES_JSON = HERE.parent / "tpch_queries.json"  # tutorials/tpch_queries.json


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _sql_lit(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _require_shm_loader(workspace: Path) -> None:
    reader = workspace / "parquet_reader.cpp"
    try:
        text = reader.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"cannot verify shm-ingest support; missing {reader}"
        ) from exc
    if "shm_ingest_enabled" not in text or "SYNNODB_SHM_INGEST" not in text:
        raise RuntimeError(
            f"{workspace} was not generated with the shm-ingest loader branch; "
            "cannot run the SHM/IPC serving-plane check"
        )


def run_shm_ipc_check(workspace: Path, spec, query_ids: list[str]) -> None:
    """Exercise all generated queries through the serving shm + IPC path.

    Generation itself validates ``./db <sf-dir>`` over the parquet plane. This helper tests the
    separate serving path: the target SF tables are read as Arrow from DuckDB, staged into
    ``/dev/shm`` for the loader, queries are sent to the warm C++ process via HotpatchProc, and
    exact Arrow IPC results are compared against DuckDB.
    """
    import duckdb

    from synnodb.router.adapt import results_diff, results_equal
    from synnodb.router.normalize import has_order_by, order_by_key_indices
    from synnodb.router.process_engine import ShmHotLoadEngine
    from synnodb.tools.run_tool_mode import RunToolMode
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

    _require_shm_loader(workspace)

    provider = OLAPWorkloadProvider(
        benchmark=spec.name,
        base_parquet_dir=PARQUET_DIR,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=query_ids,
        num_instantiations=SHM_IPC_INSTANTIATIONS,
    )
    provider.set_benchmark_sf(SF)
    provider.set_benchmark_instantiations(SHM_IPC_INSTANTIATIONS)
    provider.set_benchmark_repetitions(1)
    batches = provider.produce_workload(
        run_mode=RunToolMode.BENCHMARK,
        query_ids=query_ids,
        num_threads=1,
        core_ids=None,
    )
    if len(batches) != 1:
        raise RuntimeError(
            f"expected one benchmark batch for shm check, got {len(batches)}"
        )
    batch = batches[0]

    con = duckdb.connect(":memory:")
    try:
        log(
            f"loading target SF tables into DuckDB views from {batch.exec_settings.parquet_dir}"
        )
        for table in spec.tables:
            parquet = batch.exec_settings.parquet_dir / f"{table}.parquet"
            if not parquet.exists():
                raise FileNotFoundError(
                    f"missing parquet table for shm check: {parquet}"
                )
            con.execute(
                f"CREATE VIEW {_quote_ident(table)} AS "
                f"SELECT * FROM read_parquet({_sql_lit(parquet)})"
            )

        with ShmHotLoadEngine("generate-all-shm-ipc-check", workspace) as engine:
            log(f"staging {len(spec.tables)} table(s) into /dev/shm for engine ingest")
            engine.ingest(
                {
                    table: con.execute(
                        f"SELECT * FROM {_quote_ident(table)}"
                    ).to_arrow_table()
                    for table in spec.tables
                }
            )

            total = len(batch.query_list)
            for idx, entry in enumerate(batch.query_list, start=1):
                log(f"shm/ipc check {idx}/{total}: Q{entry.query_id}")
                expected = con.execute(entry.sql).to_arrow_table()
                got = engine.run(entry.query_id, entry.placeholders)
                ordered = has_order_by(entry.sql)
                order_keys = (
                    order_by_key_indices(entry.sql, expected.column_names)
                    if ordered
                    else None
                )
                if not results_equal(
                    got, expected, ordered=ordered, order_keys=order_keys
                ):
                    diffs, diff_total = results_diff(
                        got, expected, ordered=ordered, order_keys=order_keys
                    )
                    raise RuntimeError(
                        f"SHM/IPC mismatch for Q{entry.query_id}: {diff_total} differing "
                        f"cell(s)/row(s), first diffs: {diffs[:5]}"
                    )
            log(f"SHM/IPC check passed for {total} concrete query execution(s)")
    finally:
        con.close()


def main() -> None:
    from synnodb import SynnoDB
    from synnodb.workloads.byo_workload import register_workload_from_json

    log(
        f"model={MODEL} sf={SF} threads={THREADS} max_turns={MAX_TURNS} "
        f"parquet={PARQUET_DIR} engines={ENGINES_DIR}"
    )

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
        log(
            f"===== STORAGE PLAN DONE in {time.time() - t0:.0f}s  run={plan.run_id} ====="
        )

        log("===== BASE IMPL (all queries on the one plan) START =====")
        t0 = time.time()
        impl = db.createBaseImpl(storage_plan=plan.text)
        log(
            f"===== BASE IMPL DONE in {time.time() - t0:.0f}s  workspace={impl.workspace} ====="
        )

        if SHM_IPC_CHECK:
            log("===== SHM/IPC ALL-QUERY CHECK START =====")
            t0 = time.time()
            run_shm_ipc_check(Path(impl.workspace), spec, query_ids)
            log(f"===== SHM/IPC ALL-QUERY CHECK DONE in {time.time() - t0:.0f}s =====")

        log(f"ALL DONE in {(time.time() - t_start) / 3600:.1f}h")
        log(f"engine published under: {ENGINES_DIR}")
    except Exception:
        log(f"FAILED after {(time.time() - t_start) / 3600:.1f}h")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
