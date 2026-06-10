import logging
from pathlib import Path
from typing import Optional

from tools.validate.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from utils.utils import DBStorage

logger = logging.getLogger(__name__)


class DuckDBRunner:
    name = "DuckDB"

    def __init__(
        self,
        parquet_path: Path,
        benchmark: str,
        db_storage: DBStorage,
        disk_db_dir: Optional[Path] = None,
        num_threads: int = 1,
        pin_worker: bool = True,
    ) -> None:
        self._parquet_path = parquet_path
        self._benchmark = benchmark
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
            parquet_path=self._parquet_path.as_posix(),
            sf=scale_factor,
            pin_worker=self._pin_worker,
            benchmark=self._benchmark,
            num_threads=self._num_threads,
            db_storage=self._db_storage,
            disk_db_dir=self._disk_db_dir,
        )

        results: list[float | None] = []
        for sql in sql_list:
            time_ms, _, _ = duckdb_con.duckdb_sql(sql)
            results.append(time_ms)
        return results
