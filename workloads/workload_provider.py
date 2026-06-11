from abc import abstractmethod
from dataclasses import dataclass

from tools.run import RunToolMode
from utils import utils


class WorkloadProvider:
    benchmark_name: str
    query_ids: list[str]
    sql_dict: dict[str, str]
    memory_limit_mb: int | None

    def __init__(
        self,
        benchmark_name: str,
        query_ids: list[str],
        sql_dict: dict[str, str],
        memory_limit_mb: int | None = None,
    ):
        self.benchmark_name = benchmark_name
        self.query_ids = query_ids
        self.sql_dict = sql_dict
        self.memory_limit_mb = memory_limit_mb

    @abstractmethod
    def get_placeholders_fn(self):
        raise NotImplementedError("Subclasses must implement get_placeholders_fn")

    @abstractmethod
    def produce_workload(self, run_mode: RunToolMode):
        raise NotImplementedError("Subclasses must implement produce_workload")


@dataclass
class QueryEntry:
    query_id: str
    sql: str
    query_args: str


@dataclass
class WrapperConfig:
    memory_limit_mb: int | None

    def to_dict(self) -> dict:
        return {"memory_limit_mb": self.memory_limit_mb}


@dataclass
class QueryBatch:
    query_list: list[QueryEntry]
    log_info: dict[str, str]
    cli_call_args: str
    wrapper_config: WrapperConfig
    timeout_s: int
    extra_env: dict[str, str] = dict()

    def to_dict(self) -> str:
        # Create a stable hash of the query batch by converting it to a JSON string with sorted keys
        batch_dict = {
            "query_list": [
                {
                    "query_id": entry.query_id,
                    "sql": entry.sql,
                    "query_args": entry.query_args,
                }
                for entry in self.query_list
            ],
            # "log_info": self.log_info, --- IGNORE --- log info is only for reporting, has no influence on execution
            "cli_call_args": self.cli_call_args,
            "wrapper_config": self.wrapper_config.to_dict(),
            "timeout_s": self.timeout_s,
            "extra_env": self.extra_env,
        }
        return utils.stable_json(batch_dict)
