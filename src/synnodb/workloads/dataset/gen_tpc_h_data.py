import argparse
import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

import duckdb

# TPC-H tables ordered roughly by size (small first), so the largest table is generated last
# and memory spikes are predictable.
TPCH_TABLES: tuple[str, ...] = (
    "region",
    "nation",
    "supplier",
    "part",
    "customer",
    "partsupp",
    "orders",
    "lineitem",  # largest: ~6B rows at SF=1000
)


def _memory_limit_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                return max(1, int(total_kb / 1024 / 1024 * 0.65))
    raise RuntimeError("Could not read MemTotal from /proc/meminfo")


def _thread_count() -> int:
    return os.cpu_count() or 8


def ensure_tpch_parquet(
    parquet_dir: str | Path,
    scale_factors: Iterable[float],
    tables: Sequence[str] = TPCH_TABLES,
    *,
    spill_dir: str | Path | None = None,
) -> None:
    """Materialize ``<parquet_dir>/sf<N>/<table>.parquet`` for each scale factor via DuckDB's
    built-in ``dbgen`` - no external tooling.

    Idempotent: tables already on disk are skipped, so re-running is cheap. Generation runs on
    an on-disk DuckDB connection with a memory cap and a spill directory, and drops each table
    right after export, so even large scale factors do not OOM.

    ``spill_dir`` selects *where* spill files live (e.g. a large scratch mount); we always nest
    a private ``_duckdb_spill`` subdirectory under it and only that subdirectory is removed on
    cleanup, so a shared ``spill_dir`` holding other jobs' files is never clobbered.
    """
    parquet_dir = Path(parquet_dir)
    spill_root = Path(spill_dir) if spill_dir is not None else parquet_dir
    spill = spill_root / "_duckdb_spill"
    for sf in scale_factors:
        out = parquet_dir / f"sf{sf}"
        out.mkdir(parents=True, exist_ok=True)
        missing = [t for t in tables if not (out / f"{t}.parquet").exists()]
        if not missing:
            print(f"sf{sf}: present ({len(tables)} tables), skipping")
            continue

        print(f"sf{sf}: generating {missing} ...")
        spill.mkdir(parents=True, exist_ok=True)
        temp_db = (
            out / f"_gen_sf{sf}.duckdb"
        )  # on-disk so large SFs can spill instead of OOM
        con = duckdb.connect(str(temp_db))
        try:
            con.execute("INSTALL tpch; LOAD tpch;")
            con.execute(f"SET memory_limit='{_memory_limit_gb()}GB';")
            con.execute(f"SET threads={_thread_count()};")
            con.execute(f"SET temp_directory='{spill.as_posix()}';")
            con.execute("PRAGMA disable_progress_bar")
            con.execute(f"CALL dbgen(sf={sf});")
            for t in missing:
                dest = out / f"{t}.parquet"
                con.execute(f"COPY {t} TO '{dest.as_posix()}' (FORMAT PARQUET)")
                con.execute(f"DROP TABLE {t}")  # free memory before the next table
                print(f"  wrote {dest}")
        finally:
            con.close()
            temp_db.unlink(missing_ok=True)
            Path(f"{temp_db}.wal").unlink(missing_ok=True)
    shutil.rmtree(spill, ignore_errors=True)


def ensure_tpch_duckdb(
    duckdb_path: str | Path,
    scale_factor: float,
    *,
    spill_dir: str | Path | None = None,
) -> Path:
    """Materialize a single ``tpch.duckdb`` file holding the TPC-H tables at ``scale_factor``
    via DuckDB's built-in ``dbgen`` - the source of truth the new DuckDB-rooted flow downscales
    from (no pre-scaled parquet subsets).

    Idempotent: an existing file with all tables present is reused. Generation runs with a
    memory cap and a spill directory so even large scale factors do not OOM.
    """
    duckdb_path = Path(duckdb_path)
    if duckdb_path.exists():
        con = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            present = {r[0] for r in con.execute("PRAGMA show_tables").fetchall()}
        finally:
            con.close()
        if set(TPCH_TABLES) <= present:
            print(f"{duckdb_path}: present ({len(TPCH_TABLES)} tables), reusing")
            return duckdb_path

    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    spill = (Path(spill_dir) if spill_dir else duckdb_path.parent) / "_duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    print(f"{duckdb_path}: generating TPC-H sf{scale_factor} ...")
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute("INSTALL tpch; LOAD tpch;")
        con.execute(f"SET memory_limit='{_memory_limit_gb()}GB';")
        con.execute(f"SET threads={_thread_count()};")
        con.execute(f"SET temp_directory='{spill.as_posix()}';")
        con.execute("PRAGMA disable_progress_bar")
        con.execute(f"CALL dbgen(sf={scale_factor});")
        con.execute("CHECKPOINT")
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    return duckdb_path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate TPC-H Parquet data via DuckDB."
    )
    parser.add_argument(
        "--sf", type=int, default=1, metavar="N", help="TPC-H scale factor (1 = ~1 GB)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    scale_factor = args.sf
    base = Path("/mnt/labstore/bespoke_olap/tpch_parquet")
    tmp_dir = Path("/mnt/labstore/bespoke_olap/duckdb_tmp")

    ensure_tpch_parquet(base, [scale_factor], spill_dir=tmp_dir)

    output_dir = base / f"sf{scale_factor}"
    print(f"\nParquet files stored in: {output_dir}/")

    # ------------------------------------------------------------------
    # Optional: create a DuckDB file from the Parquet files.
    # At SF >= 100 this can be very large (100s of GB); skip if unwanted.
    # ------------------------------------------------------------------
    db_file = output_dir / "duckdb.db"
    print(f"\nCreating DuckDB file at {db_file} ...")
    db_con = duckdb.connect(str(db_file))
    db_con.execute(f"SET memory_limit='{_memory_limit_gb()}GB';")
    db_con.execute(f"SET temp_directory='{tmp_dir.as_posix()}';")
    for t in TPCH_TABLES:
        print(f"  Loading {t} ...")
        db_con.execute(
            f"CREATE TABLE {t} AS SELECT * FROM parquet_scan('{output_dir / (t + '.parquet')}');"
        )
    db_con.close()
    print(f"\nDuckDB file stored at: {db_file}")
