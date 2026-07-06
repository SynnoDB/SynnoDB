import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import clickhouse_connect
import duckdb
import pandas as pd
from clickhouse_connect.driver.client import Client
from tqdm import tqdm

from synnodb.workloads.dataset.dataset_tables_dict import get_tables_for_benchmark

logger = logging.getLogger(__name__)

# Pandas dtype -> ClickHouse type mapping
_DTYPE_MAP: dict[str, str] = {
    "int8": "Int8",
    "int16": "Int16",
    "int32": "Int32",
    "int64": "Int64",
    "uint8": "UInt8",
    "uint16": "UInt16",
    "uint32": "UInt32",
    "uint64": "UInt64",
    "float32": "Float32",
    "float64": "Float64",
    "bool": "UInt8",
    "object": "String",
}


def _pandas_dtype_to_clickhouse(dtype) -> str:
    name = str(dtype)
    if name in _DTYPE_MAP:
        return _DTYPE_MAP[name]
    if name.startswith("datetime64"):
        return "DateTime"
    if name.startswith("int"):
        return "Int64"
    if name.startswith("uint"):
        return "UInt64"
    if name.startswith("float"):
        return "Float64"
    return "String"


class ClickHouseRunner:
    """Benchmarks ClickHouse via its HTTP interface.

    Runs ClickHouse in a Docker container. Tables are stored using the Memory
    engine (fully in-RAM). Supports core pinning via ``--cpuset-cpus`` for
    consistent benchmarking results.

    One ClickHouse database is created per scale factor
    (e.g. ``tpch_sf1``, ``ceb_sf2``) so that table names inside each database
    match the SQL query references exactly.
    """

    name = "ClickHouse"

    def __init__(
        self,
        parquet_path: Path,
        benchmark: str,
        scale_factors: list[float],
        host: str = "localhost",
        port: int = 8123,
        user: str = "default",
        password: str = "clickhouse",
        auto_start_container: bool = True,
        container_name: str = "clickhouse-bench",
        container_image: str = "clickhouse/clickhouse-server:latest",
        container_num_cores: int = 1,
        container_pin_core_id: int = 4,
    ) -> None:
        self._parquet_path = parquet_path
        self._benchmark = benchmark
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._auto_start_container = auto_start_container
        self._container_name = container_name
        self._container_image = container_image
        self._container_num_cores = container_num_cores
        self._container_pin_core_id = container_pin_core_id

        # Per-SF clients (each targeting its own database).
        self._clients: dict[float, Client] = {}
        self._loaded_sf: float | None = None
        self._current_client: Optional[Client] = None
        self._duckdb_con: Optional[duckdb.DuckDBPyConnection] = None

        if self._container_pin_core_id < 0:
            raise ValueError("container_pin_core_id must be >= 0.")

        if self._auto_start_container:
            # Start once with CPU pinning already applied — Memory engine tables
            # are not persisted across restarts, so we cannot reload after restart.
            self._restart_container(query_mode=True)

        for scale_factor in scale_factors:
            self._load_sf(scale_factor)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_sf(scale_factor: float) -> str:
        if int(scale_factor) == scale_factor:
            return str(int(scale_factor))
        return str(scale_factor).replace(".", "_")

    def _db_name_for_sf(self, scale_factor: float) -> str:
        return f"{self._benchmark}_sf{self._format_sf(scale_factor)}"

    def _make_client(self, database: str = "default") -> Client:
        return clickhouse_connect.get_client(
            host=self._host,
            port=self._port,
            username=self._user,
            password=self._password,
            database=database,
        )

    def _admin_client(self) -> Client:
        return self._make_client("default")

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    def _build_run_cmd(self, query_mode: bool) -> list[str]:
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._container_name,
        ]
        if query_mode:
            end_core = self._container_pin_core_id + self._container_num_cores - 1
            cpuset = (
                str(self._container_pin_core_id)
                if self._container_num_cores == 1
                else f"{self._container_pin_core_id}-{end_core}"
            )
            cmd.extend(
                [
                    "--cpus",
                    str(self._container_num_cores),
                    "--cpuset-cpus",
                    cpuset,
                ]
            )
        cmd.extend(
            [
                "-p",
                f"{self._port}:8123",
                "-e",
                f"CLICKHOUSE_USER={self._user}",
                "-e",
                f"CLICKHOUSE_PASSWORD={self._password}",
                "--ulimit",
                "nofile=262144:262144",
                self._container_image,
            ]
        )
        return cmd

    def _restart_container(self, query_mode: bool) -> None:
        if self._host not in {"127.0.0.1", "localhost"}:
            return

        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(cmd, check=False, capture_output=True, text=True)

        _run(["docker", "rm", "-f", self._container_name])
        run_cmd = self._build_run_cmd(query_mode=query_mode)
        run = _run(run_cmd)
        if run.returncode != 0:
            raise RuntimeError(
                f"Failed to start ClickHouse container '{self._container_name}': "
                f"{run.stderr.strip()}"
            )
        logger.info(
            "Started ClickHouse container '%s' in %s mode.",
            self._container_name,
            "query" if query_mode else "load",
        )
        self._wait_until_ready()

    def _wait_until_ready(self, timeout_s: float = 60.0) -> None:
        import clickhouse_connect

        deadline = time.time() + timeout_s
        last_err: str | None = None
        while time.time() < deadline:
            try:
                client = clickhouse_connect.get_client(
                    host=self._host,
                    port=self._port,
                    username=self._user,
                    password=self._password,
                )
                client.ping()
                return
            except Exception as exc:
                last_err = str(exc)
                time.sleep(0.5)
        raise RuntimeError(
            "Timed out waiting for ClickHouse to become ready."
            + (f" Last error: {last_err}" if last_err else "")
        )

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_sf(self, scale_factor: float) -> None:
        db_name = self._db_name_for_sf(scale_factor)
        tables = get_tables_for_benchmark(self._benchmark)

        admin = self._admin_client()

        # Create database if needed.
        out = admin.command(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
        logger.debug(
            "ClickHouse: CREATE DATABASE '%s' result: %s", db_name, out.__dict__
        )

        client: Client = self._make_client(db_name)

        # Check whether all tables already exist with data.
        existing = {
            row[0]
            for row in client.query(
                "SELECT name FROM system.tables WHERE database = {db:String}",
                parameters={"db": db_name},
            ).result_rows
        }
        if all(t in existing for t in tables):
            all_have_data = True
            for table in tables:
                count = client.query(f"SELECT count() FROM `{table}`").first_row[0]
                if count == 0:
                    all_have_data = False
                    break
            if all_have_data:
                logger.debug(
                    "ClickHouse: reusing existing SF%s tables in database '%s'.",
                    scale_factor,
                    db_name,
                )
                self._clients[scale_factor] = client
                return

        # (Re)load tables.
        from synnodb.workloads.workload_spec import find_sf_dir

        tier_dir = find_sf_dir(self._parquet_path, scale_factor)
        if tier_dir is None:
            raise FileNotFoundError(
                f"No tier directory for ratio/SF {scale_factor:g} under {self._parquet_path}."
            )
        for table in tqdm(
            tables,
            desc=f"Loading ClickHouse tables for {tier_dir.name} ({db_name})",
        ):
            parquet_file = tier_dir / f"{table}.parquet"
            self._load_table(client, table, parquet_file)

        self._clients[scale_factor] = client
        logger.info(
            "ClickHouse: loaded SF%s into database '%s'.", scale_factor, db_name
        )

    def _load_table(self, client: Client, table: str, parquet_file: Path) -> None:
        """Read parquet via DuckDB and insert into a Memory-engine ClickHouse table."""
        if self._duckdb_con is None:
            self._duckdb_con = duckdb.connect(":memory:")

        df: pd.DataFrame = self._duckdb_con.execute(
            f"SELECT * FROM read_parquet('{parquet_file.as_posix()}')"
        ).df()

        # Convert object columns containing None to string to avoid type issues.
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].fillna("").astype(str)

        # Build CREATE TABLE DDL with Memory engine.
        col_defs = ", ".join(
            f"`{col}` {_pandas_dtype_to_clickhouse(df[col].dtype)}"
            for col in df.columns
        )
        client.command(f"DROP TABLE IF EXISTS `{table}`")
        client.command(f"CREATE TABLE `{table}` ({col_defs}) ENGINE = Memory")

        client.insert_df(table, df)
        logger.debug("ClickHouse: inserted %d rows into '%s'.", len(df), table)

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def _switch_sf(self, scale_factor: float) -> None:
        if scale_factor not in self._clients:
            self._load_sf(scale_factor)
        self._current_client = self._clients[scale_factor]
        self._loaded_sf = scale_factor
        logger.debug(
            "ClickHouse: switched to SF%s database '%s'.",
            scale_factor,
            self._db_name_for_sf(scale_factor),
        )

    def run_scale_factor(
        self,
        scale_factor: float,
        query_list: list[str],
        sql_list: list[str],
        args_list: list[str],
    ) -> list[float | None]:
        if self._loaded_sf != scale_factor:
            self._switch_sf(scale_factor)

        assert self._current_client is not None, "ClickHouse client not initialized."

        results: list[float | None] = []
        for sql in sql_list:
            t0 = time.perf_counter()

            # split queries on semicolons and run sequentially to support multiple statements per query file
            sql_statements = sql.split(";")
            for stmt in sql_statements:
                stmt = stmt.strip()
                if stmt:
                    try:
                        self._current_client.query(stmt)
                    except Exception as exc:
                        logger.error(
                            "Error running ClickHouse query '%s' for SF%s: %s",
                            stmt,
                            scale_factor,
                            exc,
                        )
                        raise

            results.append((time.perf_counter() - t0) * 1000.0)
        return results

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(cmd, check=False, capture_output=True, text=True)

        _run(["docker", "stop", self._container_name])
        _run(["docker", "rm", "-f", self._container_name])
        logger.info(
            "Stopped and deleted ClickHouse container '%s'.", self._container_name
        )
