from abc import abstractmethod

from workloads.workload_provider import ExecSettings, GeneralSystemConfig, Workload


class SystemFactory:
    @abstractmethod
    def get_system(
        self,
        system_name: str,
        benchmark: Workload,
        exec_settings: ExecSettings,
        general_system_config: GeneralSystemConfig,
    ):
        raise NotImplementedError
