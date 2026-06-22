import logging
import os
from pathlib import Path
from typing import Optional

from observability.plots.utils.wandb_utils import (
    get_wandb_latest_query_runtimes,
    get_wandb_max_scale_factor,
    get_wandb_run,
    get_wandb_snapshot_hash,
    get_wandb_stats,
)
from workloads.workload_provider import Workload

logger = logging.getLogger(__name__)


def wandb_retrieve_metrics_for_run(
    benchmark: Workload,
    run_id: str,
    entity: str | None = None,
    project: str | None = None,
    output_hist: bool = False,
    fetch_latest_runtimes: bool = False,
    wandb_run_cache_path: Optional[Path] = None,
) -> tuple[dict, dict, object | None]:
    run = None

    if entity is None or project is None:
        # lookup from .evn file
        entity = os.getenv("WANDB_ENTITY", "learneddb")
        project = os.getenv("WANDB_PROJECT", "SynnoDB")

    summary, history, config = get_wandb_stats(
        run_id=run_id,
        entity=entity,
        project=project,
        wandb_run_cache_path=wandb_run_cache_path,
    )

    assert summary is not None, f"Could not retrieve summary for run {run_id}"
    run_name = summary.get("_run_name")
    if run_name is None:
        run = get_wandb_run(run_id, entity, project)
        run_name = run.name
    assert isinstance(run_name, str), (
        f"Expected run name to be a string, got {type(run_name)}"
    )
    assert benchmark.value in run_name, (
        f"Expected benchmark name in run name, got {run_name}"
    )

    last_commit_hash = get_wandb_snapshot_hash(summary)
    assert last_commit_hash != "N/A", (
        f"Could not retrieve a valid commit hash from wandb for run {run_id} in benchmark "
        f"{benchmark}. Got {last_commit_hash}.\n{list(summary.keys())}"
    )

    runtimes_df = None
    max_scale_factor = None
    if fetch_latest_runtimes:
        max_scale_factor = get_wandb_max_scale_factor(history)
        assert max_scale_factor is not None, (
            f"No scale factor data in history for run {run_id}"
        )
        logger.info(f"Fetching latest runtimes for scale factor {max_scale_factor}...")

        if run is None:
            run = get_wandb_run(run_id, entity, project)
        runtimes_df = get_wandb_latest_query_runtimes(run, max_scale_factor)

    out = {
        "code/snapshot_hash": last_commit_hash,
        "scalefactor": max_scale_factor,
        "query_runtimes": runtimes_df,
    }
    assert config is not None, f"Could not retrieve config for run {run_id}"

    if output_hist:
        return out, config, history
    return out, config, None
