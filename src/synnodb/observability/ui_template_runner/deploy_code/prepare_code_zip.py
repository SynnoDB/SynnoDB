#!/usr/bin/env python3
"""Checkout bespoke code snapshot for a given wandb run-id."""

import argparse
import json
import logging
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import (
    prepare_repo_and_load_snapshot,
)
from synnodb.cpp_runner.prepare_repo.prepare_features import Parallelism
from synnodb.cpp_runner.prepare_repo.prepare_workspace_olap import OLAPPrepareWorkspace
from synnodb.observability.logging.logger import setup_logging
from synnodb.observability.logging.wandb_api_helper import (
    wandb_retrieve_metrics_for_run,
)
from synnodb.observability.plots.utils.wandb_trace_preprocessor import SECTION_RULES
from synnodb.synth_framework.git_snapshotter import GitSnapshotter

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
CACHE_REPO = "git://c01/bespoke_cache.git"
ARTIFACTS_DIR = Path("/mnt/labstore/bespoke_olap/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Checkout bespoke code snapshot for a wandb run-id."
    )
    parser.add_argument("benchmark", choices=["tpch", "ceb"], help="Which benchmark")
    parser.add_argument("wandb_id", help="Wandb run-id to check out")
    parser.add_argument(
        "--snapshot_hash",
        help="Override the auto-resolved snapshot hash (must belong to the run)",
    )
    args = parser.parse_args()

    statistics, config, hist = wandb_retrieve_metrics_for_run(
        benchmark=args.benchmark, run_id=args.wandb_id, output_hist=True
    )

    assert "db_storage" in config, (
        "db_storage must be specified in the config of the wandb run"
    )
    db_storage = config["db_storage"]

    assert isinstance(hist, pd.DataFrame)

    snapshot_col = "code/snapshot_hash"
    run_hashes: set[str] = (
        set(hist[snapshot_col].dropna().tolist())
        if snapshot_col in hist.columns
        else set()
    )

    if args.snapshot_hash:
        if args.snapshot_hash not in run_hashes:
            raise SystemExit(
                f"Snapshot hash {args.snapshot_hash!r} does not appear in wandb history for run {args.wandb_id}. "
                f"Known hashes: {sorted(run_hashes)}"
            )
        git_snapshot = args.snapshot_hash
        matching_turns = hist[hist[snapshot_col] == git_snapshot].index.tolist()
        turn_number = int(matching_turns[-1]) if matching_turns else None
        logger.info(
            "Using provided snapshot hash: %s (turn %s)", git_snapshot, turn_number
        )
    else:
        # extract hash from last turn in the optim with expert knowledge phase (the following optim with human baseline uses sometimes cpu features not supported on our public machine)
        optim_human_rule = next(r for r in SECTION_RULES if r.label == "optim human")
        col = "current_prompt"
        if col in hist.columns:
            human_mask = hist[col].apply(
                lambda s: optim_human_rule.predicate(s) if isinstance(s, str) else False
            )
            human_start = human_mask[human_mask].index[0] if human_mask.any() else None
        else:
            human_start = None

        if human_start is not None and snapshot_col in hist.columns:
            pre_human = hist.loc[: human_start - 1, snapshot_col].dropna()
            if not pre_human.empty:
                turn_number = int(pre_human.index[-1])
                git_snapshot = pre_human.iloc[-1]
            else:
                turn_number = int(hist.index[-1])
                git_snapshot = statistics["code/snapshot_hash"]
            logger.info(
                "Resolved snapshot hash from last optim-expert turn (before optim-human at index %d)",
                human_start,
            )
        else:
            git_snapshot = statistics["code/snapshot_hash"]
            last_valid = (
                hist[snapshot_col].dropna() if snapshot_col in hist.columns else None
            )
            turn_number = (
                int(last_valid.index[-1])
                if last_valid is not None and not last_valid.empty
                else int(hist.index[-1])
            )
            logger.info(
                "optim-human section not found; falling back to final snapshot hash"
            )
        logger.info("Resolved snapshot hash: %s (turn %d)", git_snapshot, turn_number)

    snapshotter = GitSnapshotter(
        cache_repo=CACHE_REPO,
        working_dir=OUTPUT_DIR,
        extra_gitignore=[],
        do_not_snapshot=True,
    )

    # Restore the snapshot and regenerate its untracked framework helper files
    # by replaying the prepare record committed with the snapshot itself.
    from synnodb import settings
    from synnodb.utils.utils import DBStorage
    from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider
    from synnodb.workloads.workload_spec import get_workload_spec

    db_storage_enum = DBStorage(db_storage)
    workload_spec = get_workload_spec(args.benchmark)
    parquet_dir = (
        settings.get_data_dir()
        / "workloads"
        / args.benchmark
        / f"{workload_spec.dataset_name}_parquet"
    )
    workload_provider = OLAPWorkloadProvider(
        benchmark=args.benchmark,
        base_parquet_dir=parquet_dir,
        db_storage=db_storage_enum,
    )
    prepare_ws = OLAPPrepareWorkspace(
        db_storage=db_storage_enum,
        workload_provider=workload_provider,
        workspace_dir=OUTPUT_DIR,
        git_snapshotter=snapshotter,
    )
    prepare_repo_and_load_snapshot(
        snapshotter=snapshotter,
        snapshot=git_snapshot,
        features=None,  # replay the snapshot's own prepare record
        prepare_workspace_provider=prepare_ws,
        parallelism=Parallelism.SINGLE_THREADED,  # ignored on the replay path
    )

    zip_name = args.wandb_id or git_snapshot
    zip_path = OUTPUT_DIR.parent / f"{zip_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in OUTPUT_DIR.rglob("*"):
            if ".git" not in file.parts and file.is_file():
                zf.write(file, file.relative_to(OUTPUT_DIR))
    logger.info("Code zipped to %s", zip_path)

    metadata = {
        "wandb_run": args.wandb_id,
        "turn": turn_number,
        "git_snapshot_hash": git_snapshot,
        "model": config.get("model", "unknown") if config else "unknown",  # type: ignore[union-attr]
    }
    metadata_path = OUTPUT_DIR.parent / "code_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))
    logger.info("Metadata written to %s", metadata_path)


if __name__ == "__main__":
    main()
