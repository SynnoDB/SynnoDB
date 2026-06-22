import enum
import random
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime

from tools.run_tool_mode import RunToolMode
from utils import utils


class Workload(enum.Enum):
    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def of(cls, value: str) -> "Workload":
        for subclass in cls.__subclasses__():
            try:
                return subclass(value)
            except ValueError:
                continue
        raise ValueError(f"invalid Workload value: {value!r}")


@dataclass
class ExecSettings:
    pass


@dataclass
class QueryEntry:
    benchmark: Workload
    query_id: str
    sql: str
    query_args: str
    placeholders: dict[str, str]
    order_by_info: list[tuple[str, str]]
    # Index of this repetition within its query group. A batch repeats the same query multiple times (benchmark mode); each repetition is identical except for this index. It is the deterministic distinguisher used by the query execution cache so that every repetition gets its own cache entry (and thus its own measured runtime) without colliding on the same cache file.
    rep_index: int = 0
    num_reps: int = 1  # total number of repetitions in the batch for this query. Necessary for query execution cache.

    def hash_entries(self) -> dict:
        # validate / run cache keys: is ignoring repetition stuff, and req_id of query_args (this is not determinstic - info is covered deterministically by placeholders dict and sql)
        return {
            "benchmark": self.benchmark,
            "query_id": self.query_id,
            "sql": self.sql,
            "placeholders": self.placeholders,
            "order_by_info": self.order_by_info,
        }

    def query_exec_cache_hash_entries(self) -> dict:
        # cache keys for query-execution-cache: is taking repetitions into account
        return {
            **self.hash_entries(),
            "rep_index": self.rep_index,
            "num_reps": self.num_reps,
        }


@dataclass
class GeneralSystemConfig:
    memory_limit_mb: int | None
    num_threads: int
    core_ids: list[int] | None

    def to_dict(self) -> dict:
        return {
            "memory_limit_mb": self.memory_limit_mb,
            "num_threads": self.num_threads,
            "core_ids": self.core_ids,
        }


@dataclass
class QueryBatch:
    query_list: list[QueryEntry]
    benchmark: Workload
    exec_settings: ExecSettings
    cli_call_args: str
    general_system_config: GeneralSystemConfig
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
            "general_system_config": self.general_system_config.to_dict(),
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
        self,
        run_mode: RunToolMode,
        num_threads: int,
        core_ids: list[int] | None,
        query_ids: list[str] | None = None,
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
