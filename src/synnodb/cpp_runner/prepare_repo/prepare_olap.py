import logging

from synnodb.conversations.filenames import get_filenames
from synnodb.cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext
from synnodb.utils.utils import DBStorage

logger = logging.getLogger(__name__)


def prepare_storage_plan(ctx: PrepareContext) -> str:

    usecase_args = {
        **ctx.usecase_prepare_args,
        "add_thread_pool_to_query_impl": False,
        "add_sample_trace": ctx.add_sample_trace,
    }

    return ctx.prepare_workspace_provider.prepare(
        only_query_md=True,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=usecase_args,
    )


def prepare_base(ctx: PrepareContext) -> str:
    parallel_ready_in_memory = (
        getattr(ctx.prepare_workspace_provider, "db_storage", None)
        == DBStorage.IN_MEMORY
    )
    usecase_args = {
        **ctx.usecase_prepare_args,
        # In-memory base query implementations are written in a parallel-ready
        # shape. Non-parallel runs still execute through CORE_IDS=1, so the pool
        # takes its serial fast path during base validation.
        "add_thread_pool_to_query_impl": parallel_ready_in_memory,
        "add_sample_trace": ctx.add_sample_trace,
    }
    return ctx.prepare_workspace_provider.prepare(
        only_query_md=False,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=usecase_args,
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

    usecase_args = {
        **ctx.usecase_prepare_args,
        "add_thread_pool_to_query_impl": True,
        "add_sample_trace": ctx.add_sample_trace,
    }

    artifacts = ctx.prepare_workspace_provider.prepare(
        only_query_md=False,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=usecase_args,
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


def prepare_replay_source_run(ctx: PrepareContext) -> str:
    """Dynamic prepare for CHECK_SF: replay the prepare steps of the source run.

    The source run's stage is not known until runtime; its name is supplied via
    ``ctx.source_stage_name`` and resolved against the stage registry to reuse
    that stage's own ``prepare`` step.
    """
    from synnodb.api import get_stage

    assert ctx.source_stage_name is not None, (
        "CHECK_SF prepare requires source_stage_name to be set"
    )
    return get_stage(ctx.source_stage_name).prepare(ctx)
