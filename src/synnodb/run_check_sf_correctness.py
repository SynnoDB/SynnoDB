import argparse

from synnodb.conversations.conversation_spec import ConversationSpec, FrameworkContext
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import prepare_replay_source_run
from synnodb.main import run_conv_wrapper
from synnodb.observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from synnodb.run_gen_base_impl import base_args, base_args_extract, validate_snapshot
from synnodb.utils.cli_config import RunConfig, add_common_args
from synnodb.utils.conv_name_utils import ConvMode
from synnodb.utils.gen_common import parse_query_ids
from synnodb.utils.utils import DBStorage

### RUN CMD
# python run_check_sf_correctness.py --source_run_id <wandb-id> --target_sf 100 \
#   --queries 1-22 --benchmark tpch --bespoke_storage --auto_u --auto_finish


def _factory(ctx: FrameworkContext):
    from synnodb.conversations.check_sf_correctness_conv import CheckSFCorrectnessConv

    assert ctx.args.target_sf is not None, (
        "target_sf must be provided for check-sf correctness conversation"
    )
    return CheckSFCorrectnessConv(
        query_ids=ctx.query_list,
        target_sf=ctx.args.target_sf,
        bespoke_storage=ctx.args.bespoke_storage,
        db_storage=ctx.db_storage,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


# Dynamic prepare: the source run's conv_mode is passed via RunConfig.prepare_mode
# below and reaches prepare_replay_source_run as source_run_prepare_mode (which it
# uses to replay the source run's prepare steps); main.py resolves parallelism from it too.
SPEC = ConversationSpec(
    prepare=prepare_replay_source_run,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory,
)


def main(args):
    bespoke_storage = args.bespoke_storage
    queries_str = args.queries_str
    benchmark = args.benchmark
    target_sf = args.target_sf

    query_ids = parse_query_ids(queries_str, benchmark=benchmark)
    assert query_ids is not None, (
        f"Could not parse query ids from queries str {queries_str}"
    )

    wandb_id = args.source_run_id
    assert wandb_id is not None, (
        "source_run_id must be provided to fetch the git snapshot of the "
        "implementation to validate from wandb"
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

    # convert target_sf to int if it's a whole number for better prompt formatting (e.g. 100.0 -> 100)
    if target_sf.is_integer():
        target_sf = int(target_sf)

    config = RunConfig(
        **base_args_extract(args),
        conv_mode=ConvMode.CHECK_SF,
        prepare_mode=config["conv_mode"],
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,
        bespoke_storage=bespoke_storage,
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        target_sf=target_sf,
        memory_budget_mb=50 * 1024
        if args.db_storage in [DBStorage.LABSTORE, DBStorage.SSD]
        else None,
    )

    return run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument(
        "--source_run_id",
        type=str,
        default=None,
        help="Wandb run id to read the implementation snapshot from",
    )
    parser.add_argument(
        "--target_sf",
        type=float,
        required=True,
        help="Target scale factor at which to verify correctness",
    )

    add_common_args(
        parser,
        include_bespoke_storage=True,
        **base_args(),
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    main(args)
