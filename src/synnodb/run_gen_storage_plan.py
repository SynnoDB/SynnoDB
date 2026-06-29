import argparse
import sys
from pathlib import Path

# add parent to path
sys.path.append(Path(__file__).parent.as_posix())
from synnodb.conversations.conversation_spec import ConversationSpec, FrameworkContext
from synnodb.cpp_runner.prepare_repo.prepare_olap import prepare_storage_plan
from synnodb.main import run_conv_wrapper
from synnodb.run_gen_base_impl import base_args, base_args_extract
from synnodb.utils.cli_config import RunConfig, add_common_args
from synnodb.utils.conv_name_utils import ConvMode
from synnodb.utils.gen_common import parse_query_ids


def _factory(ctx: FrameworkContext):
    from synnodb.conversations.gen_storage_plan_conversation import (
        GenStoragePlanConversation,
    )

    return GenStoragePlanConversation(
        benchmark=ctx.args.benchmark,
        schema=ctx.workload_provider.dataset_schema,
        workspace_path=ctx.workspace_path,
        db_storage=ctx.db_storage,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


SPEC = ConversationSpec(
    prepare=prepare_storage_plan,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory,
)


def main(args):
    # ===== CONFIGURATION =====
    queries_str = args.queries_str
    benchmark = args.benchmark

    # extract queries from short name
    query_ids = parse_query_ids(queries_str, benchmark=benchmark)
    assert query_ids is not None, f"Failed to parse query ids from {queries_str}"

    # =========================

    config = RunConfig(
        query_list=",".join(map(str, query_ids)),
        conv_mode=ConvMode.STORAGE_PLAN,
        bespoke_storage=True,
        **base_args_extract(args),
    )

    return run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)

    add_common_args(
        parser,
        **base_args(),
    )
    return parser


def cli():
    """Console-script entry point."""
    main(build_parser().parse_args())


if __name__ == "__main__":
    cli()
