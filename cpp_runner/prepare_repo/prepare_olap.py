import logging

from conversations.filenames import get_filenames
from cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext, PrepareFn

logger = logging.getLogger(__name__)


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
