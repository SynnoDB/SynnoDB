import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

import duckdb
import pandas as pd
import pyarrow as pa
from tqdm import tqdm

from synnodb.utils.drop_caches import drop_os_caches, is_memory_backed
from synnodb.utils.utils import DBStorage, ServeFrom
from synnodb.workloads.workload_provider import Workload


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
        run_duckdb_on_parquet: bool = True,
        serve_from: ServeFrom = ServeFrom.PARQUET,
        drop_os_caches_before_sql: bool = True,
    ):
        self.con: duckdb.DuckDBPyConnection | None = None
        self.pre_load_duckdb_tables = pre_load_duckdb_tables
        # If True, each dataset table is registered as a CREATE VIEW over its
        # parquet file, so queries stream the data from parquet at query time
        # and nothing is held in DuckDB. If False, each table is materialized
        # into DuckDB via CREATE TABLE (data lives in memory / the .duckdb file).
        # Only consulted when the subset is served from PARQUET.
        self.run_duckdb_on_parquet = run_duckdb_on_parquet
        # Where the oracle reads each subset from: ``ServeFrom.DUCKDB`` materializes the flat tables
        # from the subset's ``subset.duckdb`` (ATTACH) instead of from parquet.
        self.serve_from = serve_from
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
        self.drop_os_caches_before_sql = drop_os_caches_before_sql

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
        """A DataFrame view of the result (for callers that want pandas: the UI service, plots).

        The correctness check uses ``duckdb_sql_arrow`` instead, because Arrow -> pandas coerces
        DECIMAL columns to float64 - which makes an exact bespoke decimal result compare unequal to
        the reference. Keep Arrow end to end for the comparison; pandas only for display."""
        exec_time_ms, table, profile_data = self.duckdb_sql_arrow(sql)
        return exec_time_ms, table.to_pandas(), profile_data

    def duckdb_sql_arrow(self, sql: str) -> Tuple[float, "pa.Table", Dict]:
        if self.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
            assert self.duckdb_path is not None
            if not self.duckdb_path.exists():
                self._ensure_tables_loaded()
            self._connect(self.duckdb_path)

        elif self.db_storage == DBStorage.IN_MEMORY:
            if self.con is None:
                self._ensure_tables_loaded()
        else:
            raise ValueError(f"Unknown db source: {self.db_storage}")

        if self.drop_os_caches_before_sql or self.db_storage in [
            DBStorage.LABSTORE,
            DBStorage.SSD,
        ]:
            # Drop OS page caches *after* connecting so connection-setup pages
            # don't stay warm; ensures cold-start semantics for every query.
            drop_os_caches()

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
                # Keep the result as exact Arrow: DECIMAL stays decimal128 (a pandas round-trip
                # coerces it to float64), so the correctness check can compare the bespoke engine's
                # exact decimal result bit-for-bit. Timing is the profiler latency, independent of
                # the fetch path.
                result_table = self.con.execute(sql).to_arrow_table()

                with open(profile_output_path, "r") as f:
                    profile_data = json.load(f)

                exec_time_ms = profile_data["latency"] * 1000.0
        finally:
            if orig_affinity is not None:
                os.sched_setaffinity(pid, orig_affinity)

        return exec_time_ms, result_table, profile_data

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
        self._apply_thread_pragma()
        return self.con

    def _apply_thread_pragma(self) -> None:
        """Set ``PRAGMA threads`` on the live connection and verify it actually took effect.

        The reference timings are cached keyed by the *requested* ``num_threads`` (see
        ``QueryExecutionCache``), so a connection that silently runs at a different thread count
        would poison the cache with a mislabeled timing - e.g. an effectively single-threaded
        DuckDB run stored as an 8-thread baseline, inflating the reported speedup. Read the
        setting back and fail loudly instead of caching a wrong number.
        """
        assert self.con is not None
        self.con.execute(f"PRAGMA threads={self.num_threads};")
        applied = int(self.con.execute("SELECT current_setting('threads')").fetchone()[0])
        assert applied == self.num_threads, (
            f"DuckDB thread count did not take effect: requested {self.num_threads}, "
            f"connection reports {applied}. Refusing to run so a mislabeled timing is not cached."
        )

    def set_thread_config(
        self, num_threads: int, pin_worker: bool, pin_core: Optional[int]
    ) -> None:
        """Resync this (possibly already-connected, already-loaded) manager to a new thread
        count, without rebuilding the connection or re-materializing tables.

        Callers memoize ``DuckDBConnectionManager`` by dataset identity alone (see
        ``OLAPSystemFactory.get_system``), so the same instance is reused across a run even as
        the run tool's thread count changes (e.g. serial generation -> multi-threaded
        validation). DuckDB must always run at the same thread count as the engine under test,
        so re-apply ``PRAGMA threads`` on the live connection here; CPU pinning itself is already
        re-applied per query in ``duckdb_sql_arrow``, so updating the attributes is enough for
        that part.
        """
        if pin_worker:
            assert pin_core is not None
            assert num_threads == 1, (
                "Pinning worker to a single core only makes sense if num_threads=1"
            )
        if num_threads != 1:
            assert not pin_worker, (
                "Pinning worker to a single core is not compatible with multi-threading (num_threads > 1)"
            )

        if num_threads == self.num_threads and pin_worker == self.pin_worker:
            return

        self.num_threads = num_threads
        self.pin_worker = pin_worker
        self.pin_core = pin_core
        if self.con is not None:
            self._apply_thread_pragma()

    def _ensure_tables_loaded(self) -> None:
        """Connect and make each dataset table available under its real name.

        The tables resolve from one of three sources: a ``ServeFrom.DUCKDB`` subset's
        ``subset.duckdb`` (materialized flat via ATTACH), a view over the subset's parquet
        (``run_duckdb_on_parquet`` - data streamed from parquet at query time), or a materialized
        table read from that parquet. Either way the (unchanged) SQL queries that reference bare
        table names resolve correctly.
        """
        self._connect(self.duckdb_path)
        assert self.con is not None

        # Resolve the subset directory under the parquet root (sampling-fraction ``fraction<f>``
        # or legacy ``sf<N>``).
        from synnodb.workloads.workload_spec import find_sf_dir

        subset_dir = find_sf_dir(self.parquet_path, self.sf)
        if subset_dir is None:
            raise FileNotFoundError(
                f"No subset directory for fraction/SF {self.sf:g} under {self.parquet_path}."
            )

        if self.serve_from == ServeFrom.DUCKDB:
            self._load_tables_from_subset_duckdb(subset_dir)
        else:
            if self.run_duckdb_on_parquet:
                object_kind = "VIEW"
                desc = f"Registering DuckDB parquet views for {subset_dir.name}"
            else:
                object_kind = "TABLE"
                desc = f"Loading DuckDB tables for {subset_dir.name}"

            for table in tqdm(self.dataset_tables, desc=desc):
                self.con.execute(
                    f"CREATE {object_kind} {table} AS "
                    f"SELECT * FROM read_parquet('{(subset_dir / f'{table}.parquet').as_posix()}')"
                )
        if self.duckdb_path is not None:
            self.con.execute("CHECKPOINT")
            self.con.close()
            self.con = None

    def _load_tables_from_subset_duckdb(self, subset_dir: Path) -> None:
        """Materialize each dataset table flat from the subset's ``subset.duckdb`` (DuckDB-native).

        The subset database is ATTACHed read-only and each table copied into a native table, so
        the oracle answers the exact same rows the engine ingests over shm - no parquet on disk.
        """
        assert self.con is not None
        subset_db = subset_dir / "subset.duckdb"
        if not subset_db.exists():
            raise FileNotFoundError(
                f"No DuckDB-native subset database at {subset_db} (expected subset.duckdb)."
            )
        alias = "_synno_subset_src"
        safe_db = subset_db.as_posix().replace("'", "''")
        self.con.execute(f"ATTACH '{safe_db}' AS {alias} (READ_ONLY)")
        try:
            for table in tqdm(
                self.dataset_tables,
                desc=f"Loading DuckDB tables for {subset_dir.name} (native)",
            ):
                self.con.execute(
                    f'CREATE TABLE {table} AS SELECT * FROM {alias}."{table}"'
                )
        finally:
            self.con.execute(f"DETACH {alias}")

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
