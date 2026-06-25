import argparse
from typing import TypedDict

from conversations.conversation_spec import ConversationSpec, FrameworkContext
from cpp_runner.prepare_repo.prepare_olap import prepare_base
from main import run_conv_wrapper
from observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from utils.cli_config import RunConfig, add_common_args
from utils.confirm_dialog import await_user_confirmation
from utils.conv_name_utils import ConvMode
from utils.gen_common import parse_query_ids
from utils.utils import DBStorage
from workloads.workload_provider import Workload

### RUN CMD
# python run_gen_base_impl.py --conv initial1-22v66 --benchmark tpch --bespoke_storage --auto_u --auto_finish


def _factory(ctx: FrameworkContext):
    from conversations.base_impl_conversation import BaseImplConversation
    from utils.get_sample_q_args import get_sample_exec_settings, get_sample_query_args
    from workloads.workload_provider_olap import OLAPExecSettings

    sample_query_args_dict = get_sample_query_args(
        workload_provider=ctx.workload_provider
    )
    exec_settings = get_sample_exec_settings(workload_provider=ctx.workload_provider)
    assert isinstance(exec_settings, OLAPExecSettings)

    return BaseImplConversation(
        benchmark=ctx.args.benchmark,
        read_storage_plan=ctx.args.bespoke_storage,
        sample_query_args_dict=sample_query_args_dict,
        workspace_path=ctx.workspace_path,
        use_master_prompt=ctx.args.use_autonomy_master_prompt,
        sql_dict=ctx.workload_provider.sql_dict,
        db_storage=ctx.db_storage,
        parquet_dir=exec_settings.parquet_dir,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


SPEC = ConversationSpec(
    prepare=prepare_base,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory,
)


def validate_snapshot(
    snapshot_config,
    benchmark,
    queries_str,
    query_ids,
    db_storage: DBStorage | None,
    model: str | None,
):
    # run validation
    snapshot_benchmark: str = snapshot_config["benchmark"]
    snapshot_queries_str = snapshot_config["queries_str"]
    snapshot_model = snapshot_config["model"]
    snapshot_db_storage = snapshot_config["db_storage"]

    assert snapshot_benchmark.upper() == benchmark.name.upper(), (
        f"Expected benchmark {benchmark.name.upper()} in storage plan run, got {snapshot_benchmark}"
    )
    if queries_str is not None:
        assert snapshot_queries_str == queries_str, (
            f"Expected queries str {queries_str} in storage plan run, got {snapshot_queries_str}"
        )
    assert query_ids == parse_query_ids(snapshot_queries_str, benchmark=benchmark), (
        f"Expected query ids {query_ids} in storage plan run, got {parse_query_ids(snapshot_queries_str, benchmark=benchmark)}"
    )

    # convert snapshot db source to enum
    if db_storage is not None:
        assert snapshot_db_storage.lower() == db_storage.value.lower(), (
            f"Expected db_storage {db_storage.value.lower()} (type: {type(db_storage.value)}) in storage plan run, got {snapshot_db_storage.lower()} (type: {type(snapshot_db_storage)})"
        )

    if model is not None and snapshot_model != model:
        response = await_user_confirmation(
            f"Model in storage plan run is {snapshot_model}, but current model is {model}. Do you want to continue?"
        )
        if not response:
            print("Aborting run.")
            import sys

            sys.exit(0)


def main(args):
    # extract parameters
    bespoke_storage = args.bespoke_storage
    queries_str = args.queries_str
    benchmark = args.benchmark

    # extract queries from short name
    query_ids = parse_query_ids(queries_str, benchmark=benchmark)
    assert query_ids is not None, (
        f"Could not parse query ids from short name {query_ids}"
    )

    if bespoke_storage:
        assert args.storage_plan_run_id is not None, (
            "storage_plan_run_id must be provided when bespoke_storage is True"
        )
        storage_plan_run_id = args.storage_plan_run_id
        statistics, config, _ = wandb_retrieve_metrics_for_run(
            benchmark,
            storage_plan_run_id,
        )

        validate_snapshot(
            config,
            benchmark,
            queries_str,
            query_ids,
            db_storage=args.db_storage,
            model=args.model,
        )

        # extract git snapshot
        storage_plan_snapshot = statistics["code/snapshot_hash"]  # type: ignore
        assert storage_plan_snapshot != "N/A", (
            f"Could not retrieve a valid commit hash from wandb for run {storage_plan_snapshot} in benchmark {benchmark}. Got {storage_plan_snapshot}."
        )
    else:
        storage_plan_snapshot = None

    # CLI --memory_budget_mb overrides the default; otherwise pick a RAM budget
    # only for persistent storage runs (in-memory uses the full available RAM).
    if args.memory_budget_mb is not None:
        memory_budget_mb = args.memory_budget_mb
    elif args.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
        memory_budget_mb = 50 * 1024
    else:
        memory_budget_mb = None

    config = RunConfig(
        **base_args_extract(
            args,
        ),
        conv_mode=ConvMode.BASE,  # not scripted: instead autonomous conversation
        query_list=",".join(map(str, query_ids)),
        keep_csv=False,  # keep .csv files around instead of git-ignoring them (maybe to backtrack correctness issues)
        bespoke_storage=bespoke_storage,
        storage_plan_snapshot=storage_plan_snapshot,
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        memory_budget_mb=memory_budget_mb,
        include_mem_budget_for_in_mem_in_hashes=args.include_mem_budget_for_in_mem_in_hashes,
    )

    # run conversation
    run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "--storage_plan_run_id",
        type=str,
        default=None,
        help="Wandb run id to read the storage plan from (required if --bespoke_storage is set)",
    )

    add_common_args(
        parser,
        include_bespoke_storage=True,
        **base_args(),
    )
    return parser


def base_args() -> dict:
    # cli args to activate
    return dict(
        include_model=True,
        include_api_base=True,
        include_notify=True,
        include_disable_repo_sync=True,
        include_replay_cache=True,
        include_benchmark=True,
        include_log_to_wandb=True,
        include_disable_openai_tracing=True,
        include_auto_u=True,
        include_auto_finish=True,
        include_replay=True,
        include_do_not_cache=True,
        include_only_from_llm_cache=True,
        include_only_from_cache=True,
        include_tool_search_tool=True,
        include_queries_str=True,
        include_glm_thinking=True,
        include_db_storage=True,
        include_include_mem_budget_for_in_mem_in_hashes=True,
        include_memory_budget_mb=True,
    )


class BaseArgs(TypedDict):
    model: str
    disable_repo_sync: bool
    replay_cache: bool
    benchmark: Workload
    log_to_wandb: bool
    disable_openai_tracing: bool
    auto_u: bool
    auto_finish: bool
    replay: bool
    do_not_cache: bool
    only_from_llm_cache: bool
    only_from_cache: bool
    tool_search_tool: bool
    notify: bool
    queries_str: str
    api_base: str | None
    glm_thinking: bool
    db_storage: DBStorage


def base_args_extract(args) -> BaseArgs:
    args_dict: BaseArgs = {
        "model": args.model,
        "notify": args.notify,
        "disable_repo_sync": args.disable_repo_sync,
        "replay_cache": args.replay_cache,
        "benchmark": args.benchmark,
        "log_to_wandb": args.log_to_wandb,
        "disable_openai_tracing": args.disable_openai_tracing,
        "auto_u": args.auto_u,
        "auto_finish": args.auto_finish,
        "replay": args.replay,
        "do_not_cache": args.do_not_cache,
        "only_from_llm_cache": args.only_from_llm_cache,
        "only_from_cache": args.only_from_cache,
        "tool_search_tool": args.tool_search_tool,
        "queries_str": args.queries_str,
        "api_base": getattr(args, "api_base", None),
        "glm_thinking": getattr(args, "glm_thinking", False),
        "db_storage": args.db_storage,
    }
    # # ensure overwrite keys are valid
    # orig_keys = set(args_dict.keys())
    # assert set(overwrite.keys()).issubset(orig_keys), (
    #     f"Invalid keys in overwrite: {set(overwrite.keys()) - orig_keys}"
    # )

    # args_dict.update(overwrite)
    return args_dict


if __name__ == "__main__":
    args = build_parser().parse_args()
    main(args)
