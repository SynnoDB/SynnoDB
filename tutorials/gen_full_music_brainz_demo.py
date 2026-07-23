"""SynnoDB: From DuckDB to Bespoke in One Import - generation part (MusicBrainz).

The bring-your-own sibling of ``gen_full_tpch_demo.py``, ``gen_full_ceb_demo.py`` and
``gen_full_stack_demo.py``, run against the MusicBrainz workload over the MusicBrainz core
catalog. Covers only the engine-generation flow:
  Setup -> open MusicBrainz DuckDB -> register workload -> storage plan -> base impl -> optimization.
The benchmark / drop-in steps are not included here.

Like IMDB and Stack, and unlike TPC-H, there is no ``dbgen`` to synthesize MusicBrainz - it is real
data (the public MusicBrainz data dump), so this demo opens a ``musicbrainz.duckdb`` you point it at
rather than materializing one. Build it once with
``tutorials/workloads/music_brainz/load_musicbrainz.py`` (download + load the dump into DuckDB).

The 10 MusicBrainz queries share one template per class and differ only in their filter literals, so
``musicbrainz_queries.json`` (under ``tutorials/workloads/music_brainz/``) ships each class as a
fixed template whose placeholders are bound - as a ``tuples`` parameter group - to pre-generated
correlated literal rows: at run time SynnoDB samples a whole binding per execution, the templated
bring-your-own shape ``sync_from_duckdb`` consumes. Regenerate it with
``tutorials/workloads/music_brainz/_gen_musicbrainz_queries.py``.

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

# The data root holds everything: engines and workspace. Honor SYNNO_DATA_DIR if set, else default
# to a project-local .synno_data/. It need not exist yet. Unlike the TPC-H demo it does not hold the
# source database - MusicBrainz is real data you bring (SYNNO_MUSICBRAINZ_DUCKDB below), not
# something generated.
DATA_ROOT = Path(os.environ.get("SYNNO_DATA_DIR") or repo_root() / ".synno_data")
GENERATED_ENGINES_DIR = DATA_ROOT / "engines"

# The MusicBrainz tables the workload touches. The full dump loads many more; these are the ones the
# 10 query classes reference. SynnoDB reads the actual schema from the live connection below - this
# list is documentation only.
TABLES = [
    "recording",
    "artist",
    "artist_type",
    "artist_credit_name",
    "tag",
    "release",
    "release_group",
    "release_group_meta",
    "release_group_tag",
    "release_group_primary_type",
    "release_country",
    "release_label",
    "label",
    "label_type",
    "medium",
    "medium_format",
    "area",
]

MODEL = os.environ.get(
    "SYNNO_MODEL", "anthropic/claude-sonnet-5"
)  # e.g. "anthropic/claude-sonnet-4-6", "gpt-5.4", "openrouter/z-ai/glm-5.2"
MODEL_EXTRA_BODY = json.loads(os.environ.get("SYNNO_MODEL_EXTRA_BODY", "null"))
QUERIES_JSON = (
    Path(__file__).parent / "workloads" / "music_brainz" / "musicbrainz_queries.json"
)

print("Data root   :", DATA_ROOT)
print("Generated engines dir :", GENERATED_ENGINES_DIR)
print("Model       :", MODEL)


# --- Your DuckDB database (for the demo: MusicBrainz) --------------------------------------
# SynnoDB works off a DuckDB database you already have. TPC-H can be synthesized with DuckDB's
# built-in dbgen; MusicBrainz cannot - it is real data - so this demo simply opens a
# ``musicbrainz.duckdb`` you already hold (built by
# ``tutorials/workloads/music_brainz/load_musicbrainz.py``). In your own project this is just the
# duckdb.connect(...) you already have. Point SYNNO_MUSICBRAINZ_DUCKDB at your file.
import duckdb

MUSICBRAINZ_DB = Path(
    os.environ.get(
        "SYNNO_MUSICBRAINZ_DUCKDB",
        "/mnt/labstore/learned_db/synno_data/workloads/musicbrainz/musicbrainz.duckdb",
    )
)  # the single source of truth - one live DuckDB
if not MUSICBRAINZ_DB.exists():
    raise SystemExit(
        f"MusicBrainz DuckDB not found: {MUSICBRAINZ_DB}\n"
        "Set SYNNO_MUSICBRAINZ_DUCKDB to your musicbrainz.duckdb (build it with "
        "tutorials/workloads/music_brainz/load_musicbrainz.py)."
    )

# Your live working database. Open it read-only: a read-write connection takes an exclusive OS lock
# on the file, which would block SynnoDB's own read-only access below. Read-only openers coexist.
duckdb_con = duckdb.connect(str(MUSICBRAINZ_DB), read_only=True)
print("Source tables:", [r[0] for r in duckdb_con.execute("SHOW TABLES").fetchall()])


# --- Step 1: Workload Registration ---------------------------------------------------------
# Describe the workload once (its queries) and hand SynnoDB the live DuckDB. From that one
# connection it reads the schema, benchmarks the first queries at full scale, and derives the cheap
# correctness rungs by FK-preserving downscaling (no pre-scaled data subsets).
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
    queries="1-10",
    data_dir=DATA_ROOT,
    threads=NUM_THREADS,
)

# Hand your LIVE connection to SynnoDB and register the workload. It reads the schema + queries
# through this connection once, freezing a consistent point-in-time snapshot it owns - then derives
# a few downscaled versions of the dataset from that snapshot. Your database is only ever read.
spec = db.sync_from_duckdb(
    duckdb_con,  # your live duckdb.DuckDBPyConnection (a ".duckdb" path also works)
    name="musicbrainz_byo",
    queries_json=QUERIES_JSON,
    schema_example_table="recording",
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
