from cpp_runner.prepare_repo.load_snapshot_and_prepare import PrepareContext


def prepare_ff_plan(ctx: PrepareContext) -> str:
    usecase_args = {
        **ctx.usecase_prepare_args,
    }

    return ctx.prepare_workspace_provider.prepare(
        only_query_md=True,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=usecase_args,
    )


def prepare_base_ff(ctx: PrepareContext) -> str:

    usecase_args = {
        **ctx.usecase_prepare_args,
    }

    return ctx.prepare_workspace_provider.prepare(
        only_query_md=False,
        write_non_tracked_only=ctx.write_non_tracked_only,
        only_from_cache=ctx.only_from_cache,
        do_not_cache=ctx.do_not_cache,
        usecase_args=usecase_args,
    )
