from cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext


def prepare_ff_plan(ctx: PrepareContext) -> str:
    return ctx.prepare_workspace_provider.prepare(
        add_thread_pool_to_query_impl=False,
        only_query_md=True,
        add_sample_trace=ctx.add_sample_trace,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=ctx.usecase_prepare_args,
    )
