import enum
from abc import abstractmethod

from synnodb.workloads.workload_provider import (
    ExecSettings,
    GeneralSystemConfig,
    Workload,
)


class System(enum.Enum):
    DUCKDB = "duckdb"
    UMBRA = "umbra"
    BESPOKE = "bespoke"

    def __str__(self) -> str:
        return str(self.value)


class SystemFactory:
    @abstractmethod
    def get_system(
        self,
        system_name: System,
        benchmark: Workload,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ):
        raise NotImplementedError
