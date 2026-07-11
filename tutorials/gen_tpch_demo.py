"""SynnoDB: From DuckDB to Bespoke in One Import - generation part.

Exported from gen_tpch_demo.ipynb. Covers only the engine-generation flow:
  Setup -> build TPC-H DuckDB -> register workload -> storage plan -> base impl.
The benchmark / drop-in steps from the notebook are not included here.

Prerequisites: pip install "synnodb[factory]"
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from synnodb.utils.path_utils import repo_root
from synnodb.observability.logging.logger import setup_logging

setup_logging(logging.INFO)
load_dotenv()  # let SYNNO_DATA_DIR / SYNNO_ENGINES_DIR / SYNNO_WORKSPACE come from a .env

# The data root holds everything: the source DuckDB, engines, workspace. Honor SYNNO_DATA_DIR if
# set, else default to a project-local .synno_data/. It need not exist yet - the next step builds
# the TPC-H DuckDB into it.
DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR") or repo_root() / ".synno_data")
GENERATED_ENGINES_DIR = DATA_ROOT / "engines"

TABLES = [
    "customer",
    "lineitem",
    "nation",
    "orders",
    "part",
    "partsupp",
    "region",
    "supplier",
]

MODEL = os.environ.get(
    "SYNNO_MODEL", "anthropic/claude-sonnet-5"
)  # e.g. "anthropic/claude-sonnet-4-6", "gpt-5.4", "openrouter/z-ai/glm-5.2"
MODEL_EXTRA_BODY = json.loads(os.environ.get("SYNNO_MODEL_EXTRA_BODY", "null"))
QUERIES_JSON = Path("queries.json")  # lives next to this script

print("Data root   :", DATA_ROOT)
print("Generated engines dir :", GENERATED_ENGINES_DIR)
print("Model       :", MODEL)


# --- Your DuckDB database (for the demo: TPC-H) --------------------------------------------
# SynnoDB works off a DuckDB database you already have. For this demo only we assemble a TPC-H
# database with DuckDB's built-in dbgen; in your own project this is simply the duckdb.connect(...)
# you already hold. SF5 is a couple of GB and takes a little while to build.
import duckdb
from synnodb.workloads.dataset.gen_tpc_h_data import generate_tpch_duckdb

TPCH_DB = DATA_ROOT / "tpch.duckdb"  # the single source of truth - one live DuckDB

# --- Demo only: materialize a TPC-H database. Swap this line for your own dataset. ---
generate_tpch_duckdb(TPCH_DB, 50)

# Your live working database. Open it read-only: a read-write connection takes an exclusive OS lock
# on the file, which would block SynnoDB's own read-only access below. Read-only openers coexist.
duckdb_con = duckdb.connect(str(TPCH_DB), read_only=True)
print("Source tables:", [r[0] for r in duckdb_con.execute("SHOW TABLES").fetchall()])


# --- Step 1: Workload Registration ---------------------------------------------------------
# Describe the workload once (its queries) and hand SynnoDB the live DuckDB. From that one
# connection it reads the schema, benchmarks Q1-Q5 at full scale, and derives the cheap correctness
# rungs by FK-preserving downscaling (no pre-scaled data subsets).
from synnodb import SynnoDB

# Degree of parallelism the engine is generated, validated, AND served at (the DuckDB-style
# config={'threads': N}). 1 => single-threaded; for a real benchmark use os.cpu_count(). When > 1,
# the base-impl run ends with a per-query pass that runs each query at this thread count and fixes
# any that are only correct single-threaded.
NUM_THREADS = 8  # 8 for demo, for all cores: os.cpu_count()
assert NUM_THREADS is not None, "os.cpu_count() returned None"

db = SynnoDB(
    model=MODEL,
    model_extra_body=MODEL_EXTRA_BODY,
    db_storage="in_memory",
    queries="1-22",
    data_dir=DATA_ROOT,
    threads=NUM_THREADS,
)

# Hand your LIVE connection to SynnoDB and register the workload. It reads the schema + queries
# through this connection once, freezing a consistent point-in-time snapshot it owns - then derives
# a few downscaled versions of the dataset from that snapshot. Your database is only ever read.
spec = db.sync_from_duckdb(
    duckdb_con,  # your live duckdb.DuckDBPyConnection (a ".duckdb" path also works)
    name="tpch_byo",
    queries_json=QUERIES_JSON,
    schema_example_table="lineitem",
)

duckdb_con.execute("PRAGMA threads=%d" % NUM_THREADS)  # match SynnoDB's thread count

print("Workload :", spec.name)
print("Tables   :", spec.tables)
print("Queries  :", spec.all_query_ids)
print("Subsets  :", spec.exhaustive_sfs, "(benchmark:", spec.benchmark_sf, ")")


# --- Step 2: Generate the Simple Bespoke Engine --------------------------------------------
# 1. Create a storage plan - decide how each query's columns are laid out in memory.
# 2. Implement the engine - write a naive multi-threaded C++ engine, compile it, validate every
#    output against DuckDB on the cheap downscaled rungs first (then the full data subset), and
#    auto-publish the binary into GENERATED_ENGINES_DIR.

# Storage plan.
# Expected cost: ~$0.1
# Expected time: ~2 mins / 6 turns (depending on model speed)
plan = db.createStoragePlan()

print(plan.text[:600], "...")

# Base implementation. We feed the plan content straight in via storage_plan=plan.text, so this
# step needs no W&B. If you instead chain off a logged storage-plan run, pass its run id with
# db.createBaseImpl(storage_plan_wandb_id=plan.run_id). Provide exactly one of the two.
# Expected cost: ~$6
# Expected time: ~1 hrs / 500 turns (depending on model speed)
impl = db.createBaseImpl(storage_plan=plan.text)

print("Workspace :", impl.workspace)
print("Files     :", sorted(impl.files))
print()
print(f"Engine published to: {GENERATED_ENGINES_DIR}")

# Optimization Loop
# Takes ~10hrs, $50
impl = db.runOptimLoop(base_impl=impl)
