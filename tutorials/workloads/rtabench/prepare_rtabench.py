"""RTABench as a bring-your-own SynnoDB workload.

RTABench (https://github.com/timescale/rtabench) models an online store - customers,
products, orders, order_items and ~171M order_events - and asks the questions a
real-time application asks: selective time windows, joins across the normalized
schema, pre-aggregated rollups. Its queries carry their filter values inline, so each
one registers as a constant template (the CEB shape), not a parameterized one.

Nothing of RTABench is vendored here. Its dataset lives behind
``rtadatasets.timescale.com`` and its SQL in the upstream repo (CC BY-NC-SA 4.0);
point ``RTABENCH_DIR`` at your clone and this module reads the schema and queries from
it, exactly as the Stack tutorial reads its downloaded query log.

Not every RTABench query can be bespoke-served. ``routable_queries`` runs each one
against the live schema and keeps those whose output columns are inside the engine's
exact-egress vocabulary; the rest stay on DuckDB and are reported by name so the gap
is visible rather than silent. Today that is 27 of 31: the four that stay behind all
project ``order_events.event_payload``, whose JSON type is outside that vocabulary.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import urllib.request
from pathlib import Path
from typing import Any

DATASET_URL = "https://rtadatasets.timescale.com"
TABLES = ("customers", "products", "orders", "order_items", "order_events")


def rtabench_dir() -> Path:
    """The user's RTABench clone (``RTABENCH_DIR``), which holds the schema and queries."""
    root = os.environ.get("RTABENCH_DIR")
    if not root:
        raise SystemExit(
            "Set RTABENCH_DIR to your clone of https://github.com/timescale/rtabench"
        )
    path = Path(root)
    if not (path / "duckdb" / "create.sql").exists():
        raise SystemExit(
            f"{path} does not look like an RTABench clone (no duckdb/create.sql)"
        )
    return path


def ensure_dataset(dataset_dir: Path) -> Path:
    """Download and decompress the five RTABench CSVs once (~5.6 GB compressed)."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for table in TABLES:
        csv = dataset_dir / f"{table}.csv"
        if csv.exists():
            continue
        archive = dataset_dir / f"{table}.csv.gz"
        if not archive.exists():
            print(f"downloading {table}.csv.gz ...", flush=True)
            urllib.request.urlretrieve(f"{DATASET_URL}/{table}.csv.gz", archive)
        print(f"decompressing {table}.csv.gz ...", flush=True)
        with gzip.open(archive, "rb") as src, open(csv, "wb") as dst:
            shutil.copyfileobj(src, dst)
        archive.unlink()
    return dataset_dir


def ensure_rtabench_duckdb(db_path: Path, dataset_dir: Path) -> Path:
    """Build ``rtabench.duckdb`` from the CSVs using RTABench's own schema. Idempotent."""
    db_path = Path(db_path)
    if db_path.exists():
        return db_path
    import duckdb

    ensure_dataset(dataset_dir)
    staging = db_path.with_suffix(".building")
    staging.unlink(missing_ok=True)
    con = duckdb.connect(str(staging))
    try:
        con.execute((rtabench_dir() / "duckdb" / "create.sql").read_text())
        for table in TABLES:
            print(f"loading {table} ...", flush=True)
            # The published CSVs are CRLF-terminated with bare newlines inside quoted
            # address fields, which DuckDB's strict sniffer refuses outright (it parsed
            # under the 1.2 CLI the benchmark shipped with). Reading them needs the
            # permissive mode, so the loaded row counts are checked below.
            con.execute(
                f"COPY {table} FROM '{dataset_dir / f'{table}.csv'}' "
                "(FORMAT CSV, HEADER false, strict_mode false)"
            )
        _verify_row_counts(con)
    finally:
        con.close()
    # Rename only once the load succeeded, so an interrupted build never leaves a
    # half-filled database looking ready.
    staging.rename(db_path)
    return db_path


# Row counts RTABench publishes for its dataset. A permissive CSV parse that silently
# dropped or merged rows would otherwise reach the engine as a plausible-looking dataset.
PUBLISHED_ROWS = {"customers": 1_102, "products": 9_255, "orders": 10_010_342}


def _verify_row_counts(con: Any) -> None:
    for table in TABLES:
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        expected = PUBLISHED_ROWS.get(table)
        if expected is not None and rows != expected:
            raise RuntimeError(
                f"{table}: loaded {rows:,} rows, RTABench publishes {expected:,}. "
                "The CSV parse is wrong - refusing to build a database the engine "
                "would then be validated against."
            )
        print(f"  {table}: {rows:,} rows", flush=True)


def _query_files() -> list[Path]:
    return sorted((rtabench_dir() / "duckdb" / "queries").glob("*.sql"))


def query_id(path: Path) -> str:
    """RTABench's own query number, unpadded: ``0018_customer_month_value`` -> ``18``."""
    return str(int(path.name.split("_", 1)[0]))


def routable_queries(con: Any) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Split RTABench's queries into those a bespoke engine can serve and those it cannot.

    *con* is a DuckDB connection carrying the RTABench schema (rows are irrelevant - only
    the output types are read). Returns ``({query_id: sql}, [(name, reason), ...])``.
    """
    from synnodb.router.normalize import is_select, normalize_sql
    from synnodb.router.registration import unroutable_queries

    candidates: dict[str, str] = {}
    by_id: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for path in _query_files():
        sql = path.read_text().strip().rstrip(";")
        if not is_select(sql):
            skipped.append((path.stem, "not a plain SELECT"))
            continue
        if normalize_sql(sql) is None:
            skipped.append((path.stem, "does not parse into a template key"))
            continue
        candidates[query_id(path)] = sql
        by_id[query_id(path)] = path.stem
    # One check, the framework's own, so the tutorial can never drift from what the router
    # will actually accept.
    refused = dict(unroutable_queries(con, candidates))
    keep = {qid: sql for qid, sql in candidates.items() if qid not in refused}
    skipped.extend((by_id[qid], reason) for qid, reason in refused.items())
    return keep, skipped


def write_queries_json(target: Path, queries: dict[str, str]) -> Path:
    """Write the bring-your-own ``queries.json`` sync_from_duckdb consumes."""
    target = Path(target)
    target.write_text(json.dumps(queries, indent=2))
    return target
