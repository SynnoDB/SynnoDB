import argparse
import sys
from pathlib import Path

# add parent to path
sys.path.append(Path(__file__).parent.as_posix())
from main import run_conv_wrapper
from run_gen_base_impl import base_args, base_args_extract
from utils.cli_config import RunConfig, add_common_args
from utils.gen_common import parse_query_ids


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
        conv_mode="storageplan",
        bespoke_storage=True,
        **base_args_extract(args),
    )

    run_conv_wrapper(args=None, run_config=config)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)

    add_common_args(
        parser,
        **base_args(),
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    main(args)
