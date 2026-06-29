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
from synnodb.workloads.workload_provider_olap import (
    OLAPExecSettings,
    OLAPWorkload,
    OLAPWorkloadProvider,
)

DUCKDB_PIN_CORE = 3
UMBRA_PIN_CORE = 3


class OLAPSystemFactory(SystemFactory):
    # sf -> instance
    duckdb_cons: dict[float, DuckDBConnectionManager] = dict()
    umbra_runner: "Any | None" = None

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
            if exec_settings.scale_factor not in self.duckdb_cons:
                if general_system_config.num_threads == 1:
                    val_pin_worker = True
                    val_pin_core = DUCKDB_PIN_CORE
                else:
                    val_pin_worker = False
                    val_pin_core = None

                self.duckdb_cons[exec_settings.scale_factor] = DuckDBConnectionManager(
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
                )
            return self.duckdb_cons[exec_settings.scale_factor]
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
