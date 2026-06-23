from observability.benchmark.systems.duckdb_connection_manager import (
    DuckDBConnectionManager,
)
from observability.benchmark.systems.umbra import UmbraRunner
from utils.utils import DBStorage
from workloads.system_factory import System, SystemFactory
from workloads.workload_provider import ExecSettings, GeneralSystemConfig, Workload
from workloads.workload_provider_bff import (
    BFFExecSettings,
    BFFWorkload,
    BFFWorkloadProvider,
)

DUCKDB_PIN_CORE = 3
UMBRA_PIN_CORE = 3


class BFFSystemFactory(SystemFactory):
    # sf -> instance
    duckdb_cons: dict[float, DuckDBConnectionManager] = dict()

    def get_system(
        self,
        system_name: System,
        benchmark: Workload,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ) -> DuckDBConnectionManager | UmbraRunner:
        assert isinstance(exec_settings, BFFExecSettings), (
            "exec_settings must be an instance of BFFExecSettings"
        )
        assert isinstance(benchmark, BFFWorkload), (
            "benchmark must be an instance of BFFWorkload"
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
                    dataset_tables=BFFWorkloadProvider._dataset_tables(benchmark),
                    pre_load_duckdb_tables=False,
                    parquet_path=exec_settings.parquet_dir.parent,
                    sf=exec_settings.scale_factor,
                    pin_worker=val_pin_worker,
                    pin_core=val_pin_core,
                    num_threads=general_system_config.num_threads,
                    db_storage=DBStorage.IN_MEMORY,  # does not matter for us, we read from parquet
                    disk_db_dir=exec_settings.disk_db_dir,
                )
            return self.duckdb_cons[exec_settings.scale_factor]
        else:
            raise ValueError(f"Unsupported system: {system_name}")
