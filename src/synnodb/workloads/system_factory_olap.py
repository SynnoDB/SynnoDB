from __future__ import annotations

from typing import Any

from synnodb.observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from synnodb.workloads.system_factory import System, SystemFactory
from synnodb.workloads.workload_provider import (
    ExecSettings,
    GeneralSystemConfig,
    Workload,
    WorkloadId,
)
from synnodb.utils.utils import DataSource, ServeFrom
from synnodb.workloads.workload_provider_olap import (
    OLAPExecSettings,
    OLAPWorkload,
    OLAPWorkloadProvider,
    validate_storage_combo,
)

DUCKDB_PIN_CORE = 3
UMBRA_PIN_CORE = 3


class OLAPSystemFactory(SystemFactory):
    def __init__(self) -> None:
        # (sf, duckdb_source) -> instance. The source representation is part of the key because
        # it changes how the DuckDBConnectionManager materializes data (parquet views vs flat
        # from parquet vs flat from subset.duckdb); a manager for one must not be reused for another.
        self.duckdb_cons: dict[tuple[float, str], DuckDBConnectionManager] = dict()
        self.umbra_runner: "Any | None" = None

    def get_system(
        self,
        system_name: System,
        benchmark: Workload,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ) -> "DuckDBConnectionManager | Any":
        assert isinstance(exec_settings, OLAPExecSettings), (
            "exec_settings must be an instance of OLAPExecSettings"
        )
        # Accept a built-in OLAPWorkload enum member or a registered (bring-your-own)
        # WorkloadId; both expose `.value`, which is all the downstream consumers use.
        assert isinstance(benchmark, (OLAPWorkload, WorkloadId)), (
            f"benchmark must be an OLAPWorkload or registered WorkloadId, got {type(benchmark)}"
        )

        if system_name == System.DUCKDB:
            # The run's data source describes the bespoke engine; the DuckDB oracle mirrors it:
            # a parquet run streams parquet views, a DuckDB-native run materializes flat tables
            # from the subset's subset.duckdb, and a flat/bespoke run materializes flat from parquet -
            # the ground-truth answer for whatever the engine ingested.
            duckdb_source = (
                exec_settings.data_source
                if exec_settings.data_source in (DataSource.PARQUET, DataSource.DUCKDB)
                else DataSource.FLAT
            )
            validate_storage_combo(
                System.DUCKDB, exec_settings.db_storage, duckdb_source
            )
            run_on_parquet = duckdb_source == DataSource.PARQUET
            serve_from = (
                ServeFrom.DUCKDB
                if duckdb_source == DataSource.DUCKDB
                else ServeFrom.PARQUET
            )

            # Cache by (sf, source representation): the same SF can be queried against parquet
            # views, flat-from-parquet, or flat-from-subset.duckdb - distinct physical
            # representations that must not share a cached connection. Thread count is
            # deliberately NOT part of this key: it's a mutable property of the connection
            # (resynced below), not a distinct physical representation - keying on it would
            # rebuild (and for IN_MEMORY, re-materialize) the whole dataset on every thread-count
            # change instead of just re-issuing PRAGMA threads.
            if general_system_config.num_threads == 1:
                val_pin_worker = True
                val_pin_core = DUCKDB_PIN_CORE
            else:
                val_pin_worker = False
                val_pin_core = None

            con_key = (exec_settings.scale_factor, duckdb_source.value)
            if con_key not in self.duckdb_cons:
                self.duckdb_cons[con_key] = DuckDBConnectionManager(
                    benchmark=benchmark,
                    dataset_tables=OLAPWorkloadProvider._dataset_tables(benchmark),
                    pre_load_duckdb_tables=False,
                    parquet_path=exec_settings.parquet_dir.parent,
                    sf=exec_settings.scale_factor,
                    pin_worker=val_pin_worker,
                    pin_core=val_pin_core,
                    num_threads=general_system_config.num_threads,
                    db_storage=exec_settings.db_storage,
                    disk_db_dir=exec_settings.disk_db_dir,
                    run_duckdb_on_parquet=run_on_parquet,
                    serve_from=serve_from,
                )
            else:
                # The run tool's thread count may have changed since this connection was built
                # (e.g. a stage switching from serial generation to multi-threaded validation) -
                # DuckDB must always run at the same thread count as the engine under test.
                self.duckdb_cons[con_key].set_thread_config(
                    num_threads=general_system_config.num_threads,
                    pin_worker=val_pin_worker,
                    pin_core=val_pin_core,
                )
            return self.duckdb_cons[con_key]
        elif system_name == System.UMBRA:
            if self.umbra_runner is None:
                from synnodb.observability.benchmark.systems.umbra import UmbraRunner

                self.umbra_runner = UmbraRunner(
                    parquet_path=exec_settings.parquet_dir.parent,
                    benchmark=benchmark,
                    scale_factors=[exec_settings.scale_factor],
                    container_num_cores=general_system_config.num_threads,
                    container_pin_core_id_start=general_system_config.core_ids[0]
                    if general_system_config.core_ids
                    else UMBRA_PIN_CORE,
                    allow_auto_restarts=True,
                    db_storage=exec_settings.db_storage,
                    disk_db_dir=exec_settings.disk_db_dir,
                )

            return self.umbra_runner
        else:
            raise ValueError(f"Unsupported system: {system_name}")
