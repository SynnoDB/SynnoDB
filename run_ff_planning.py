import argparse
import sys
from pathlib import Path

from conversations.ff.get_ff_plan_conversation import GenFFPlanConversation
from cpp_runner.prepare_repo.prepare_ff import prepare_ff_plan

# add parent to path
sys.path.append(Path(__file__).parent.as_posix())
from conversations.conversation_spec import ConversationSpec, FrameworkContext
from main import run_conv_wrapper
from run_gen_base_impl import base_args, base_args_extract
from utils.cli_config import RunConfig, Usecase, add_common_args
from utils.conv_name_utils import ConvMode
from utils.gen_common import parse_query_ids
from workloads.workload_provider_bff import BFFWorkload


def _factory(ctx: FrameworkContext):

    return GenFFPlanConversation(
        benchmark=ctx.args.benchmark,
        schema=ctx.workload_provider.dataset_schema,
        workspace_path=ctx.workspace_path,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


SPEC = ConversationSpec(
    prepare=prepare_ff_plan,
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
        usecase=Usecase.BFF,
        **base_args_extract(args),
    )

    run_conv_wrapper(args=None, run_config=config, spec=SPEC)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)

    add_common_args(
        parser,
        benchmark_class=BFFWorkload,
        **base_args(),
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    main(args)
