"""Assemble ``tutorials/gen_rtabench_demo.ipynb``.

The RTABench sibling of ``_gen_clickbench_notebook.py``: prepare the dataset, register
the routable queries as a bring-your-own workload, generate a bespoke engine, then drop
SynnoDB in and serve the very same statements with every result cross-checked.

Run: .venv/bin/python tutorials/assemble_tutorial/_gen_rtabench_notebook.py
"""

from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell

# Four queries keep the demo to one coffee break; widen with QUERY_SUBSET below.
QUERY_SUBSET = "2-9"
NUM_THREADS = 32
N_REPS = 5

cells = []


def md(text):
    cells.append(new_markdown_cell(text.strip()))


def code(text):
    cells.append(new_code_cell(text.strip()))


md("""
# SynnoDB on RTABench

[RTABench](https://github.com/timescale/rtabench) asks what a real-time application asks:
selective time windows over a normalized store schema - customers, products, orders,
order_items and ~171M order_events - joined and pre-aggregated rather than scanned. Its
queries carry their filter values inline, which would make every one a *constant*
template - a single statement whose answer an engine could simply memorize. We lift those
values into typed `[PLACEHOLDER]` holes instead, so each query becomes a real template
drawn many times with different values. That is what makes a generated kernel
falsifiable: one that folded the answer to a constant passes the first draw and fails the
next.

The dataset (~5.6 GB compressed) is downloaded once, and the SQL is read from your own
RTABench clone - set `RTABENCH_DIR`. Nothing of RTABench is redistributed here: the
parameterization is stored as edit pairs against your clone's files, and drifts loudly if
an upstream query changes.
""")

md("## Step 0 - Prepare the data")

code("""
import json
import logging
import os
import statistics
from pathlib import Path

from dotenv import load_dotenv

from synnodb.observability.logging.logger import setup_logging
from tutorials.workloads.rtabench.parameterize import write_parameterized_json
from tutorials.workloads.rtabench.prepare_rtabench import ensure_rtabench_duckdb

setup_logging(logging.INFO)
load_dotenv()

DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR") or ".synno_data").resolve()
DATASET_DIR = DATA_ROOT / "dataset"
RTABENCH_DB = DATA_ROOT / "rtabench.duckdb"
MODEL = os.environ.get("SYNNO_MODEL", "anthropic/claude-sonnet-5")
MODEL_EXTRA_BODY = json.loads(os.environ.get("SYNNO_MODEL_EXTRA_BODY", "null"))
NUM_THREADS = __THREADS__

print("Data root :", DATA_ROOT)
print("RTABench  :", os.environ["RTABENCH_DIR"])
print("Model     :", MODEL)

# Downloads the five CSVs and loads them with RTABench's own schema. Idempotent, and the
# slow part of this notebook the first time (~5.6 GB down, ~170M rows in).
ensure_rtabench_duckdb(RTABENCH_DB, DATASET_DIR)
print("Database  :", RTABENCH_DB, f"({RTABENCH_DB.stat().st_size / 1e9:.1f} GB)")
""")

md("""
### Which queries can be accelerated?

A bespoke engine reproduces its results exactly, so a query whose output columns fall
outside the engine's exact-egress vocabulary is left on DuckDB rather than approximated.
`sync_from_duckdb` runs that check up front against the live schema and prunes what it
cannot serve, naming each dropped query - so generation never pays for a query that could
only ever fall back, and the boundary is visible instead of silent. Four RTABench queries
project `order_events.event_payload`, whose JSON type is outside that vocabulary; they
stay on DuckDB.
""")

code("""
import duckdb

QUERIES_JSON, n_queries = write_parameterized_json(
    DATA_ROOT / "rtabench_parameterized.json"
)
print(f"parameterized catalog: {n_queries} queries -> {QUERIES_JSON}")

# The same check registration will run, shown here so the boundary is visible before
# anything is generated.
from synnodb.router.registration import unroutable_queries
from tutorials.workloads.rtabench.parameterize import parameterized_queries
from synnodb.workloads.byo_workload import _build_param_spaces, _parse_queries_json
import random

_entries = parameterized_queries()
_sql, _params = _parse_queries_json(_entries, "rtabench")
_spaces = _build_param_spaces(_sql, _params)
_rnd = random.Random(0)
_concrete = {}
for _qid, _tmpl in _sql.items():
    _s = _tmpl
    for _name, _value in _spaces[_qid].sample(_rnd).items():
        _s = _s.replace(f"[{_name}]", str(_value))
    _concrete[_qid] = _s

probe = duckdb.connect(str(RTABENCH_DB), read_only=True)
skipped = unroutable_queries(probe, _concrete)
probe.close()
print(f"routable: {n_queries - len(skipped)} of {n_queries}")
for name, reason in skipped:
    print(f"  stays on DuckDB - {name}: {reason}")
""")

md("## Step 1 - Register the workload")

code("""
from synnodb import SynnoDB

# None covers every query the workload registers; a range like "2-9" keeps the demo to one
# coffee break. Whatever is set here is what the engine is generated for AND what is served
# below, so the two can never drift apart.
QUERY_SUBSET_USED = __SUBSET_REPR__

db = SynnoDB(
    model=MODEL,
    model_extra_body=MODEL_EXTRA_BODY,
    db_storage="in_memory",
    query_subset=QUERY_SUBSET_USED,
    data_dir=DATA_ROOT,
    threads=NUM_THREADS,
    max_turns=450,
)

# Read-only: a read-write connection takes an exclusive OS lock that would block
# SynnoDB's own read-only access. Read-only openers coexist.
duckdb_con = duckdb.connect(str(RTABENCH_DB), read_only=True)

spec = db.sync_from_duckdb(
    duckdb_con,
    name="rtabench_byo",
    queries_json=QUERIES_JSON,
    schema_example_table="order_events",
)

print("Workload :", spec.name)
print("Tables   :", spec.tables)
print("Queries  :", spec.all_query_ids)
print("Subsets  :", spec.exhaustive_sfs, "(benchmark:", spec.benchmark_sf, ")")
""")

code("""
import random

# The queries the engine was actually built for. `query_subset` narrows generation, so
# reading the whole catalog here would ask the engine for templates it never saw and the
# final routing assertion would fail on queries nobody generated.
from synnodb.utils.gen_common import parse_query_ids

QUERY_IDS = parse_query_ids(QUERY_SUBSET_USED, benchmark=spec.name)
rng = random.Random(42)
gen = spec.query_gen_factory(None)

# DISTINCT draws, not repetitions of one statement. Each is a different point in the
# query's parameter space, so an engine is asked a different question every time and a
# kernel that memorized one answer cannot survive the set.
instantiations = {}
for qid in QUERY_IDS:
    seen, draws = set(), []
    while len(draws) < N_REPS:
        drawn = gen(f"Q{qid}", rng)
        if drawn[1] not in seen:
            seen.add(drawn[1])
            draws.append(drawn)
    instantiations[qid] = draws
for qid in QUERY_IDS:
    print(f"Q{qid}: {len(instantiations[qid])} distinct draws, e.g.",
          instantiations[qid][0][2])
""")

md("## Step 2 - Generate the bespoke engine")

code("""
plan = db.createStoragePlan()

print(plan.text[:600], "...")
""")

code("""
impl = db.createBaseImpl(storage_plan=plan.text)

print("Workspace :", impl.workspace)
print("Files     :", sorted(impl.files))
print()
print(f"Engine published to: {DATA_ROOT / 'engines'}")
""")

md("## Step 3a - Benchmark DuckDB")

code('''
import tempfile

duck = duckdb.connect(":memory:", config={"threads": NUM_THREADS})
duck.execute("PRAGMA disable_progress_bar")
duck.execute("PRAGMA enable_profiling='json'")
# enable_profiling also dumps a JSON profile after every statement; send those to a
# throwaway per-run file (/tmp is shared, so a fixed name can be another user's).
_fd, _profile_path = tempfile.mkstemp(suffix="_duckdb_profile.json")
os.close(_fd)
duck.execute(f"PRAGMA profiling_output='{_profile_path}'")

# Materialize the tables in memory (CREATE TABLE, not a VIEW) so the measurement is
# in-memory execution on both sides, not a fresh scan of the file.
duck.execute(f"ATTACH '{RTABENCH_DB}' AS src (READ_ONLY)")
for (table,) in duck.execute(
    "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'"
).fetchall():
    duck.execute(f'CREATE TABLE "{table}" AS SELECT * FROM src."{table}"')
duck.execute("DETACH src")


def analyze_ms(con, sql):
    """DuckDB's own execution latency via EXPLAIN ANALYZE - server-side query time,
    excluding the Python call and result fetch a wall clock would include."""
    profile = json.loads(con.execute("EXPLAIN ANALYZE " + sql).fetchone()[1])
    return profile["latency"] * 1_000


baseline_times = {
    qid: [analyze_ms(duck, sql) for _, sql, _ in insts]
    for qid, insts in instantiations.items()
}
duck.close()

for qid in QUERY_IDS:
    t = baseline_times[qid]
    print(f"Q{qid}: median {statistics.median(t):.1f} ms")
''')

md("""
## Step 3 - Drop in SynnoDB

One import line and a few keyword arguments; every other line is the DuckDB code you
already had.
""")

code("""
import synnodb
from synnodb.router import RouterMode, RouterPolicy

con = synnodb.connect(
    ":memory:",
    config={"threads": NUM_THREADS},
    engines=str(DATA_ROOT / "engines"),
    policy=RouterPolicy(mode=RouterMode.SAMPLED, cross_check_rate=1.0),
)

con.duckdb.execute(f"ATTACH '{RTABENCH_DB}' AS src (READ_ONLY)")
for (table,) in con.duckdb.execute(
    "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'"
).fetchall():
    con.duckdb.execute(f'CREATE TABLE "{table}" AS SELECT * FROM src."{table}"')
con.duckdb.execute("DETACH src")

con.refresh_engines()
print("Templates :", con.router_stats()["registry"]["templates"])

# Load the engine's data now, as an explicit step, so the first measured query is served
# warm instead of carrying the one-time ingest.
print("Preloaded :", con.synno_ingest_data(), "engine(s)")
""")

code("""
synno_times = {}
routed_to = {}
for qid, insts in instantiations.items():
    times, bespoke = [], []
    for _, sql, _ in insts:
        con.execute(sql).fetchall()
        last = con._last
        served = "engine_ms" in last
        times.append(last["engine_ms"] if served else last["duckdb_ms"])
        bespoke.append(served)
    synno_times[qid] = times
    routed_to[qid] = "SynnoDB" if all(bespoke) else "DuckDB"
""")

code("""
print(f"{'Query':<8} {'Routing':>8} {'DuckDB (ms)':>12} {'SynnoDB (ms)':>14} {'Speedup':>9}")
print("-" * 55)
total_duck = total_synno = 0.0
for qid in QUERY_IDS:
    med_d = statistics.median(baseline_times[qid])
    med_s = statistics.median(synno_times[qid])
    total_duck += med_d
    total_synno += med_s
    speedup = med_d / med_s if med_s > 0 else float("inf")
    mark = " ⚡" if routed_to[qid] == "SynnoDB" else ""
    print(f"Q{qid:<7} {routed_to[qid]:>8} {med_d:>12.1f} {med_s:>14.1f} {speedup:>8.2f}x{mark}")
print("-" * 55)
print(f"{'TOTAL':<8} {'':>8} {total_duck:>12.1f} {total_synno:>14.1f} "
      f"{total_duck / total_synno:>8.2f}x")
""")

md("""
### Correctness

Every routed result was compared against DuckDB (`cross_check_rate=1.0`), and every
draw asked a different question. A fallback here is a failure, not a soft degrade: the
engine was built for this template, so it must serve every point of the parameter space
it claims - and match DuckDB exactly on each one.
""")

code("""
stats = con.router_stats()["session"]
print(f"Routed:         {stats['routed']}")
print(f"Cross-checked:  {stats['cross_checked']}")
print(f"Mismatches:     {stats['cross_check_mismatch']}")
print(f"Fallbacks:      {stats['fallback_reasons']}")
assert stats["cross_check_mismatch"] == 0, "result divergence detected!"
assert stats["routed"] == N_REPS * len(QUERY_IDS), (
    f"a draw was not served by the engine: {stats['fallback_reasons']}"
)
print(
    f"\\nAll {stats['routed']} draws bespoke-served across {len(QUERY_IDS)} templates; "
    "every result matches DuckDB exactly."
)
con.close()
""")

nb = nbformat.v4.new_notebook(cells=cells)
for cell in nb.cells:
    if cell.cell_type == "code":
        cell.source = (
            cell.source.replace("__THREADS__", str(NUM_THREADS))
            .replace("__SUBSET_REPR__", repr(QUERY_SUBSET))
            .replace("N_REPS = 5", f"N_REPS = {N_REPS}")
        )
nb.cells.insert(
    3,
    new_code_cell(f"N_REPS = {N_REPS}  # repetitions per query, on both sides"),
)
# Canonical, stable cell ids. nbformat mints a random id per cell, so every
# regeneration would churn the whole file and fail the nbstripout gate that keeps
# tutorial notebooks diffable.
for index, cell in enumerate(nb.cells):
    cell.id = str(index)

out = Path(__file__).resolve().parent.parent / "gen_rtabench_demo.ipynb"
nbformat.write(nb, out)
print(f"wrote {out} with {len(nb.cells)} cells")
