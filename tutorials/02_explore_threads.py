"""Tutorial 2 / explore: accelerate TPC-H Q1-5 at SF20 with a multi-threaded engine.

This is the middle rung of the ladder. Tutorial 1 (01_quickstart.ipynb) accelerated a single
DuckDB query in a notebook. Here we scale up: accelerate five queries, at a bigger scale factor,
and - the new idea - control PARALLELISM with the DuckDB-style `threads` knob. As always, SynnoDB
accelerates the queries you choose while DuckDB keeps serving everything else.

Unlike the notebook, generation here takes long enough (many minutes) that you want it running
unattended. Run it in tmux:

    tmux new -s synno2
    yes | .venv/bin/python -u tutorials/02_explore_threads.py
    # detach: Ctrl-b then d   |   reattach: tmux attach -t synno2

`yes |` auto-answers the occasional confirmation prompt; `-u` keeps the tmux log live.

----------------------------------------------------------------------------------------------
THE `threads` KNOB (this tutorial's focus)
----------------------------------------------------------------------------------------------
`threads` is the degree of parallelism the engine is DESIGNED, VALIDATED, and SERVED at. It is
the same idea as DuckDB's `config={'threads': N}`. The resolution is:

    threads unset (None) -> 1     : a single-threaded engine (the safe default; what tutorial 1 got)
    threads = 0          -> ALL usable cores on this machine (auto-detected)
    threads = N          -> N threads (clamped to the machine's usable cores)

It matters at TWO moments:
  1. GENERATION - the storage planner and the query implementer are told the target thread count,
     so they design a layout that partitions cleanly across that many workers.
  2. SERVING - a published engine records the count it was built for, and you can override it per
     connection with `connect(config={'threads': N})` without regenerating.

Below we generate ONE engine designed for 8 threads, then serve it at 8 and at 0 (all cores) to
see the runtime override in action.
"""
from __future__ import annotations

import os
import random
import statistics
import time
from pathlib import Path

# --- environment ------------------------------------------------------------------------
os.environ.setdefault("LLM_API_BASE", "http://dgx02:13506/v1")

DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR", "/mnt/labstore/learneddb/synno_data"))
PARQUET_DIR = Path(os.environ.get("SYNNO_TPCH_PARQUET", "/mnt/labstore/bespoke_olap/tpch_parquet"))
ENGINES_DIR = Path(os.environ.get("SYNNO_ENGINES_DIR", DATA_ROOT / "engines"))
ENGINES_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SYNNO_ENGINES_DIR"] = str(ENGINES_DIR)

MODEL = os.environ.get("SYNNO_MODEL", "openai/unsloth/MiniMax-M3")
SF = int(os.environ.get("SYNNO_SF", "20"))
QUERIES = "1-5"

# The engine we PUBLISH is designed for this many threads. Try 0 to design for all cores, or
# any N; unset (1) would build a single-threaded engine like tutorial 1.
GEN_THREADS = int(os.environ.get("SYNNO_THREADS", "8"))
MAX_TURNS = int(os.environ.get("SYNNO_MAX_TURNS", "300"))

TABLES = ["customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier"]
HERE = Path(__file__).parent
QUERIES_JSON = HERE / "queries.json"
N_REPS = 10  # parameter instantiations per query, for a stable timing average


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def draw_instantiations(spec):
    """Fill each query template with N_REPS seeded parameter draws. The SAME strings are reused
    for the DuckDB baseline and every SynnoDB run, so all comparisons are apples-to-apples."""
    rng = random.Random(42)
    gen = spec.query_gen_factory(None)  # gen(name, rng) -> (name, sql_with_values, params)
    return {qid: [gen(f"Q{qid}", rng) for _ in range(N_REPS)] for qid in spec.all_query_ids}


def time_queries(execute, instantiations, ordered_ids) -> dict[str, list[float]]:
    """Run every instantiation through `execute(sql)` and return per-query millisecond timings."""
    out: dict[str, list[float]] = {}
    for qid in ordered_ids:
        times = []
        for _, sql, _ in instantiations[qid]:
            t0 = time.perf_counter()
            execute(sql)
            times.append((time.perf_counter() - t0) * 1_000)
        out[qid] = times
    return out


def print_table(title, ordered_ids, baseline, candidate=None):
    log(title)
    if candidate is None:
        print(f"{'Query':<8}{'Total (ms)':>14}{'Median (ms)':>14}")
        for qid in ordered_ids:
            t = baseline[qid]
            print(f"Q{qid:<7}{sum(t):>14.1f}{statistics.median(t):>14.1f}")
        print(f"{'TOTAL':<8}{sum(sum(v) for v in baseline.values()):>14.1f}")
    else:
        print(f"{'Query':<8}{'DuckDB (ms)':>14}{'SynnoDB (ms)':>14}{'Speedup':>10}")
        for qid in ordered_ids:
            d, s = sum(baseline[qid]), sum(candidate[qid])
            print(f"Q{qid:<7}{d:>14.1f}{s:>14.1f}{(d / s if s else float('inf')):>9.2f}x")
        td, ts = sum(sum(v) for v in baseline.values()), sum(sum(v) for v in candidate.values())
        print(f"{'TOTAL':<8}{td:>14.1f}{ts:>14.1f}{(td / ts if ts else float('inf')):>9.2f}x")


def main() -> None:
    import duckdb

    from synnodb import SynnoDB
    from synnodb.workloads.byo_workload import register_workload_from_json

    log(f"model={MODEL} sf={SF} queries={QUERIES} gen_threads={GEN_THREADS}")

    # 1) Register the bring-your-own workload. Passing the small SFs too (1, 2) lets the framework
    #    validate cheaply at those before the SF20 pass.
    spec = register_workload_from_json(
        name="tpch_byo",
        queries_json=QUERIES_JSON,
        parquet_dir=PARQUET_DIR,
        scale_factors=(1, 2, SF),
        schema_example_table="lineitem",
    )
    ordered_ids = list(spec.all_query_ids)
    instantiations = draw_instantiations(spec)
    log(f"registered tpch_byo; queries={ordered_ids}, {N_REPS} instantiations each")

    # 2) DuckDB baseline over the identical SQL strings.
    sf_dir = PARQUET_DIR / f"sf{SF}"
    duck = duckdb.connect(":memory:")
    for t in TABLES:
        duck.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")
    baseline = time_queries(lambda sql: duck.execute(sql).fetchall(), instantiations, ordered_ids)
    duck.close()
    print_table("===== DuckDB baseline =====", ordered_ids, baseline)

    # 3) Generate the bespoke engine DESIGNED FOR GEN_THREADS. This is the long step; the storage
    #    plan and base implementation are both told the thread target so the layout partitions
    #    cleanly across that many workers. The validated engine auto-publishes into ENGINES_DIR.
    db = SynnoDB(
        workload="tpch_byo",
        model=MODEL,
        db_storage="in_memory",
        queries=QUERIES,
        threads=GEN_THREADS,   # <- the knob: 8 here; try 0 for all cores
        max_turns=MAX_TURNS,
    )
    log(f"===== GENERATE (designed for {GEN_THREADS} threads) START =====")
    t0 = time.time()
    plan = db.createStoragePlan()
    log(f"storage plan done ({time.time() - t0:.0f}s), run={plan.run_id}")
    t0 = time.time()
    impl = db.createBaseImpl(storage_plan=plan.text)
    log(f"base impl done ({time.time() - t0:.0f}s), published to {ENGINES_DIR}, workspace={impl.workspace}")

    # 4) Serve the SAME published engine at two thread counts on a real DuckDB connection
    #    (con.duckdb is DuckDB; con.execute accelerates the queries that have an engine, and
    #    falls back to DuckDB for the rest). The thread count is a RUNTIME override, no
    #    regeneration: connect(config={'threads': N}) runs at N, same knob as generation -
    #    8 = eight workers, 0 = all cores.
    import synnodb
    from synnodb.router import RouterMode, RouterPolicy

    for serve_threads in (GEN_THREADS, 0):
        con = synnodb.connect(
            ":memory:",
            engines=str(ENGINES_DIR),
            policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
            config={"threads": serve_threads},
        )
        for t in TABLES:
            con.duckdb.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sf_dir}/{t}.parquet')")
        con.refresh_engines()

        synno = time_queries(lambda sql: con.execute(sql).fetchall(), instantiations, ordered_ids)
        label = "all cores" if serve_threads == 0 else f"{serve_threads} threads"
        print_table(f"===== SynnoDB served at {label} =====", ordered_ids, baseline, synno)

        stats = con.router_stats()["session"]
        assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
        log(f"served at {label}: routed={stats['routed']}, "
            f"mismatches={stats['cross_check_mismatch']} (all match DuckDB)")
        con.close()

    log("DONE. The engine is published; tutorial 3 scales this to all 22 queries at SF50 / 64 threads.")


if __name__ == "__main__":
    main()
