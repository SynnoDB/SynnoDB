import enum
from abc import abstractmethod
from dataclasses import dataclass

from synnodb.ram_check import RamCheck
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils import utils


# Base number of parameter instantiations generated per query for the correctness
# sweep (and pre-inferred for templated bring-your-own queries). Override per run via
# OLAPWorkloadProvider(num_instantiations=...) / set_num_instantiations(...).
DEFAULT_NUM_INSTANTIATIONS = 10


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


class WorkloadId(str):
    """A workload identity for registered (e.g. bring-your-own) workloads that are not
    members of a fixed `Workload` enum. It is a plain `str` (so it serializes cleanly
    into cache keys) that also exposes `.value`, matching the enum interface the
    framework reads — so a data-registered workload can drive the pipeline without
    editing any enum.
    """

    @property
    def value(self) -> str:
        return str(self)


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

    def preflight_ram_check(self) -> RamCheck | None:
        """The host-RAM check for the largest dataset this provider could load
        fully into memory, or None when there is nothing to gate (disk-backed
        storage, or no measurable dataset present).

        The provider owns what a run loads: subclasses that ingest a dataset into
        RAM override this to point the check at the files they may load (a
        scale-factor workload picks a scale factor's parquet dir; a workload with
        no scale-factor notion points at its data directory). The pipeline calls
        this before any generation work and refuses to start a run whose check is
        insufficient."""
        return None

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


def _gen_req_id(
    qid_str: str, placeholders: dict, request_disambiguator: str | int | None
) -> str:
    # Id used to name the result file (result_<req_id>.csv). Derived deterministically
    # from the query, its placeholder values, and the per-repetition disambiguator so a
    # given (query, params, rep) always maps to the same result file - reproducible runs
    # and stable debugging. It is kept out of the LLM-facing sample args (see
    # format_sample_args) and out of every cache key (validate / run / query-execution
    # caches all omit query_args); the disambiguator makes otherwise-identical
    # repetitions distinct.
    #
    # Hash the already-rendered placeholder *string* (not the raw objects): placeholder
    # values may be non-JSON types such as Decimal when bound from DuckDB, and rendering
    # first keeps this robust to any value type while staying stable across runs.
    payload = "|".join(
        (
            qid_str,
            _format_placeholder_values(placeholders),
            "" if request_disambiguator is None else str(request_disambiguator),
        )
    )
    return f"req_{qid_str}_{utils.sha256(payload)[:12]}"


def _format_placeholder_values(placeholders: dict) -> str:
    # Don't add double quotes to IN lists (they start with '(')
    formatted_values = []
    for value in placeholders.values():
        if isinstance(value, str) and value.startswith("("):
            # IN list - don't add quotes
            formatted_values.append(value)
        else:
            # Regular value - add quotes
            formatted_values.append(f'"{value}"')
    return " ".join(formatted_values)


def format_sample_args(qid_str: str, placeholders: dict) -> str:
    # LLM-facing rendering: the example instantiation of the query placeholders.
    # Deliberately omits the execution-time req_id (see format_args_element) so that
    # this non-deterministic plumbing token never enters the LLM prompt / cache key.
    return f"{qid_str} {_format_placeholder_values(placeholders)}"


def format_args_element(
    qid_str: str, placeholders: dict, request_disambiguator: str | int | None = None
) -> str:
    # Execution wire format: includes the req_id used at runtime to name the
    # result file (result_<req_id>.csv) so outputs can be mapped back to queries.
    # The req_id is deterministic in (qid, placeholders, request_disambiguator); pass a
    # distinct disambiguator per repetition so repeated identical queries get distinct
    # result files.
    req_id = _gen_req_id(qid_str, placeholders, request_disambiguator)
    return f"{qid_str} {req_id} {_format_placeholder_values(placeholders)}"
