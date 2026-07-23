import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

import psycopg2
from tqdm import tqdm

from synnodb.utils.drop_caches import drop_os_caches, is_memory_backed
from synnodb.utils.utils import DBStorage, create_dir_and_set_permissions
from synnodb.workloads.workload_provider import Workload
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

logger = logging.getLogger(__name__)


class UmbraRunner:
    """Benchmarks Umbra via its PostgreSQL-wire-compatible server.

    On init, connects to a running Umbra Docker container and preloads one
    dedicated Umbra database per scale factor (e.g. ``tpch_sf1``, ``ceb_sf2``).
    Each database is initialized from benchmark schema SQL and then populated
    from parquet files via DuckDB parquet->CSV->COPY fallback. Query execution then
    only switches the active database/connection by scale factor.

    With ``in_memory=False``, the container is restarted and host OS page
    caches are dropped before every query so each query reads fully from
    disk (cold buffer pool + cold page cache). This requires a local host.
    """

    name = "Umbra"

    def __init__(
        self,
        parquet_path: Path,
        benchmark: Workload,
        scale_factors: list[float],
        db_storage: DBStorage,
        disk_db_dir: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "postgres",
        password: str = "postgres",
        setup: bool = False,
        allow_auto_restarts: bool = False,
        container_name: str = "umbradb",
        container_image: str = "umbradb/umbra:latest",
        container_data_dir: Path | None = None,
        container_num_cores: int = 1,
        container_pin_core_id_start: int = 4,
    ) -> None:
        self._parquet_path = parquet_path
        self._benchmark = benchmark
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._loaded_sf: float | None = None
        self._db_names: dict[float, str] = {}
        self._conns: dict[float, psycopg2.extensions.connection] = {}
        self._con: psycopg2.extensions.connection | None = None
        self._duckdb_con = None
        self._db_storage = db_storage
        self._disk_db_dir = disk_db_dir
        self._container_name = container_name
        self._container_image = container_image
        self._scale_factors = scale_factors

        # accept any registered workload identity (a WorkloadId, the norm, or a workload-package
        # enum member); both expose `.value`, which is all the downstream consumers use
        from synnodb.workloads.workload_provider import WorkloadId

        assert isinstance(benchmark, (Workload, WorkloadId)), (
            f"benchmark must be a Workload or registered WorkloadId, got {type(benchmark)}"
        )
        self.dataset_tables = OLAPWorkloadProvider._dataset_tables(benchmark)
        self.dataset_schema = OLAPWorkloadProvider._get_dataset_schema(benchmark)

        if self._db_storage in [DBStorage.LABSTORE, DBStorage.SSD] and host not in {
            "127.0.0.1",
            "localhost",
        }:
            raise ValueError(
                "in_memory=False requires a local Umbra host so the container "
                "can be restarted between queries to clear the buffer pool."
            )
        self._container_data_dir = (
            container_data_dir
            if container_data_dir is not None
            else (Path.home() / "umbra-db")
        )
        create_dir_and_set_permissions(self._container_data_dir)
        if self._db_storage in [
            DBStorage.LABSTORE,
            DBStorage.SSD,
        ] and is_memory_backed(self._container_data_dir):
            raise RuntimeError(
                f"Umbra data directory is memory-backed: {self._container_data_dir}. "
                "Use container_data_dir on persistent storage for disk-based benchmarks."
            )

        self._container_num_cores = container_num_cores
        self._container_pin_core_id_start = container_pin_core_id_start

        if self._container_num_cores < 1:
            raise ValueError("container_num_cores must be >= 1.")
        if self._container_pin_core_id_start < 0:
            raise ValueError("container_pin_core_id_start must be >= 0.")

        self._allow_auto_restarts = allow_auto_restarts

        # setup umbra (load data, connections, ...)
        self.setup_done = False
        if setup:
            self.setup()

    def setup(self):
        if self.setup_done:
            logger.warning(
                "Calling setup again on UmbraRunner; skipping since setup is already done."
            )
            return

        if self._allow_auto_restarts:
            # restart container into unrestricted mode for loading; we'll restart in pinned/limited mode after loading.
            self._restart_container(query_mode=False)

        try:
            self._admin_con = self._create_admin_con()
        except psycopg2.OperationalError as exc:
            logger.warning(
                "Failed to connect to Umbra at %s:%d: %s. Attempting to auto-start container and retry connection.",
                self._host,
                self._port,
                exc,
            )
            self._restart_container(query_mode=False)

            self._admin_con = self._create_admin_con()

        self._admin_con.autocommit = True
        logger.debug("Connected to Umbra at %s:%d", self._host, self._port)

        for scale_factor in self._scale_factors:
            self._load_sf(scale_factor)

        if self._allow_auto_restarts:
            # Loading is done in unrestricted mode; restart in pinned/limited mode.
            self._restart_container(query_mode=True)
            try:
                self._admin_con.close()
            except Exception:
                pass
            self._admin_con = self._create_admin_con()
            self._admin_con.autocommit = True

            self._close_scale_factor_connections()

        self.setup_done = True

    def _close_scale_factor_connections(self) -> None:
        for con in self._conns.values():
            try:
                con.close()
            except Exception:
                pass
        self._conns.clear()
        self._con = None
        self._loaded_sf = None

    def _close_all_connections(self) -> None:
        self._close_scale_factor_connections()
        if hasattr(self, "_admin_con"):
            try:
                self._admin_con.close()
            except Exception:
                pass

    @staticmethod
    def _format_sf(scale_factor: float) -> str:
        if int(scale_factor) == scale_factor:
            return str(int(scale_factor))
        return str(scale_factor).replace(".", "_")

    def _db_name_for_sf(self, scale_factor: float) -> str:
        return f"{self._benchmark}_sf{self._format_sf(scale_factor)}"

    def _ensure_admin_con(self) -> None:
        if not hasattr(self, "_admin_con"):
            self._admin_con = self._create_admin_con()
            self._admin_con.autocommit = True

    def _load_sf(self, scale_factor: float, verbose: bool = True) -> None:
        self._ensure_admin_con()
        db_name = self._db_name_for_sf(scale_factor)
        self._db_names[scale_factor] = db_name
        tables = self.dataset_tables

        con = None
        if self._database_exists(db_name):
            con = self._create_db_con(db_name)
            con.autocommit = True
            if self._has_all_tables_with_data(con, tables):
                self._conns[scale_factor] = con
                if verbose:
                    logger.debug(
                        "Umbra: reusing existing SF%s database '%s' (tables already loaded).",
                        scale_factor,
                        db_name,
                    )
                return

            logger.info(
                "Umbra: existing database '%s' missing data; reloading tables in-place.",
                db_name,
            )
            self._reset_existing_tables(con, tables)
        else:
            admin_cur = self._admin_con.cursor()
            try:
                admin_cur.execute(f"CREATE DATABASE {db_name}")
            except Exception as exc:
                logger.error(
                    "Failed to create database '%s' for SF%s: %s",
                    db_name,
                    scale_factor,
                    exc,
                )
                raise exc
            admin_cur.close()

            con = self._create_db_con(db_name)
            con.autocommit = True

        assert con is not None
        cur = con.cursor()

        cur.execute(self.dataset_schema)

        from synnodb.workloads.workload_spec import find_sf_dir

        subset_dir = find_sf_dir(self._parquet_path, scale_factor)
        if subset_dir is None:
            raise FileNotFoundError(
                f"No subset directory for fraction/SF {scale_factor:g} under {self._parquet_path}."
            )
        for table in tqdm(
            tables,
            desc=f"Loading Umbra tables for {subset_dir.name} ({db_name})",
        ):
            parquet_file = subset_dir / f"{table}.parquet"
            self._copy_table_via_duckdb_csv(
                cur=cur,
                table=table,
                parquet_file=parquet_file,
            )

        cur.close()
        self._conns[scale_factor] = con
        logger.info("Umbra: loaded SF%s into database '%s'.", scale_factor, db_name)

    def _ensure_container_running(self, query_mode: bool) -> None:
        if self._host not in {"127.0.0.1", "localhost"}:
            logger.info(
                "Umbra auto-start skipped for non-local host '%s'.",
                self._host,
            )
            return

        self._container_data_dir.mkdir(parents=True, exist_ok=True)

        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )

        # If already running, nothing to do.
        ps = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"name=^{self._container_name}$",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ]
        )
        if ps.returncode == 0 and self._container_name in ps.stdout.splitlines():
            logger.info(
                "Umbra container '%s' is already running.",
                self._container_name,
            )
            return

        # If container exists but is stopped, start it.
        inspect = _run(["docker", "inspect", self._container_name])
        if inspect.returncode == 0:
            start = _run(["docker", "start", self._container_name])
            if start.returncode != 0:
                raise RuntimeError(
                    f"Failed to start existing Umbra container '{self._container_name}': "
                    f"{start.stderr.strip()}"
                )
            logger.info("Started existing Umbra container '%s'.", self._container_name)
            self._wait_until_ready()
            return

        run_cmd = self._build_run_cmd(query_mode=query_mode)
        run = _run(run_cmd)
        if run.returncode != 0:
            raise RuntimeError(
                f"Failed to start Umbra container '{self._container_name}': "
                f"{run.stderr.strip()}"
            )
        logger.info(
            "Started Umbra container '%s' with volume '%s' (%s mode).",
            self._container_name,
            self._container_data_dir,
            "query" if query_mode else "load",
        )
        self._wait_until_ready()

    def _build_run_cmd(self, query_mode: bool) -> list[str]:
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._container_name,
        ]
        if query_mode:
            end_core = self._container_pin_core_id_start + self._container_num_cores - 1
            cpuset = (
                str(self._container_pin_core_id_start)
                if self._container_num_cores == 1
                else f"{self._container_pin_core_id_start}-{end_core}"
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
                "-v",
                f"{self._container_data_dir.as_posix()}:/var/db",
                "-p",
                f"{self._port}:5432",
                "--ulimit",
                "nofile=1048576:1048576",
                "--ulimit",
                "memlock=8388608:8388608",
                self._container_image,
            ]
        )
        return cmd

    def _restart_container(self, query_mode: bool) -> None:
        if self._host not in {"127.0.0.1", "localhost"}:
            return

        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )

        rm = _run(["docker", "rm", "-f", self._container_name])
        logger.debug(
            f"docker rm stdout: {rm.stdout.strip()} / stderr: {rm.stderr.strip()}"
        )
        run_cmd = self._build_run_cmd(query_mode=query_mode)
        run = _run(run_cmd)
        logger.debug(
            f"docker run stdout: {run.stdout.strip()} / stderr: {run.stderr.strip()}"
        )
        if run.returncode != 0:
            raise RuntimeError(
                f"Failed to restart Umbra container '{self._container_name}': "
                f"{run.stderr.strip()}"
            )
        logger.info(
            "Restarted Umbra container '%s' in %s mode.",
            self._container_name,
            "query" if query_mode else "load",
        )
        self._wait_until_ready()

    def _wait_until_ready(self, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        last_err: str | None = None
        while time.time() < deadline:
            try:
                con = self._create_admin_con()
                con.close()
                return
            except Exception as exc:
                last_err = str(exc)
                time.sleep(0.5)
        raise RuntimeError(
            "Timed out waiting for Umbra to become ready."
            + (f" Last error: {last_err}" if last_err else "")
        )

    def _database_exists(self, db_name: str) -> bool:
        cur = self._admin_con.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_name,),
        )
        exists = cur.fetchone() is not None
        cur.close()
        return exists

    @staticmethod
    def _has_all_tables_with_data(con, tables: list[str]) -> bool:
        cur = con.cursor()
        try:
            # Umbra catalog coverage varies by version, so probe tables directly.
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                cur.execute(f"SELECT 1 FROM {quoted} LIMIT 1")
                if cur.fetchone() is None:
                    return False
            return True
        except Exception:
            return False
        finally:
            cur.close()

    @staticmethod
    def _reset_existing_tables(con, tables: list[str]) -> None:
        cur = con.cursor()
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            cur.execute(f"DROP TABLE IF EXISTS {quoted}")
        cur.close()

    def _switch_sf(self, scale_factor: float) -> None:
        if self._loaded_sf == scale_factor:
            # do not switch: already on the right SF
            return

        if scale_factor not in self._conns:
            self._load_sf(scale_factor, verbose=False)
        self._con = self._conns[scale_factor]
        self._loaded_sf = scale_factor
        logger.debug(
            "Umbra: switched to SF%s database '%s'.",
            scale_factor,
            self._db_names[scale_factor],
        )

    def _copy_table_via_duckdb_csv(self, cur, table: str, parquet_file: Path) -> None:
        import duckdb

        if self._duckdb_con is None:
            self._duckdb_con = duckdb.connect(database=":memory:")

        # Umbra runs inside Docker; stage CSV in the mounted volume so the DB process
        # can open it (container path: /var/db/...).
        host_stage_dir = self._container_data_dir / ".ingest_tmp"
        host_stage_dir.mkdir(parents=True, exist_ok=True)

        file_id = uuid.uuid4().hex
        host_csv = host_stage_dir / f"{self._benchmark}_{table}_{file_id}.csv"
        container_csv = f"/var/db/.ingest_tmp/{host_csv.name}"

        parquet_literal = parquet_file.as_posix().replace("'", "''")
        host_csv_literal = host_csv.as_posix().replace("'", "''")
        container_csv_literal = container_csv.replace("'", "''")

        try:
            self._duckdb_con.execute(
                f"COPY (SELECT * FROM read_parquet('{parquet_literal}')) "
                f"TO '{host_csv_literal}' (FORMAT CSV, HEADER FALSE, NULL '')"
            )

            cur.execute(
                f"COPY {table} FROM '{container_csv_literal}' "
                "(FORMAT CSV, HEADER FALSE)"
            )
        finally:
            try:
                os.remove(host_csv)
            except FileNotFoundError:
                pass

    def run_scale_factor(
        self,
        scale_factor: float,
        query_list: list[str],
        sql_list: list[str],
        args_list: list[str],
    ) -> list[tuple[dict, float]]:
        if self._db_storage == DBStorage.IN_MEMORY:
            # in-memory: just switch connections if needed; no restarts or cache drops.
            if self._loaded_sf != scale_factor:
                self._switch_sf(scale_factor)
        elif self._db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
            # ssd based: restart container and drop caches to ensure cold reads from disk for every query.
            assert scale_factor in self._db_names, (
                f"SF{scale_factor} not loaded; cannot run query. Loaded SFs: {list(self._db_names.keys())}"
            )
            # if scale_factor not in self._db_names:
            #     self._load_sf(scale_factor, verbose=False)
        else:
            raise ValueError(f"Unknown db source: {self._db_storage}")

        results: list[tuple[dict, float]] = []
        for sql in tqdm(sql_list, desc=f"Umbra running queries (SF{scale_factor})"):
            if self._db_storage == DBStorage.IN_MEMORY:
                pass
            elif self._db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
                # Cold-start every query: restart the container (drops Umbra's
                # buffer pool) and drop OS page caches on the host (evicts the
                # bind-mounted data files) so the query reads fully from disk.
                self._cold_restart_for_query(scale_factor)
            else:
                raise ValueError(f"Unknown db source: {self._db_storage}")

            # create new cursor
            assert self._con is not None, "Umbra connection not initialized."
            cur = self._con.cursor()
            try:
                cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
                row = cur.fetchone()
                if row is None:
                    raise ValueError("EXPLAIN returned no rows.")
                plan = json.loads(row[0])

                analyze_pipelines = plan["analyzePlanPipelines"]
                runtime_ms = (
                    sum([p["duration"] for p in analyze_pipelines]) / 1000
                )  # originally in ns, convert to ms
                results.append((plan, runtime_ms))
            finally:
                cur.close()

        return results

    def _cold_restart_for_query(self, scale_factor: float) -> None:
        db_name = self._db_names[scale_factor]

        self._close_all_connections()

        # Restart the container to clear Umbra's buffer pool. This is necessary for a cold-start benchmark that reflects the performance of reading from disk with an empty buffer pool.
        self._restart_container(query_mode=True)

        self._admin_con = self._create_admin_con()
        self._admin_con.autocommit = True

        # Reconnect directly to the existing per-SF database; do NOT go through
        # _load_sf, which probes every table with SELECT and would warm caches.
        con = self._create_db_con(db_name)
        con.autocommit = True
        self._conns[scale_factor] = con
        self._con = con
        self._loaded_sf = scale_factor

        # Drop OS page caches *after* setup so any pages read by container
        # startup or connection establishment are evicted before the query.
        drop_os_caches()

    def _create_admin_con(self):
        return psycopg2.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            dbname="postgres",
        )

    def _create_db_con(self, db_name: str):
        return psycopg2.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            dbname=db_name,
        )

    def stop(self) -> None:
        if not self.setup_done:
            # umbra was not started in the first place.
            return

        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )

        _run(["docker", "stop", self._container_name])

        _run(["docker", "rm", "-f", self._container_name])

        logger.info("Stopped and deleted Umbra container")
