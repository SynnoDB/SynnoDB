import logging
from dataclasses import dataclass

from synnodb.cpp_runner.prepare_repo.prepare_features import (
    PrepareFeatures,
    apply_prepare_features,
    read_prepare_metadata,
    write_prepare_metadata,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run import delete_result_files
from synnodb.utils.confirm_dialog import await_user_confirmation
from synnodb.utils.utils import DBStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrepareResult:
    """What prepare produced: the artifacts string (a cache-key input via
    ``framework_code_content``), the concrete features that were applied
    ("auto" resolved), and the parallelism recorded in the workspace metadata."""

    artifacts_str: str
    features: PrepareFeatures
    parallelism: bool


def prepare_repo_and_load_snapshot(
    snapshotter: GitSnapshotter,
    snapshot: str | None,
    features: PrepareFeatures | None,
    prepare_workspace_provider: PrepareWorkspace,
    parallelism: bool,
    do_not_cache: bool = True,
    conv_name: str | None = None,
    only_from_cache: bool = False,
) -> PrepareResult:
    """Bring the workspace into a known state and apply the requested features.

    If ``snapshot`` is given, it is restored first and its prepare record
    (``.synnodb_prepare.json``, written by every prepare) decides per feature
    whether tracked files are written (newly enabled) or only untracked
    support files are refreshed (already present). If ``snapshot`` is None, an
    empty snapshot is created (named ``conv_name``) and all features apply
    fully.

    ``features=None`` replays the restored snapshot's own record: the requested
    features and the run's parallelism are read from the workspace metadata
    (the checkSfCorrectness case). ``parallelism`` is ignored on that path.
    """
    source_features: PrepareFeatures | None = None
    if snapshot is None:
        assert conv_name is not None, "conv_name is required when snapshot is None"
        assert features is not None, (
            "replaying a workspace's recorded prepare requires a start snapshot"
        )
        snapshotter.create_empty_snapshot(conv_name)
    else:
        if not snapshotter.has_snapshot(snapshot):
            raise ValueError(f"Snapshot {snapshot} not found in repo.")

        # Avoid stale untracked files from previous snapshots (e.g. queries.md).
        is_dirty, git_status_output = snapshotter.is_dirty()
        if is_dirty:
            if await_user_confirmation(
                f"Working directory ({snapshotter.working_dir}) has uncommitted changes:\n{git_status_output}\n\nRemove them now?"
            ):
                snapshotter.reset_changes()
            else:
                raise SystemExit(f"Aborted. Clean up {snapshotter.working_dir} first.")
        snapshotter.clear_untracked(include_ignored=True)

        logger.info("Restoring snapshot %s", snapshot)
        snapshotter.restore(snapshot)

        # The restored workspace is the authoritative record of what its files
        # were prepared with; the delta against it drives tracked-vs-untracked
        # writes below.
        source_features, source_parallelism = read_prepare_metadata(
            snapshotter.working_dir
        )
        if features is None:
            # replay: reuse the source workspace's own prepare record
            features = source_features
            parallelism = source_parallelism

    delete_result_files(workspace_path=snapshotter.working_dir)

    in_memory_storage = (
        getattr(prepare_workspace_provider, "db_storage", None) == DBStorage.IN_MEMORY
    )
    features = features.resolve(in_memory_storage=in_memory_storage)

    artifacts_str = apply_prepare_features(
        features,
        prepare_workspace_provider,
        source_features,
        do_not_cache=do_not_cache,
        only_from_cache=only_from_cache,
    )

    # Written after all features applied, before the run's first snapshot
    # commit, so the record travels with every snapshot of this workspace.
    write_prepare_metadata(snapshotter.working_dir, features, parallelism=parallelism)

    return PrepareResult(
        artifacts_str=artifacts_str, features=features, parallelism=parallelism
    )
