import logging
from dataclasses import dataclass

from synnodb.cpp_runner.prepare_repo.prepare_features import (
    Parallelism,
    PrepareFeatures,
    assemble_prepare_features,
    prepare_metadata_content,
    read_prepare_metadata,
    write_prepare_metadata,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import (
    PrepareCacheType,
    PrepareWorkspace,
)
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run import delete_result_files
from synnodb.utils import utils
from synnodb.utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrepareResult:
    """What prepare produced: the artifacts string (a cache-key input via
    ``framework_code_content``), the concrete features that were applied
    ("auto" resolved), and the parallelism recorded in the workspace metadata."""

    artifacts_str: str
    features: PrepareFeatures
    parallelism: Parallelism


def prepare_repo_and_load_snapshot(
    snapshotter: GitSnapshotter,
    snapshot: str | None,
    features: PrepareFeatures | None,
    prepare_workspace_provider: PrepareWorkspace,
    parallelism: Parallelism,
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

    # Resolve the "auto" values against the run's storage backend. On the
    # replay path the record's concrete storage must match the backend;
    # resolve() raises otherwise.
    features = features.resolve(prepare_workspace_provider.db_storage)

    # The snapshot the prepare starts from. Assembled files are not written
    # until after the prepared-snapshot cache lookup, so HEAD stays at this
    # base and a cache miss becomes a single commit on top.
    base_snapshot = snapshotter.current_hash

    artifacts_str, prepared_parts = assemble_prepare_features(
        features,
        prepare_workspace_provider,
        source_features,
    )

    # Commit the prepared workspace as one content-addressed snapshot carrying
    # both the prepared files and the prepare record (.synnodb_prepare.json).
    # The prepare cache mirrors the legacy implementation: a stable payload is
    # hashed to a .pkl file, and that file stores the prepared snapshot commit.
    # Git-excluded read-only support files are not part of the cache key because
    # they are refreshed on every prepare and cannot be restored from git.
    metadata_content = prepare_metadata_content(features, parallelism)
    tracked_artifacts_str = "".join(
        part.tracked_artifacts_str for part in prepared_parts
    )
    hash_payload = utils.stable_json(
        {
            "snapshotter_hash": base_snapshot,
            "files_id_str": tracked_artifacts_str,
            "metadata_content": metadata_content,
        }
    )
    cache_hash = utils.sha256(hash_payload)
    cache_path = (
        prepare_workspace_provider.prepare_cache_dir / f"{cache_hash}.pkl"
        if prepare_workspace_provider.prepare_cache_dir is not None
        else None
    )
    if prepare_workspace_provider.prepare_cache_dir is not None:
        utils.create_dir_and_set_permissions(
            prepare_workspace_provider.prepare_cache_dir
        )

    if cache_path is not None and cache_path.exists():
        cached = utils.load_pickle(cache_path, PrepareCacheType)
        assert cached is not None
        # An identical prepared state is already committed: reload it. Clear
        # untracked first so checkout is not blocked by stale files.
        logger.info("Restoring prepared repo from cache: %s", cache_path.name)
        snapshotter.clear_untracked()
        snapshotter.restore(cached.snapshot_hash)
        # The prepared snapshot restores git-tracked files. Read-only
        # support files excluded from git must still be refreshed.
        for part in prepared_parts:
            prepare_workspace_provider.write_prepared_files(part, write_tracked=False)
    elif only_from_cache:
        raise ValueError(
            "Prepared workspace not found in cache and only_from_cache is "
            f"enabled. Cache path: {cache_path}\nPayload: {hash_payload}"
        )
    else:
        for part in prepared_parts:
            prepare_workspace_provider.write_prepared_files(part)
        write_prepare_metadata(snapshotter.working_dir, features, parallelism)
        if not do_not_cache:
            assert not snapshotter.do_not_snapshot, (
                "prepare_repo_and_load_snapshot was asked to cache, but "
                "snapshotting is disabled on the GitSnapshotter"
            )
            assert base_snapshot is not None, (
                "prepared workspace has no base snapshot to commit the record onto"
            )
            _, commit = snapshotter.snapshot(cache_hash)
            assert commit is not None, "Failed to create git snapshot for prepare_repo"
            if cache_path is not None:
                utils.dump_pickle(
                    cache_path,
                    PrepareCacheType(
                        hash_payload=hash_payload,
                        snapshot_hash=commit,
                    ),
                    do_not_cache=False,
                )

    return PrepareResult(
        artifacts_str=artifacts_str, features=features, parallelism=parallelism
    )
