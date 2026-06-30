import argparse

from synnodb.conversations.conversation_spec import ConversationSpec, FrameworkContext
from synnodb.cpp_runner.prepare_repo.prepare_olap import prepare_replay_source_run
from synnodb.main import run_conv_wrapper
from synnodb.run_gen_base_impl import (
    base_args,
    base_args_extract,
    resolve_source_snapshot,
)
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

    # The source implementation reaches us either as a git snapshot hash directly
    # (W&B-free) or via a W&B run id we resolve to that snapshot hash.
    snapshot = getattr(args, "source_snapshot", None)
    commit_hash, source_config = resolve_source_snapshot(
        snapshot=snapshot,
        wandb_id=args.source_run_id,
        source_kind="implementation to validate",
        snapshot_flag="--source_snapshot",
        wandb_flag="--source_run_id",
        benchmark=benchmark,
        queries_str=queries_str,
        query_ids=query_ids,
        db_storage=args.db_storage,
        model=args.model,
        wandb_entity=getattr(args, "wandb_entity", None),
        wandb_project=getattr(args, "wandb_project", None),
    )

    # CHECK_SF replays the source run's prepare steps, so it needs that run's
    # conv_mode. The W&B path reads it from the source run config; the W&B-free
    # path must be told it explicitly via --source_prepare_mode (the API derives
    # it from the source artifact's type).
    if source_config is not None:
        prepare_mode = source_config["conv_mode"]
    else:
        prepare_mode = getattr(args, "source_prepare_mode", None)
        assert prepare_mode is not None, (
            "--source_prepare_mode is required with --source_snapshot (the source "
            "run's conv_mode, e.g. 'base', 'optim', or 'mt')."
        )

    # convert target_sf to int if it's a whole number for better prompt formatting (e.g. 100.0 -> 100)
    if target_sf.is_integer():
        target_sf = int(target_sf)

    config = RunConfig(
        **base_args_extract(args),
        conv_mode=ConvMode.CHECK_SF,
        prepare_mode=prepare_mode,
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
        help="Wandb run id to read the implementation snapshot from. Provide "
        "exactly one of this or --source_snapshot.",
    )
    parser.add_argument(
        "--source_snapshot",
        type=str,
        default=None,
        help="Git snapshot hash of the implementation to validate, supplied "
        "directly (W&B-free). Requires --source_prepare_mode. Provide exactly one "
        "of this or --source_run_id.",
    )
    parser.add_argument(
        "--source_prepare_mode",
        type=str,
        default=None,
        help="conv_mode of the source run (e.g. 'base', 'optim', 'mt'); required "
        "with --source_snapshot to replay its prepare steps.",
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


def cli() -> None:
    """Console-script entry point."""
    main(build_parser().parse_args())


if __name__ == "__main__":
    cli()
