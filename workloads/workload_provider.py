import random
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime

from tools.run_tool_mode import RunToolMode
from utils import utils


@dataclass
class ExecSettings:
    pass


@dataclass
class QueryEntry:
    query_id: str
    sql: str
    query_args: str
    placeholder: dict[str, str]
    order_by_info: list[tuple[str, str]]


@dataclass
class WrapperConfig:
    memory_limit_mb: int | None

    def to_dict(self) -> dict:
        return {"memory_limit_mb": self.memory_limit_mb}


@dataclass
class QueryBatch:
    query_list: list[QueryEntry]
    exec_settings: ExecSettings
    cli_call_args: str
    wrapper_config: WrapperConfig
    timeout_s: int
    extra_env: dict[str, str]

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
    def produce_workload(
        self, run_mode: RunToolMode, query_ids: list[str] | None = None
    ) -> list[QueryBatch]:
        raise NotImplementedError("Subclasses must implement produce_workload")


# separate args by , and add double quotes around each arg (except for IN lists which start with '(')
def format_args_string(
    query_list: list[str], placeholder_list: list[dict]
) -> list[str]:
    args_list = []
    for qid_str, placeholders in zip(query_list, placeholder_list):
        args_list.append(format_args_element(qid_str, placeholders))
    return args_list


def format_args_element(qid_str: str, placeholders: dict) -> str:
    # generate random req-id
    # req_id = date_time + random int, to ensure uniqueness across different runs and queries
    req_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(1, 100000)}"

    # Don't add double quotes to IN lists (they start with '(')
    formatted_values = []
    for value in placeholders.values():
        if isinstance(value, str) and value.startswith("("):
            # IN list - don't add quotes
            formatted_values.append(value)
        else:
            # Regular value - add quotes
            formatted_values.append(f'"{value}"')

    return f"{qid_str} {req_id} {' '.join(formatted_values)}"
