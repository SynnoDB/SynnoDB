import logging
from dataclasses import dataclass
from typing import Callable

from cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import delete_result_csv_files
from utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)


@dataclass
class PrepareContext:
    """Inputs handed to a prepare function (``ConversationSpec.prepare``).

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
    # of the source run identified by this mode string (see
    # :func:`prepare_replay_source_run`).
    source_run_prepare_mode: str | None = None


PrepareFn = Callable[[PrepareContext], str]


def prepare_storage_plan(ctx: PrepareContext) -> str:
    return ctx.prepare_workspace_provider.prepare(
        add_thread_pool_to_query_impl=False,
        only_query_md=True,
        add_sample_trace=ctx.add_sample_trace,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=ctx.usecase_prepare_args,
    )


def prepare_base(ctx: PrepareContext) -> str:
    return ctx.prepare_workspace_provider.prepare(
        add_thread_pool_to_query_impl=False,
        only_query_md=False,
        add_sample_trace=ctx.add_sample_trace,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=ctx.usecase_prepare_args,
    )


def prepare_optim(ctx: PrepareContext) -> str:
    artifacts = prepare_base(ctx)

    query_impl_filename = get_filenames()["query_impl_path"]
    logger.info(
        "Preparing workspace for optimization by adding tracing/flushing to %s and adding trace.hpp.",
        query_impl_filename,
    )
    # We are upgrading a base-impl snapshot, so modify the tracked query_impl.cpp.
    artifacts += ctx.prepare_workspace_provider.prepare_optim(
        write_non_tracked_only=False,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
    )
    return artifacts


def prepare_mt(ctx: PrepareContext) -> str:
    query_impl_filename = get_filenames()["query_impl_path"]

    artifacts = ctx.prepare_workspace_provider.prepare(
        add_thread_pool_to_query_impl=True,
        only_query_md=False,
        add_sample_trace=ctx.add_sample_trace,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=ctx.usecase_prepare_args,
    )

    logger.info(
        "Preparing workspace for optimization by adding tracing/flushing to %s and adding trace.hpp.",
        query_impl_filename,
    )
    # The snapshot already has trace applied; only the ro/untracked files need to
    # be (re)written, so the tracked query_impl.cpp is left as it is in the snapshot.
    artifacts += ctx.prepare_workspace_provider.prepare_optim(
        write_non_tracked_only=True,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
    )

    logger.info(
        "Preparing workspace for make_mt by adding thread pool helpers and flushing to %s and adding thread_pool.hpp.",
        query_impl_filename,
    )
    artifacts += ctx.prepare_workspace_provider.prepare_mt(
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
    )
    return artifacts


_PREPARE_FNS: dict[str, PrepareFn] = {
    "storage_plan": prepare_storage_plan,
    "base": prepare_base,
    "optim": prepare_optim,
    "mt": prepare_mt,
}


def get_prepare_fn(prepare_mode: str) -> PrepareFn:
    """Resolve a prepare function from a legacy prepare-mode string.

    Used by the dynamic CHECK_SF prepare, which only learns the source run's
    prepare mode at runtime.
    """
    if prepare_mode not in _PREPARE_FNS:
        raise ValueError(
            f"Invalid prepare mode: {prepare_mode!r}. Known modes: {sorted(_PREPARE_FNS)}"
        )
    return _PREPARE_FNS[prepare_mode]


def prepare_replay_source_run(ctx: PrepareContext) -> str:
    """Dynamic prepare for CHECK_SF: replay the prepare steps of the source run.

    The source run's prepare mode is not known until runtime; it is supplied via
    ``ctx.source_run_prepare_mode`` (originating from ``args.prepare_mode``).
    """
    assert ctx.source_run_prepare_mode is not None, (
        "CHECK_SF prepare requires source_run_prepare_mode to be set"
    )
    return get_prepare_fn(ctx.source_run_prepare_mode)(ctx)


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
    source_run_prepare_mode: str | None = None,
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

    delete_result_csv_files(workspace_path=snapshotter.working_dir)

    ctx = PrepareContext(
        prepare_workspace_provider=prepare_workspace_provider,
        usecase_prepare_args=usecase_prepare_args,
        write_non_tracked_only=write_non_tracked_only,
        do_not_cache=do_not_cache,
        only_from_cache=only_from_cache,
        add_sample_trace=add_sample_trace,
        source_run_prepare_mode=source_run_prepare_mode,
    )
    return prepare_fn(ctx)
