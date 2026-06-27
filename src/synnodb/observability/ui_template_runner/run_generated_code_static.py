#!/usr/bin/env python3
"""Minimal runner for bespoke-tpch or bespoke-ceb."""

import argparse
import logging
import os
import random

# add parent to path
import sys
from pathlib import Path

import cpp_runner
from observability.logging.wandb_api_helper import wandb_retrieve_metrics_for_run
from tools.validate.query_validator_class import format_args_string
from workloads.dataset.query_gen_factory import get_query_gen

sys.path.append(str(Path(__file__).parent.parent))

from observability.benchmark.run import get_all_query_ids
from observability.logging.logger import setup_logging
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import RunTool
from utils.confirm_dialog import await_user_confirmation

BASE_PARQUET_DIR = "/mnt/labstore/bespoke_olap"

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)


def get_instantiations(benchmark: str, query_ids: list[str], repeat: int = 1):
    # prepare query generator
    gen_query_fn = get_query_gen(benchmark)

    sql_list: list[str] = []
    placeholder_list: list[dict] = []
    query_list: list[str] = []

    rnd = random.Random(42)
    for _ in range(repeat):
        for query_id in query_ids:
            template, query, placeholders = gen_query_fn(
                query_name=f"Q{query_id}", rnd=rnd
            )
            query_list.append(str(query_id))
            placeholder_list.append(placeholders)
            sql_list.append(query)

    return sql_list, placeholder_list, query_list


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and run bespoke OLAP implementation."
    )
    parser.add_argument(
        "benchmark", choices=["tpch", "ceb"], help="Which benchmark to run"
    )
    parser.add_argument("--sf", type=float, default=1, help="Scale factor (default: 1)")
    parser.add_argument(
        "--no-optimize",
        dest="optimize",
        help="Compile without optimization",
        action="store_false",
        default=True,
    )
    parser.add_argument(
        "--wandb_snapshot",
        type=str,
        required=True,
        help="Wandb run-id for the snapshot to use.",
    )
    args = parser.parse_args()

    # lookup git snapshot for provided wandb run-id
    statistics, config, hist = wandb_retrieve_metrics_for_run(
        benchmark=args.benchmark, run_id=args.wandb_snapshot, output_hist=False
    )
    git_snapshot = statistics["code/snapshot_hash"]

    # assemble source directory for bespoke implementation
    ROOT = Path(__file__).parent
    workspace_dir = ROOT / "output"

    # assemble snapshotter for code version loading
    cache_repo = "git://c01/bespoke_cache.git"
    snapshotter = GitSnapshotter(
        cache_repo=cache_repo,
        working_dir=workspace_dir,
        extra_gitignore=[],
        do_not_snapshot=True,  # only load - never write
    )

    # load the code snapshot for the bespoke implementation - this will be used for building and running the code
    is_dirty, git_status_output = snapshotter.is_dirty()
    if is_dirty:
        # ask the use how to proceed
        if await_user_confirmation(
            f"The working directory ({workspace_dir}) has uncommitted changes. Git status output:\n{git_status_output}\n\nWe will remove all uncommited changes now. Is this ok?"
        ):
            # delete uncommited changes
            # clean untracked files
            snapshotter.clear_untracked()
            # reset tracked files to last commit
            snapshotter.reset_changes()
        else:
            raise Exception(
                f'Please remove all uncommitted changes in "{workspace_dir}". We expect a clean working directory to ensure reproducibility.'
            )
    # check that snapshot exists
    assert snapshotter.has_snapshot(git_snapshot), (
        f"Snapshot {git_snapshot} not found in repo."
    )

    # load from provided snapshot
    logger.info(f"Restoring snapshot {git_snapshot}")
    snapshotter.restore(git_snapshot)

    # get path for misc.fasttest (from import)

    API_PATH = Path(os.path.dirname(cpp_runner.__file__))

    # assemble the tool for building and running the bespoke implementation
    db_engine = RunTool(
        cwd=workspace_dir,
        dataset_name=args.benchmark,
        base_parquet_dir=f"{BASE_PARQUET_DIR}/{get_dataset_name(args.benchmark)}_parquet",
        api_path=API_PATH,  # necessary for compiler
        run_stats_collector=None,
    )

    logger.info(
        "Building and running %s SF=%s optimize=%s...",
        args.benchmark,
        args.sf,
        args.optimize,
    )

    # assemble the queries to run - we only need the query-name and the placeholders, the tool will take care of the rest
    sql_list, placeholder_list, query_list = get_instantiations(
        benchmark=args.benchmark, query_ids=get_all_query_ids(args.benchmark), repeat=1
    )

    # format the arguments handed in to the tool
    args_list = format_args_string(query_list, placeholder_list)

    # run the queries - results will be written to '*.csv' files in the sourcefile directory e.g. bespoke_tpch
    result = db_engine.run_worker(
        scale_factor=args.sf,
        optimize=args.optimize,
        stdin_args_data=args_list,
    )

    logger.info(f"Finished running. Result files written to {workspace_dir}/*.csv")


if __name__ == "__main__":
    main()
