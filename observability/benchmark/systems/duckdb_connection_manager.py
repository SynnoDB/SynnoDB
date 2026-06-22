import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

import duckdb
import pandas as pd
from tqdm import tqdm

from utils.drop_caches import drop_os_caches, is_memory_backed
from utils.utils import DBStorage
from workloads.workload_provider import Workload


class DuckDBConnectionManager:
    def __init__(
        self,
        pre_load_duckdb_tables: bool,
        dataset_tables: list[str],
        parquet_path: Path,
        benchmark: Workload,
        db_storage: DBStorage,
        disk_db_dir: Optional[Path] = None,
        sf: float = 1,
        pin_worker: bool = True,
        pin_core: Optional[int] = 3,
        num_threads: int = 1,
    ):
        self.con: duckdb.DuckDBPyConnection | None = None
        self.pre_load_duckdb_tables = pre_load_duckdb_tables
        self.parquet_path = parquet_path
        self.sf = sf
        self.pin_worker = pin_worker
        self.pin_core = pin_core
        self.benchmark = benchmark
        self.num_threads = num_threads
        self.db_storage = db_storage
        self.duckdb_dir: Optional[tempfile.TemporaryDirectory] = None
        self.duckdb_path: Optional[Path] = None
        self.dataset_tables = dataset_tables

        if self.pin_worker:
            assert self.pin_core is not None
            assert num_threads == 1, (
                "Pinning worker to a single core only makes sense if num_threads=1"
            )
        if self.num_threads != 1:
            assert not self.pin_worker, (
                "Pinning worker to a single core is not compatible with multi-threading (num_threads > 1)"
            )

        if self.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
            assert disk_db_dir is not None, (
                "disk_db_dir must be provided when db_storage is LABSTORE or SSD"
            )
            self.duckdb_path = self._new_duckdb_path(disk_db_dir)
        elif self.db_storage != DBStorage.IN_MEMORY:
            raise ValueError(f"Unknown db source: {self.db_storage}")

        if pre_load_duckdb_tables:
            self._ensure_tables_loaded()

    def duckdb_sql(self, sql: str) -> Tuple[float, pd.DataFrame, Dict]:
        if self.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
            assert self.duckdb_path is not None
            if not self.duckdb_path.exists():
                self._ensure_tables_loaded()
            self._connect(self.duckdb_path)
            # Drop OS page caches *after* connecting so connection-setup pages
            # don't stay warm; ensures cold-start semantics for every query.
            drop_os_caches()
        elif self.db_storage == DBStorage.IN_MEMORY:
            if self.con is None:
                self._ensure_tables_loaded()
        else:
            raise ValueError(f"Unknown db source: {self.db_storage}")

        assert self.con is not None
        pid = 0  # 0 = current process
        orig_affinity = None
        if self.pin_worker:
            orig_affinity = os.sched_getaffinity(pid)
            assert self.pin_core is not None
            os.sched_setaffinity(pid, {self.pin_core})

        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmpfile:
                profile_output_path = tmpfile.name
                self.con.execute("PRAGMA enable_profiling = 'json'")
                self.con.execute(f"PRAGMA profiling_output ='{profile_output_path}'")
                result_df = self.con.execute(sql).fetchdf()

                with open(profile_output_path, "r") as f:
                    profile_data = json.load(f)

                exec_time_ms = profile_data["latency"] * 1000.0
        finally:
            if orig_affinity is not None:
                os.sched_setaffinity(pid, orig_affinity)

        return exec_time_ms, result_df, profile_data

    def _new_duckdb_path(self, disk_db_dir: Path) -> Path:
        self.duckdb_dir = tempfile.TemporaryDirectory(
            prefix="duckdb-benchmark-", dir=disk_db_dir
        )
        duckdb_dir = Path(self.duckdb_dir.name).resolve()
        if is_memory_backed(duckdb_dir):
            self.duckdb_dir.cleanup()
            self.duckdb_dir = None
            raise RuntimeError(
                f"DuckDB benchmark directory is memory-backed: {duckdb_dir}. "
                "Provide a disk_db_dir pointing to persistent storage."
            )
        return duckdb_dir / "benchmark.duckdb"

    def _connect(self, database: Optional[Path] = None) -> duckdb.DuckDBPyConnection:
        db = database.as_posix() if database is not None else ":memory:"
        self.con = duckdb.connect(database=db)
        self.con.execute(f"PRAGMA threads={self.num_threads};")
        return self.con

    def _ensure_tables_loaded(self) -> None:
        """Connect, load tables, and for disk sources checkpoint and close."""
        self._connect(self.duckdb_path)
        assert self.con is not None
        for table in tqdm(
            self.dataset_tables,
            desc=f"Loading DuckDB tables for SF{self.sf}",
        ):
            self.con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{self.parquet_path}/sf{self.sf}/{table}.parquet')"
            )
        if self.duckdb_path is not None:
            self.con.execute("CHECKPOINT")
            self.con.close()
            self.con = None

    def clear_mem_footprint(self, including_disk: bool = False) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

        if including_disk and self.duckdb_dir is not None:
            self.duckdb_dir.cleanup()
            self.duckdb_dir = None
            self.duckdb_path = None

    def __del__(self) -> None:
        self.clear_mem_footprint(including_disk=True)
