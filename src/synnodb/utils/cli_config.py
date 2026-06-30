from __future__ import annotations

import argparse
import enum
from dataclasses import dataclass

from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import Workload
from synnodb.workloads.workload_provider_olap import OLAPWorkload


class Usecase(enum.Enum):
    OLAP = "olap"


# DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_MODEL = "gpt-5.4"


@dataclass
class RunConfig:
    benchmark: Workload
    query_list: str
    queries_str: str
    notify: bool
    # The source run's stage name, only set for checkSfCorrectness (it replays
    # that stage's prepare).
    source_stage_name: str | None = None
    verbose: bool = False  # stream DEBUG logs to the console (logfile is always DEBUG)
    start_snapshot: str | None = None
    storage_plan_snapshot: str | None = None
    storage_plan_text: str | None = None  # storage plan content supplied directly (W&B-free path)
    max_scale_factor: int | None = None
    continue_run: bool = False
    replay: bool = False
    disable_openai_tracing: bool = False
    log_to_wandb: bool = False
    wandb_entity: str | None = None  # None -> the user's own default W&B entity
    wandb_project: str | None = None  # presence enables wandb logging
    model: str = DEFAULT_MODEL
    no_preload: bool = False
    disable_repo_sync: bool = False
    replay_cache: bool = False
    keep_csv: bool = False
    disable_valtool: bool = False
    auto_u: bool = (
        False  # automatically use all prompts - skip user confirmation prompt
    )
    auto_finish: bool = False  # automatically finish if no more prompt is found in conversation / i.e. Str-D in last iteration
    bespoke_storage: bool = (
        False  # for wandb: mark that this run is using bespoke storage plan
    )
    run_tool_offer_trace_option: bool = False  # whether to offer the option to enable tracing in the conversation (for collecting execution traces for training data generation)
    only_from_llm_cache: bool = False
    only_from_cache: bool = False  # whether to only answer from cache and not call the LLM / run tool. Will raise an error if a cache miss occurs.
    do_not_cache: bool = False  # whether to not cache any new entries
    tool_search_tool: bool = False  # whether to include the tool search tool in the agent's toolbox (for collecting training data for the tool search tool, and set functionl tools to deferred loading)
    use_supervision_agent: bool = False  # whether to use a supervision agent to guide the implementation agent - this became necessary after openai introduced gpt5.4 - then the agents suddenly all ask for user confirmation
    use_autonomy_master_prompt: bool = (
        False  # whether to prefix all prompts with an autonomy master prompt
    )
    sdk: str = "openai"
    optimize_sample_plan_source: str | None = (
        None  # "umbra" or "duckdb" - determines where the initial sample plans are sourced from for the optimization conversation; this only affects the first optimization stage and does not impact the overall conversation structure
    )
    max_num_threads: int | None = (
        None  # only relevant for the multi-threading optimization conversation; determines how many threads to use for the optimized implementation
    )
    api_base: str | None = (
        None  # API base URL for local model endpoints (e.g. http://dgx02:13505/v1)
    )
    glm_thinking: bool = False  # enable GLM-5 interleaved thinking mode
    db_storage: DBStorage = (
        DBStorage.IN_MEMORY  # whether to use persistent storage instead of in-memory DBMS
    )
    memory_budget_mb: int | None = None
    include_mem_budget_for_in_mem_in_hashes: bool = False
    target_sf: float | None = (
        None  # target scale factor for the check-sf correctness conversation
    )
    usecase: Usecase = Usecase.OLAP
    workspace_dir: str | None = (
        None  # output/workspace dir; None -> settings.get_workspace_dir() (local ./output)
    )


def add_common_args(
    parser: argparse.ArgumentParser,
    *,
    benchmark_class: type = OLAPWorkload,
    include_model: bool = False,
    include_benchmark: bool = False,
    include_replay: bool = False,
    include_disable_openai_tracing: bool = False,
    include_log_to_wandb: bool = False,
    include_wandb_entity_project: bool = False,
    include_query_list: bool = False,
    include_queries_str: bool = False,
    include_continue_run: bool = False,
    include_no_preload: bool = False,
    include_notify: bool = False,
    include_start_snapshot: bool = False,
    start_snapshot_required: bool = False,
    include_disable_repo_sync: bool = False,
    include_replay_cache: bool = False,
    include_auto_u: bool = False,
    include_auto_finish: bool = False,
    include_keep_csv: bool = False,
    include_disable_valtool: bool = False,
    include_stage: bool = False,
    include_run_tool_offer_trace_option: bool = False,
    include_bespoke_storage: bool = False,
    include_storage_plan_snapshot: bool = False,
    include_only_from_llm_cache: bool = False,
    include_only_from_cache: bool = False,
    include_do_not_cache: bool = False,
    include_tool_search_tool: bool = False,
    include_use_autonomy_master_prompt: bool = False,
    include_sdk: bool = False,
    include_optimize_sample_plan_source: bool = False,
    include_use_supervision_agent: bool = False,
    include_max_num_threads: bool = False,
    include_api_base: bool = False,
    include_glm_thinking: bool = False,
    include_db_storage: bool = False,
    include_memory_budget_mb: bool = False,
    include_include_mem_budget_for_in_mem_in_hashes: bool = False,
) -> None:
    # Always available: where the run's git-tracked output lives. Local disk by
    # default (the snapshotter does heavy git ops); avoid putting it on NFS.
    parser.add_argument(
        "--workspace",
        dest="workspace_dir",
        default=None,
        help="Output/workspace directory (default: local ./output).",
    )

    # Always available: stream DEBUG-level logs to the console. Off by default —
    # the full DEBUG log is always written to the logfile regardless.
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Stream verbose (DEBUG) logs to the console.",
    )

    # Number of parameter instantiations per query for the correctness sweep. None ->
    # the provider's default (DEFAULT_NUM_INSTANTIATIONS).
    parser.add_argument(
        "--num_instantiations",
        type=int,
        default=None,
        help="Parameter instantiations per query in the correctness sweep.",
    )

    if include_model:
        parser.add_argument(
            "--model",
            default=DEFAULT_MODEL,
            help="Model ID to use for the agent.",
        )

    if include_benchmark:

        def _benchmark_type(value):
            # built-in enum member, or any registered (bring-your-own) workload name
            try:
                return benchmark_class(value)
            except ValueError:
                from synnodb.workloads.workload_spec import resolve_workload

                return resolve_workload(value)

        parser.add_argument(
            "--benchmark",
            type=_benchmark_type,
            default=benchmark_class.TPCH,
            help="Benchmark/workload: a built-in (tpch/ceb) or any registered workload name.",
        )
    if include_replay:
        parser.add_argument(
            "--replay",
            action="store_true",
            default=False,
            help="Replay previous conversation if set.",
        )
    if include_disable_openai_tracing:
        parser.add_argument(
            "--disable_openai_tracing",
            action="store_true",
            default=False,
            help="Disable OpenAI tracing if set.",
        )
    if include_log_to_wandb:
        parser.add_argument(
            "--log_to_wandb",
            action="store_true",
            default=False,
            help="Log run metrics and traces to Weights & Biases if set.",
        )
    if include_wandb_entity_project:
        parser.add_argument(
            "--wandb_entity",
            type=str,
            default=None,
            help="W&B entity to log to (default: the user's own default entity).",
        )
        parser.add_argument(
            "--wandb_project",
            type=str,
            default=None,
            help="W&B project to log to (default: 'SynnoDB' when wandb is enabled).",
        )
    if include_queries_str:
        parser.add_argument(
            "--queries",
            help="String of the queries e.g. 1-22",
            required=True,
            dest="queries_str",
        )
    if include_query_list:
        parser.add_argument(
            "--query_list",
            help="Comma-separated list of queries.",
            required=True,
        )
    if include_continue_run:
        parser.add_argument(
            "--continue_run",
            action="store_true",
            default=False,
            help="Continue with the current snapshot in the working-dir. Does not start empty.",
        )
    if include_no_preload:
        parser.add_argument(
            "--no_preload",
            action="store_true",
            default=False,
            help="Skip validate tool preloading",
        )
    if include_notify:
        parser.add_argument(
            "--notify",
            action="store_true",
            default=False,
            help="Notify when conversation requires action",
        )
    if include_start_snapshot:
        parser.add_argument(
            "--start_snapshot",
            type=str,
            default=None,
            required=start_snapshot_required,
            help="Path to snapshot to start from (if not continuing current snapshot).",
        )
    if include_disable_repo_sync:
        parser.add_argument(
            "--disable_repo_sync",
            action="store_true",
            default=False,
            help="Disable syncing snapshots with the cache repo.",
        )
    if include_replay_cache:
        parser.add_argument(
            "--replay_cache",
            action="store_true",
            default=False,
            help="Auto press 'u' until first non-cached LLM call",
        )
    if include_auto_u:
        parser.add_argument(
            "--auto_u",
            action="store_true",
            default=False,
            help="Auto press 'u' for all prompts (skip user interaction, and auto-approve all prompts). This is dangerous and might lead to large bills / unwanted changes / ... Huge caution advised.",
        )

    if include_auto_finish:
        parser.add_argument(
            "--auto_finish",
            action="store_true",
            default=False,
            help="Automatically finish if no more prompt is found in conversation / i.e. Str-D in last iteration",
        )

    if include_keep_csv:
        parser.add_argument(
            "--keep_csv",
            action="store_true",
            default=False,
            help="Keep csv if set.",
        )

    if include_disable_valtool:
        parser.add_argument(
            "--disable_valtool",
            action="store_true",
            default=False,
            help="Disable validate tool if set",
        )

    if include_stage:
        parser.add_argument(
            "--stage",
            type=str,
            default="scripted",  # any registered stage name, or 'scripted'
            help="Stage to run (e.g. 'createBaseImpl', 'runOptimLoop', 'scripted').",
        )

    if include_run_tool_offer_trace_option:
        parser.add_argument(
            "--run_tool_offer_trace_option",
            action="store_true",
            default=False,
            help="Whether to include trace options in the run tool (and consequently offer the option to enable tracing in the conversation). This is needed for collecting execution traces for training data generation.",
        )
    if include_bespoke_storage:
        parser.add_argument(
            "--bespoke_storage",
            action="store_true",
            default=False,
            help="Mark that this run is using bespoke storage",
        )
    if include_storage_plan_snapshot:
        parser.add_argument(
            "--storage_plan_snapshot",
            type=str,
            default=None,
            help="Path to snapshot to load storage plan from (incompatible with --continue_run).",
        )

    if include_only_from_llm_cache:
        parser.add_argument(
            "--only_from_llm_cache",
            action="store_true",
            default=False,
            help="Only answer from LLM cache and do not call the LLM. Will raise an error if a cache miss occurs.",
        )
    if include_only_from_cache:
        parser.add_argument(
            "--only_from_cache",
            action="store_true",
            default=False,
            help="Only answer from cache (including both LLM cache and run tool cache) and do not call the LLM or run tool. Will raise an error if a cache miss occurs.",
        )

    if include_do_not_cache:
        parser.add_argument(
            "--do_not_cache",
            action="store_true",
            default=False,
            help="Do not store any new entries in the cache (both LLM cache and run tool cache). This is different from --only_from_cache, which allows reading from cache but not writing to cache, while --do_not_cache will still call the LLM and run tool but just not store any new entries in the cache.",
        )

    if include_tool_search_tool:
        parser.add_argument(
            "--tool_search_tool",
            action="store_true",
            default=False,
            help="Whether to include the tool search tool in the agent's toolbox. This is needed for collecting training data for the tool search tool.",
        )
    if include_use_autonomy_master_prompt:
        parser.add_argument(
            "--use_autonomy_master_prompt",
            action="store_true",
            default=False,
            help="Prefix all prompts with an autonomy master prompt.",
        )
    if include_sdk:
        parser.add_argument(
            "--sdk",
            type=str,
            default="openai",
            help="Which SDK to use for the agent. E.g. 'openai', 'anthropic', ...",
        )

    if include_optimize_sample_plan_source:
        parser.add_argument(
            "--optimize_sample_plan_source",
            type=str,
            default="duckdb",
            help="For the optimization conversation mode: where to source the initial sample plans from for the first optimization stage. Options are 'umbra' or 'duckdb'. ",
        )

    if include_use_supervision_agent:
        parser.add_argument(
            "--use_supervision_agent",
            action="store_true",
            default=False,
            help="Whether to use a supervision agent to guide the implementation agent. This became necessary after openai introduced gpt5.4 - then the agents suddenly all ask for user confirmation. The supervision agent will provide feedback to the implementation agent and only ask for user confirmation for critical decisions.",
        )

    if include_max_num_threads:
        parser.add_argument(
            "--max_num_threads",
            type=int,
            default=None,
            help="Only relevant for the multi-threading optimization conversation mode: determines how many threads to use for the optimized implementation.",
        )

    if include_api_base:
        parser.add_argument(
            "--api_base",
            type=str,
            default=None,
            help="API base URL for local model endpoints (e.g. http://dgx02:13505/v1). "
            "Defaults to http://dgx02:13505/v1 for non-cloud providers if not set.",
        )

    if include_db_storage:
        parser.add_argument(
            "--db_storage",
            type=DBStorage,
            default=DBStorage.IN_MEMORY,
            choices=list(DBStorage),
            help="Source for the database. Options are 'in_memory' or 'ssd' or 'labstore' ...",
        )

    if include_memory_budget_mb:
        parser.add_argument(
            "--memory_budget_mb",
            type=int,
            default=None,
        )

    if include_include_mem_budget_for_in_mem_in_hashes:
        parser.add_argument(
            "--include_mem_budget_for_in_mem_in_hashes",
            action="store_true",
            default=False,
            help="If set, include memory_budget_mb in the validate-cache hash even for "
            "IN_MEMORY storage. Opt-in to the legacy behavior so old caches keyed on "
            "memory_budget_mb (e.g. when reusing a prior run) can be hit. Off by default "
            "because the default budget is derived from SC_PHYS_PAGES and fluctuates across "
            "machines/reboots, which silently breaks the cache chain.",
        )

    if include_glm_thinking:
        parser.add_argument(
            "--glm_thinking",
            action="store_true",
            default=False,
            help="Enable GLM-5 interleaved thinking mode. Passes thinking:{type:enabled} "
            "to the API and replays reasoning_content across turns for coherent multi-step reasoning.",
        )
