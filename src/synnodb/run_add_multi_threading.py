import argparse

from synnodb.conversations.conversation_spec import ConversationSpec, FrameworkContext
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import prepare_mt
from synnodb.main import run_conv_wrapper
from synnodb.observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from synnodb.run_gen_base_impl import base_args, base_args_extract, validate_snapshot
from synnodb.run_optim_loop import build_optim_conv_args
from synnodb.utils.cli_config import RunConfig, add_common_args
from synnodb.utils.conv_name_utils import ConvMode
from synnodb.utils.gen_common import parse_query_ids
from synnodb.utils.utils import DBStorage

### RUN CMD
# python run_add_multi_threading.py --optim_run_id <wandb-id> --bespoke_storage --benchmark tpch \
#   --notify --replay_cache --auto_u --auto_finish

# OPTIM LOOP 2:
# - Multi-Threading


def _factory(ctx: FrameworkContext):
    optim_conv_args = build_optim_conv_args(ctx)

    if ctx.db_storage == DBStorage.IN_MEMORY:
        from synnodb.conversations.in_mem_2_mt_conv import InMem2MTConversation

        return InMem2MTConversation(
            benchmark=ctx.args.benchmark,
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    elif ctx.db_storage == DBStorage.SSD:
        from synnodb.conversations.ssd_2_mt_conv import SSD2MTOptConv

        return SSD2MTOptConv(
            benchmark=ctx.args.benchmark,
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    else:
        raise Exception(
            f"Unsupported db_storage for make_mt conversation: {ctx.db_storage}"
        )


SPEC = ConversationSpec(
    prepare=prepare_mt,
    needs_parallelism=True,
    be_relaxed_supervision=True,
    factory=_factory,
)


def main(args):
    # extract parameters
    bespoke_storage = args.bespoke_storage
    queries_str = args.queries_str
    benchmark = args.benchmark

    # extract queries from short name

    query_ids = parse_query_ids(queries_str, benchmark=benchmark)
    assert query_ids is not None, (
        f"Could not parse query ids from queries str {queries_str}"
    )

    # lookup git snapshot from wandb
    wandb_id = args.optim_run_id
    assert wandb_id is not None, (
        "optim_run_id must be provided to fetch the git snapshot of the optimized "
        "implementation from wandb"
    )
    statistics, config, _ = wandb_retrieve_metrics_for_run(
        benchmark, wandb_id, fetch_latest_runtimes=False
    )
    validate_snapshot(
        config,
        benchmark,
        queries_str,
        query_ids,
        model=args.model,
        db_storage=args.db_storage,
    )

    commit_hash = statistics["code/snapshot_hash"]
    assert commit_hash != "N/A", (
        f"Could not retrieve a valid commit hash from wandb for run {wandb_id} in benchmark {benchmark}. Got {commit_hash}."
    )

    # CLI --memory_budget_mb overrides the default; otherwise pick a RAM budget
    # only for persistent storage runs (in-memory uses the full available RAM).
    if args.memory_budget_mb is not None:
        memory_budget_mb = args.memory_budget_mb
    elif args.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]:
        memory_budget_mb = 50 * 1024
    else:
        memory_budget_mb = None

    config = RunConfig(
        **base_args_extract(args),
        conv_mode=ConvMode.MAKE_MT,  # delegate the optimization loop logic to the conversation instead of hardcoding it in the main function
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,  # keep .csv files around instead of git-ignoring them (maybe to backtrack correctness issues)
        bespoke_storage=bespoke_storage,
        run_tool_offer_trace_option=True,  # for optimization conversations, we want to offer the option to run with tracing compile flag enabled to collect more fine-grained performance data for the optimized plans
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        max_num_threads=20,
        memory_budget_mb=memory_budget_mb,
        include_mem_budget_for_in_mem_in_hashes=args.include_mem_budget_for_in_mem_in_hashes,
    )

    # run conversation
    return run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "--optim_run_id",
        type=str,
        default=None,
        help="Wandb run id to read the optimization results from",
    )

    add_common_args(
        parser,
        include_bespoke_storage=True,
        **base_args(),
    )
    return parser


def cli() -> None:
    """Console-script entry point."""
    main(build_parser().parse_args())


if __name__ == "__main__":
    cli()
