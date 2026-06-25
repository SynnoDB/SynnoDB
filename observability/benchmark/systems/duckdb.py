import logging
from pathlib import Path
from typing import Optional

from observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from utils.utils import DBStorage
from workloads.workload_provider import Workload

logger = logging.getLogger(__name__)


class DuckDBRunner:
    """DuckDB reference runner, track-agnostic.

    The per-track specifics (which tables exist, whether to stream from parquet
    views vs. materialize, and the db storage) are passed in by the caller (see
    ``observability.benchmark.systems.track.DuckDBConfig``) so the same runner
    serves both the OLAP and BFF use-cases.
    """

    name = "DuckDB"

    def __init__(
        self,
        parquet_path: Path,
        benchmark: Workload,
        dataset_tables: list[str],
        db_storage: DBStorage,
        run_on_parquet: bool = True,
        disk_db_dir: Optional[Path] = None,
        num_threads: int = 1,
        pin_worker: bool = True,
    ) -> None:
        self._parquet_path = parquet_path
        self._benchmark = benchmark
        self._dataset_tables = dataset_tables
        self._run_on_parquet = run_on_parquet
        self._num_threads = num_threads
        self._pin_worker = pin_worker and (num_threads == 1)
        self._db_storage = db_storage
        self._disk_db_dir = disk_db_dir

    def run_scale_factor(
        self,
        scale_factor: float,
        query_list: list[str],
        sql_list: list[str],
        args_list: list[str],
    ) -> list[float | None]:
        logger.info("Running DuckDB timings (num_threads=%d)...", self._num_threads)
        duckdb_con = DuckDBConnectionManager(
            pre_load_duckdb_tables=True,
            parquet_path=self._parquet_path,
            sf=scale_factor,
            dataset_tables=self._dataset_tables,
            pin_worker=self._pin_worker,
            benchmark=self._benchmark,
            num_threads=self._num_threads,
            db_storage=self._db_storage,
            disk_db_dir=self._disk_db_dir,
            run_duckdb_on_parquet=self._run_on_parquet,
        )

        results: list[float | None] = []
        for sql in sql_list:
            time_ms, _, _ = duckdb_con.duckdb_sql(sql)
            results.append(time_ms)
        return results
