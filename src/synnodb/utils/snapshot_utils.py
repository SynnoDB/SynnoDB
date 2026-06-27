import logging
from pathlib import Path

from synnodb.synth_framework.git_snapshotter import GitSnapshotter

logger = logging.getLogger(__name__)


def load_storage_plan_from_snapshot(
    args, snapshotter: GitSnapshotter, workspace_path: Path, plan_filename: str
):
    assert not args.continue_run, (
        "storage_plan_snapshot and continue_current_snapshot not compatible"
    )

    # check that snapshot exists
    assert snapshotter.has_snapshot(args.storage_plan_snapshot), (
        f"Snapshot {args.storage_plan_snapshot} not found in repo."
    )

    # load from provided snapshot
    logger.info(f"Restoring snapshot {args.storage_plan_snapshot}")
    snapshotter.restore(args.storage_plan_snapshot)

    # read storage plan
    storage_plan_path = workspace_path / plan_filename

    assert storage_plan_path.exists(), (
        f"{plan_filename} not found in snapshot {args.storage_plan_snapshot}"
    )

    # read storage plan from file
    storage_plan = storage_plan_path.read_text()

    return storage_plan
