import argparse
import os

import duckdb


def _memory_limit_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                return int(total_kb / 1024 / 1024 * 0.65)
    raise RuntimeError("Could not read MemTotal from /proc/meminfo")


def _thread_count() -> int:
    return os.cpu_count() or 8


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
    output_dir = f"/mnt/labstore/bespoke_olap/tpch_parquet/sf{scale_factor}"
    temp_db_path = (
        f"/mnt/labstore/bespoke_olap/tpch_parquet/temp_gen_sf{scale_factor}.duckdb"
    )
    tmp_dir = "/mnt/labstore/bespoke_olap/duckdb_tmp/"

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    # TPC-H tables ordered roughly by size (small first, so memory spikes are predictable)
    tables = [
        "region",
        "nation",
        "supplier",
        "part",
        "customer",
        "partsupp",
        "orders",
        "lineitem",  # largest: ~6B rows at SF=1000
    ]

    # Use an on-disk connection so DuckDB can spill intermediates to disk instead of OOMing.
    # The temp DB is only used as a spill target; each table is dropped after export.
    mem_gb = _memory_limit_gb()
    threads = _thread_count()

    con = duckdb.connect(temp_db_path)
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute(f"SET memory_limit='{mem_gb}GB';")
    con.execute(f"SET threads={threads};")
    con.execute(f"SET temp_directory='{tmp_dir}';")

    missing = [t for t in tables if not os.path.exists(f"{output_dir}/{t}.parquet")]
    already_done = [t for t in tables if t not in missing]

    for t in already_done:
        print(f"  Skipping {t} (already exists)")

    if missing:
        print(f"Generating TPC-H data at scale factor {scale_factor} ...")
        con.execute(f"CALL dbgen(sf={scale_factor});")

        for t in missing:
            parquet_path = f"{output_dir}/{t}.parquet"
            print(f"  Saving {t} to Parquet ...")
            con.execute(f"""
                COPY {t} TO '{parquet_path}' (FORMAT PARQUET);
            """)
            con.execute(f"DROP TABLE {t};")
            print(f"  Done: {parquet_path}")

    con.close()
    os.remove(temp_db_path)

    print(f"\nParquet files stored in: {output_dir}/")

    # ------------------------------------------------------------------
    # Optional: create a DuckDB file from the Parquet files.
    # At SF >= 100 this can be very large (100s of GB); skip if unwanted.
    # ------------------------------------------------------------------
    db_file = f"{output_dir}/duckdb.db"
    print(f"\nCreating DuckDB file at {db_file} ...")
    db_con = duckdb.connect(db_file)
    db_con.execute(f"SET memory_limit='{mem_gb}GB';")
    db_con.execute(f"SET temp_directory='{tmp_dir}';")
    for t in tables:
        print(f"  Loading {t} ...")
        db_con.execute(
            f"CREATE TABLE {t} AS SELECT * FROM parquet_scan('{output_dir}/{t}.parquet');"
        )
    db_con.close()
    print(f"\nDuckDB file stored at: {db_file}")
