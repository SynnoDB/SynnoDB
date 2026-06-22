import logging

from conversations.filenames import get_filenames
from cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synth_framework.git_snapshotter import GitSnapshotter
from tools.run import delete_result_csv_files
from utils.confirm_dialog import await_user_confirmation

logger = logging.getLogger(__name__)


_VALID_PREPARE_MODES = {None, "storage_plan", "base", "optim", "mt"}


def prepare_repo_and_load_snapshot(
    snapshotter: GitSnapshotter,
    snapshot: str | None,
    prepare: str,
    prepare_workspace_provider: PrepareWorkspace,
    usecase_prepare_args: dict[str, str],
    do_not_cache: bool = True,
    conv_name: str | None = None,
    only_from_cache: bool = False,
    add_sample_trace: bool = False,
) -> str:
    """Bring the workspace into a known state and run the requested prepare steps.

    If `snapshot` is given, it is restored and prepare runs in "non-tracked-only" mode
    (tracked files come from the snapshot). If `snapshot` is None, an empty snapshot
    is created (named `conv_name`) and a full prepare is done from scratch.
    """
    assert prepare in _VALID_PREPARE_MODES, f"Invalid prepare option: {prepare}"

    filenames_dict = get_filenames()
    query_impl_filename = filenames_dict["query_impl_path"]  # e.g. "query_impl.cpp"

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

    readonly_files_not_git_tracked, readonly_files_git_tracked = (
        prepare_workspace_provider._get_readonly_files()
    )
    artifacts_in_context = ""

    if prepare in ["storage_plan", "base", "optim", "mt"]:
        # gen_placeholders_fn = get_placeholders_fn(
        #     benchmark,
        #     do_not_cache=do_not_cache,
        #     cache_dir=cache_path / "placeholders_cache"
        #     if cache_path is not None
        #     else None,
        # )
        # artifacts_in_context += prepare_repo(
        #     workspace_dir=snapshotter.working_dir,
        #     benchmark=benchmark,
        #     storage_plan=storage_plan,
        #     query_list=query_list,
        #     sql_dict=get_sql_dict(benchmark),
        #     gen_placeholders_fn=gen_placeholders_fn,
        #     git_snapshotter=snapshotter,
        #     cache_dir=cache_path / "repo_prepare_cache"
        #     if cache_path is not None
        #     else None,
        #     do_not_cache=do_not_cache,
        #     write_non_tracked_only=write_non_tracked_only,
        #     readonly_files_not_git_tracked=readonly_files_not_git_tracked,
        #     add_thread_pool_to_query_impl=prepare == "mt",
        #     only_query_txt=prepare == "storage_plan",
        #     db_storage=db_storage,
        #     only_from_cache=only_from_cache,
        #     add_sample_trace=add_sample_trace,
        # )

        artifacts_in_context += prepare_workspace_provider.prepare(
            add_thread_pool_to_query_impl=prepare == "mt",
            only_query_md=prepare == "storage_plan",
            add_sample_trace=add_sample_trace,
            write_non_tracked_only=write_non_tracked_only,
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
            usecase_args=usecase_prepare_args,
        )

    if prepare in ["optim", "mt"]:
        logger.info(
            "Preparing workspace for optimization by adding tracing/flushing to %s and adding trace.hpp.",
            query_impl_filename,
        )
        # For "optim" we are upgrading a base-impl snapshot, so modify the tracked
        # query_impl.cpp. For "mt" the snapshot already has trace applied; only the
        # ro/untracked files need to be (re)written.
        # artifacts_in_context += prepare_repo_for_optim(
        #     workspace_dir=snapshotter.working_dir,
        #     query_impl_filename=query_impl_filename,
        #     git_snapshotter=snapshotter,
        #     cache_dir=cache_path / "repo_prepare_optim_cache"
        #     if cache_path is not None
        #     else None,
        #     do_not_cache=do_not_cache,
        #     readonly_files_not_git_tracked=readonly_files_not_git_tracked,
        #     write_non_tracked_only=prepare
        #     != "optim",  # when loading from snapshot in make_mt mode, only write non git-tracked files as the tracked files are already included in the snapshot and might be at a different version than the current ones
        #     only_from_cache=only_from_cache,
        # )

        artifacts_in_context += prepare_workspace_provider.prepare_optim(
            write_non_tracked_only=prepare != "optim",  # see above
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

    if prepare in ["mt"]:
        logger.info(
            "Preparing workspace for make_mt by adding thread pool helpers and flushing to %s and adding thread_pool.hpp.",
            query_impl_filename,
        )
        # artifacts_in_context += prepare_repo_for_mt(
        #     workspace_dir=snapshotter.working_dir,
        #     git_snapshotter=snapshotter,
        #     cache_dir=cache_path / "repo_prepare_mt_cache"
        #     if cache_path is not None
        #     else None,
        #     do_not_cache=do_not_cache,
        #     only_from_cache=only_from_cache,
        # )

        artifacts_in_context += prepare_workspace_provider.prepare_mt(
            only_from_cache=only_from_cache,
            do_not_cache=do_not_cache,
        )

    return artifacts_in_context
