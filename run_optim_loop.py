import argparse

from conversations.conversation_spec import ConversationSpec, FrameworkContext
from cpp_runner.prepare_repo.load_snapshot_and_prepare import prepare_optim
from main import run_conv_wrapper
from observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from run_gen_base_impl import base_args, base_args_extract, validate_snapshot
from utils.cli_config import RunConfig, add_common_args
from utils.conv_name_utils import ConvMode
from utils.gen_common import parse_query_ids
from utils.utils import DBStorage

### RUN CMD
# python run_optim_loop.py --conv optim1-22v1 --bespoke_storage --benchmark tpch --auto_u --auto_finish

## for litellm it is recommended to run with disabled openai tracing (throws randomly errors sometimes) - wandb tracing collects all necessary information

# python run_optim_loop.py --model anthropic/claude-opus-4-6 --conv optim1-22v1 --benchmark tpch --bespoke_storage --disable_openai_tracing


def build_optim_conv_args(ctx: FrameworkContext):
    """Shared between the optim (this module) and make-mt conversation factories."""
    from conversations.optimization_conversation import OptimConvArgs

    assert ctx.query_validator is not None, (
        "query_validator must be provided for optimization conversations (disable_valtool is set?)"
    )
    return OptimConvArgs(
        query_ids=ctx.query_list,
        bespoke_storage=ctx.args.bespoke_storage,
        query_validator=ctx.query_validator,
        plan_source=ctx.args.optimize_sample_plan_source,
        cleanup_plans=True,
        model=ctx.args.model,
        db_storage=ctx.db_storage,
    )


def _factory(ctx: FrameworkContext):
    optim_conv_args = build_optim_conv_args(ctx)

    if ctx.db_storage == DBStorage.IN_MEMORY:
        from conversations.in_mem_1_optim_conv import InMem1OptimizationConversation

        return InMem1OptimizationConversation(
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    elif ctx.db_storage == DBStorage.SSD:
        from conversations.ssd_1_st_opt_conv import SSD1STOptimConv

        return SSD1STOptimConv(
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    else:
        raise Exception(
            f"Unsupported db_storage for optim conversation: {ctx.db_storage}"
        )


SPEC = ConversationSpec(
    prepare=prepare_optim,
    needs_parallelism=False,
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

    if args.model.startswith("anthropic/"):
        model_provider = "anthropic"
    elif args.model.startswith("gpt-"):
        model_provider = "openai"
    else:
        assert "/" in args.model, (
            f"Model name {args.model} is not in the expected format <provider>/<model_name>"
        )
        model_provider = args.model.split("/")[0]

    # lookup git snapshot from wandb
    wandb_id = args.base_impl_run_id
    assert wandb_id is not None, (
        "base_impl_run_id must be provided to fetch the git snapshot of the base implementation from wandb"
    )
    statistics, config, _ = wandb_retrieve_metrics_for_run(
        benchmark, wandb_id, fetch_latest_runtimes=False
    )
    validate_snapshot(
        config,
        benchmark,
        queries_str,
        query_ids,
        db_storage=args.db_storage,
        model=args.model,
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
        conv_mode=ConvMode.OPTIM,  # delegate the optimization loop logic to the conversation instead of hardcoding it in the main function
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,  # keep .csv files around instead of git-ignoring them (maybe to backtrack correctness issues)
        run_tool_offer_trace_option=True,  # for optimization conversations, we want to offer the option to run with tracing compile flag enabled to collect more fine-grained performance data for the optimized plans
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        optimize_sample_plan_source=args.optimize_sample_plan_source,
        bespoke_storage=bespoke_storage,
        memory_budget_mb=memory_budget_mb,
        include_mem_budget_for_in_mem_in_hashes=args.include_mem_budget_for_in_mem_in_hashes,
    )

    # run conversation
    run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "--base_impl_run_id",
        type=str,
        default=None,
        help="Wandb run id to read the base_impl from",
    )

    add_common_args(
        parser,
        include_optimize_sample_plan_source=True,
        include_bespoke_storage=True,
        **base_args(),
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    main(args)
