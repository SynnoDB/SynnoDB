import logging
from dataclasses import dataclass
from typing import Callable

from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.synth_framework.git_snapshotter import GitSnapshotter
from synnodb.tools.run import delete_result_files
from synnodb.utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)


@dataclass
class PrepareContext:
    """Inputs handed to a prepare function (``Stage.prepare``).

    A prepare function brings the workspace into the state a given conversation
    expects by invoking the relevant ``prepare_workspace_provider`` steps.
    Everything those steps need - in particular ``write_non_tracked_only``,
    which is only known once the start snapshot (if any) has been restored - is
    collected here by :func:`prepare_repo_and_load_snapshot`.
    """

    prepare_workspace_provider: PrepareWorkspace
    usecase_prepare_args: dict[str, str]
    # When True, tracked files come from the restored snapshot and only the
    # non-tracked (read-only / generated) files should be (re)written.
    write_non_tracked_only: bool
    do_not_cache: bool = True
    only_from_cache: bool = False
    add_sample_trace: bool = False
    # Only set for the dynamic CHECK_SF prepare, which replays the prepare steps
    # of the source run identified by this stage name (see
    # :func:`prepare_replay_source_run`).
    source_stage_name: str | None = None


PrepareFn = Callable[[PrepareContext], str]


def prepare_repo_and_load_snapshot(
    snapshotter: GitSnapshotter,
    snapshot: str | None,
    prepare_fn: PrepareFn,
    prepare_workspace_provider: PrepareWorkspace,
    usecase_prepare_args: dict[str, str],
    do_not_cache: bool = True,
    conv_name: str | None = None,
    only_from_cache: bool = False,
    add_sample_trace: bool = False,
    source_stage_name: str | None = None,
) -> str:
    """Bring the workspace into a known state and run the requested prepare steps.

    If `snapshot` is given, it is restored and prepare runs in "non-tracked-only" mode
    (tracked files come from the snapshot). If `snapshot` is None, an empty snapshot
    is created (named `conv_name`) and a full prepare is done from scratch.

    The mode-specific prepare steps live in ``prepare_fn`` (a
    :data:`PrepareFn`, e.g. :func:`prepare_base`), which the conversation spec
    supplies; this function only handles the snapshot/workspace bookkeeping
    shared by every mode and then hands a :class:`PrepareContext` to ``prepare_fn``.
    """
    if snapshot is None:
        assert conv_name is not None, "conv_name is required when snapshot is None"
        snapshotter.create_empty_snapshot(conv_name)
        write_non_tracked_only = False  # workspace is empty; write everything
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
        write_non_tracked_only = True  # tracked files come from the snapshot

    delete_result_files(workspace_path=snapshotter.working_dir)

    ctx = PrepareContext(
        prepare_workspace_provider=prepare_workspace_provider,
        usecase_prepare_args=usecase_prepare_args,
        write_non_tracked_only=write_non_tracked_only,
        do_not_cache=do_not_cache,
        only_from_cache=only_from_cache,
        add_sample_trace=add_sample_trace,
        source_stage_name=source_stage_name,
    )
    return prepare_fn(ctx)
