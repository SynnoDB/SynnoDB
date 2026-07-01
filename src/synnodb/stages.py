"""Stage catalog: the single place each pipeline stage is described end to end.

Every :class:`~synnodb.api.Stage` bundles its config assembly
(``build_config``: typed ``SynnoConfig`` + per-call inputs -> ``RunConfig``), its
workspace ``prepare`` step, its conversation ``factory``, and its ``result``
builder. ``SynnoDB.run()`` imports this module lazily (so plain ``import synnodb``
stays light) and dispatches straight through these descriptors — there is no
argv / argparse round-trip and there are no standalone ``run_*.py`` scripts.

The heavy conversation classes are still imported lazily inside each factory.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from synnodb.api import (
    Stage,
    _build_base_impl,
    _build_correctness,
    _build_multithreaded,
    _build_optimized,
    _build_storage_plan,
    register_stage,
)
from synnodb.cpp_runner.prepare_repo.prepare_olap import (
    prepare_base,
    prepare_mt,
    prepare_optim,
    prepare_replay_source_run,
    prepare_storage_plan,
)
from synnodb.observability.logging.wandb_api_helper import (
    wandb_retrieve_metrics_for_run,
)
from synnodb.utils.cli_config import RunConfig, Usecase
from synnodb.utils.confirm_dialog import await_user_confirmation
from synnodb.utils.gen_common import parse_query_ids
from synnodb.utils.utils import DBStorage

if TYPE_CHECKING:
    from synnodb.api import SynnoConfig
    from synnodb.conversations.conversation_spec import FrameworkContext

_OLAP = frozenset({Usecase.OLAP})


# --------------------------- config-assembly helpers ------------------------
def _base_run_config(cfg: "SynnoConfig") -> dict[str, Any]:
    """Map the typed ``SynnoConfig`` onto the RunConfig kwargs every stage shares.
    Settings the API does not model take ``RunConfig``'s own field defaults — the
    same values the old CLI path got from argparse defaults."""
    return dict(
        model=cfg.model,
        benchmark=cfg.workload,
        db_storage=cfg.db_storage,
        usecase=cfg.usecase,
        queries_str=cfg.queries,
        notify=cfg.notify,
        disable_openai_tracing=cfg.disable_openai_tracing,
        auto_u=cfg.auto_confirm,
        auto_finish=cfg.auto_finish,
        log_to_wandb=cfg.wandb_enabled,
        wandb_entity=cfg.wandb_entity,
        wandb_project=cfg.wandb_project,
        disable_repo_sync=cfg.disable_repo_sync,
        do_not_cache=cfg.do_not_cache,
        workspace_dir=cfg.workspace,
        verbose=cfg.verbose,
        threads=cfg.threads,
        max_turns=cfg.max_turns,
        model_extra_body=cfg.model_extra_body,
    )


def _parse_queries(cfg: "SynnoConfig") -> list[str]:
    query_ids = parse_query_ids(cfg.queries, benchmark=cfg.workload)
    assert query_ids is not None, f"Failed to parse query ids from {cfg.queries!r}"
    return query_ids


def _memory_budget(cfg: "SynnoConfig") -> int | None:
    """Pick a RAM budget only for persistent storage (in-memory uses all RAM)."""
    if cfg.db_storage in (DBStorage.LABSTORE, DBStorage.SSD):
        return 50 * 1024
    return None


def validate_snapshot(snapshot_config, benchmark, queries_str, query_ids, db_storage, model):
    """Validate that a previous run's logged config matches this run's settings."""
    snapshot_benchmark: str = snapshot_config["benchmark"]
    snapshot_queries_str = snapshot_config["queries_str"]
    snapshot_model = snapshot_config["model"]
    snapshot_db_storage = snapshot_config["db_storage"]

    # .value works for both built-in enum members and a WorkloadId (bring-your-own)
    assert snapshot_benchmark.upper() == benchmark.value.upper(), (
        f"Expected benchmark {benchmark.value.upper()} in storage plan run, got {snapshot_benchmark}"
    )
    if queries_str is not None:
        assert snapshot_queries_str == queries_str, (
            f"Expected queries str {queries_str} in storage plan run, got {snapshot_queries_str}"
        )
    assert query_ids == parse_query_ids(snapshot_queries_str, benchmark=benchmark), (
        f"Expected query ids {query_ids} in storage plan run, got {parse_query_ids(snapshot_queries_str, benchmark=benchmark)}"
    )

    if db_storage is not None:
        assert snapshot_db_storage.lower() == db_storage.value.lower(), (
            f"Expected db_storage {db_storage.value.lower()} in storage plan run, got {snapshot_db_storage.lower()}"
        )

    if model is not None and snapshot_model != model:
        response = await_user_confirmation(
            f"Model in storage plan run is {snapshot_model}, but current model is {model}. Do you want to continue?"
        )
        if not response:
            print("Aborting run.")
            import sys

            sys.exit(0)


def resolve_source_snapshot(
    *,
    snapshot: str | None,
    wandb_id: str | None,
    source_kind: str,
    snapshot_flag: str,
    wandb_flag: str,
    benchmark,
    queries_str,
    query_ids,
    db_storage,
    model,
    wandb_entity=None,
    wandb_project=None,
) -> tuple[str, dict | None]:
    """Resolve the git snapshot hash of a previous stage's output. Provide exactly
    one of ``snapshot`` (the git hash directly, W&B-free) or ``wandb_id`` (a W&B run
    id resolved to its logged snapshot and validated against this run's config).

    ``wandb_entity``/``wandb_project`` are the coordinates the producer run was
    logged to (from the driver config), so the lookup reads the same project the
    chain wrote to rather than the env/default fallback.

    Returns ``(commit_hash, source_config)`` where ``source_config`` is the source
    run's W&B config dict (W&B path) or ``None`` (W&B-free path)."""
    if (snapshot is None) == (wandb_id is None):
        raise ValueError(
            f"Provide exactly one of {snapshot_flag} (git snapshot hash, W&B-free) "
            f"or {wandb_flag} (W&B run id) to load the {source_kind} snapshot — got "
            + ("both" if snapshot is not None else "neither")
            + "."
        )
    if snapshot is not None:
        return snapshot, None

    statistics, config, _ = wandb_retrieve_metrics_for_run(
        benchmark,
        wandb_id,
        entity=wandb_entity,
        project=wandb_project,
        fetch_latest_runtimes=False,
    )
    validate_snapshot(config, benchmark, queries_str, query_ids, db_storage=db_storage, model=model)
    commit_hash = statistics["code/snapshot_hash"]
    assert commit_hash != "N/A", (
        f"Could not retrieve a valid commit hash from wandb for run {wandb_id} in "
        f"benchmark {benchmark}. Got {commit_hash}."
    )
    return commit_hash, config


def build_optim_conv_args(ctx: "FrameworkContext"):
    """Shared between the optim and make-mt conversation factories."""
    from synnodb.conversations.optimization_conversation import OptimConvArgs

    assert ctx.query_validator is not None, (
        "query_validator must be provided for optimization conversations (disable_valtool is set?)"
    )
    return OptimConvArgs(
        query_ids=ctx.query_list,
        bespoke_storage=ctx.args.bespoke_storage,
        query_validator=ctx.query_validator,
        plan_source=ctx.args.optimize_sample_plan_source,
        cleanup_plans=True,
        model=ctx.args.model,
        db_storage=ctx.db_storage,
    )


# ------------------------------- build_config -------------------------------
# Each returns a fully-populated RunConfig from the typed SynnoConfig + the
# per-call inputs (the chaining tokens the SynnoDB.<stage> methods pass through).
# The API always runs with a bespoke storage plan, so bespoke_storage=True here.

def _config_storage_plan(cfg: "SynnoConfig", inputs: dict[str, Any]) -> RunConfig:
    return RunConfig(
        **_base_run_config(cfg),
        query_list=",".join(map(str, _parse_queries(cfg))),
        bespoke_storage=True,
    )


def _config_base_impl(cfg: "SynnoConfig", inputs: dict[str, Any]) -> RunConfig:
    query_ids = _parse_queries(cfg)

    # The storage plan reaches us either as raw text (W&B-free) or via a W&B run id
    # we resolve to a git snapshot to recover the plan text from.
    storage_plan_text = inputs.get("storage_plan_text")
    storage_plan_run_id = inputs.get("storage_plan_wandb_id")
    assert (storage_plan_text is None) != (storage_plan_run_id is None), (
        "createBaseImpl requires exactly one of storage_plan_text or "
        "storage_plan_wandb_id"
    )

    storage_plan_snapshot = None
    if storage_plan_text is None:
        statistics, config, _ = wandb_retrieve_metrics_for_run(
            cfg.workload,
            storage_plan_run_id,
            entity=cfg.wandb_entity,
            project=cfg.wandb_project,
        )
        validate_snapshot(
            config, cfg.workload, cfg.queries, query_ids, db_storage=cfg.db_storage, model=cfg.model
        )
        storage_plan_snapshot = statistics["code/snapshot_hash"]
        assert storage_plan_snapshot != "N/A", (
            f"Could not retrieve a valid commit hash from wandb for the storage plan "
            f"run in benchmark {cfg.workload}. Got {storage_plan_snapshot}."
        )

    return RunConfig(
        **_base_run_config(cfg),
        query_list=",".join(map(str, query_ids)),
        keep_csv=False,
        bespoke_storage=True,
        storage_plan_snapshot=storage_plan_snapshot,
        storage_plan_text=storage_plan_text,
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        memory_budget_mb=_memory_budget(cfg),
    )


def _config_optim(cfg: "SynnoConfig", inputs: dict[str, Any]) -> RunConfig:
    query_ids = _parse_queries(cfg)

    # validate model format (provider/model) for non gpt-/anthropic ids
    if not (cfg.model.startswith("anthropic/") or cfg.model.startswith("gpt-")):
        assert "/" in cfg.model, (
            f"Model name {cfg.model} is not in the expected format <provider>/<model_name>"
        )

    # The base implementation reaches us either as a git snapshot hash directly
    # (W&B-free) or via a W&B run id we resolve to that snapshot hash.
    commit_hash, _ = resolve_source_snapshot(
        snapshot=inputs.get("base_impl_snapshot"),
        wandb_id=inputs.get("base_impl"),
        source_kind="base implementation",
        snapshot_flag="base_impl (snapshot)",
        wandb_flag="base_impl_wandb_id",
        benchmark=cfg.workload,
        queries_str=cfg.queries,
        query_ids=query_ids,
        db_storage=cfg.db_storage,
        model=cfg.model,
        wandb_entity=cfg.wandb_entity,
        wandb_project=cfg.wandb_project,
    )

    return RunConfig(
        **_base_run_config(cfg),
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,
        run_tool_offer_trace_option=True,  # collect fine-grained perf traces
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        optimize_sample_plan_source=inputs.get("optimize_sample_plan_source"),
        bespoke_storage=True,
        memory_budget_mb=_memory_budget(cfg),
    )


def _config_make_mt(cfg: "SynnoConfig", inputs: dict[str, Any]) -> RunConfig:
    query_ids = _parse_queries(cfg)

    # The optimized implementation reaches us either as a git snapshot hash directly
    # (W&B-free) or via a W&B run id we resolve to that snapshot hash.
    commit_hash, _ = resolve_source_snapshot(
        snapshot=inputs.get("optim_snapshot"),
        wandb_id=inputs.get("optimized"),
        source_kind="optimized implementation",
        snapshot_flag="optimized (snapshot)",
        wandb_flag="optimized_wandb_id",
        benchmark=cfg.workload,
        queries_str=cfg.queries,
        query_ids=query_ids,
        db_storage=cfg.db_storage,
        model=cfg.model,
        wandb_entity=cfg.wandb_entity,
        wandb_project=cfg.wandb_project,
    )

    return RunConfig(
        **_base_run_config(cfg),
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,
        bespoke_storage=True,
        run_tool_offer_trace_option=True,
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        memory_budget_mb=_memory_budget(cfg),
    )


def _config_check_sf(cfg: "SynnoConfig", inputs: dict[str, Any]) -> RunConfig:
    query_ids = _parse_queries(cfg)
    target_sf = inputs["target_sf"]

    # The source implementation reaches us either as a git snapshot hash directly
    # (W&B-free) or via a W&B run id we resolve to that snapshot hash.
    commit_hash, source_config = resolve_source_snapshot(
        snapshot=inputs.get("source_snapshot"),
        wandb_id=inputs.get("source"),
        source_kind="implementation to validate",
        snapshot_flag="source (snapshot)",
        wandb_flag="source_wandb_id",
        benchmark=cfg.workload,
        queries_str=cfg.queries,
        query_ids=query_ids,
        db_storage=cfg.db_storage,
        model=cfg.model,
        wandb_entity=cfg.wandb_entity,
        wandb_project=cfg.wandb_project,
    )

    # CHECK_SF replays the source run's prepare steps, so it needs that run's
    # stage. The W&B path reads it from the source run config; the W&B-free path
    # is told it explicitly via source_stage (the API derives it from the source
    # artifact's type).
    if source_config is not None:
        source_stage_name = source_config["stage_name"]
    else:
        source_stage_name = inputs.get("source_stage")
        assert source_stage_name is not None, (
            "source_stage is required with a raw source snapshot (the source run's "
            "stage, e.g. 'createBaseImpl', 'runOptimLoop', or 'addMultiThreading')."
        )

    # whole numbers format nicer in prompts (100.0 -> 100)
    if float(target_sf).is_integer():
        target_sf = int(target_sf)

    return RunConfig(
        **_base_run_config(cfg),
        source_stage_name=source_stage_name,
        query_list=",".join(map(str, query_ids)),
        start_snapshot=commit_hash,
        storage_plan_snapshot=None,
        keep_csv=False,
        bespoke_storage=True,
        use_supervision_agent=True,
        use_autonomy_master_prompt=False,
        target_sf=target_sf,
        memory_budget_mb=_memory_budget(cfg),
    )


# --------------------------------- factories --------------------------------
def _factory_storage_plan(ctx: "FrameworkContext"):
    from synnodb.conversations.gen_storage_plan_conversation import (
        GenStoragePlanConversation,
    )

    return GenStoragePlanConversation(
        benchmark=ctx.args.benchmark,
        schema=ctx.workload_provider.dataset_schema,
        workspace_path=ctx.workspace_path,
        db_storage=ctx.db_storage,
        num_threads=ctx.args.target_threads,
        max_turns=ctx.args.max_turns,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


def _factory_base_impl(ctx: "FrameworkContext"):
    from synnodb.conversations.base_impl_conversation import BaseImplConversation
    from synnodb.utils.get_sample_q_args import (
        get_sample_exec_settings,
        get_sample_query_args,
    )
    from synnodb.workloads.workload_provider_olap import OLAPExecSettings

    sample_query_args_dict = get_sample_query_args(workload_provider=ctx.workload_provider)
    exec_settings = get_sample_exec_settings(workload_provider=ctx.workload_provider)
    assert isinstance(exec_settings, OLAPExecSettings)

    return BaseImplConversation(
        benchmark=ctx.args.benchmark,
        read_storage_plan=ctx.args.bespoke_storage,
        sample_query_args_dict=sample_query_args_dict,
        workspace_path=ctx.workspace_path,
        use_master_prompt=ctx.args.use_autonomy_master_prompt,
        sql_dict=ctx.workload_provider.sql_dict,
        db_storage=ctx.db_storage,
        parquet_dir=exec_settings.parquet_dir,
        num_threads=ctx.args.target_threads,
        max_turns=ctx.args.max_turns,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


def _factory_optim(ctx: "FrameworkContext"):
    optim_conv_args = build_optim_conv_args(ctx)

    if ctx.db_storage == DBStorage.IN_MEMORY:
        from synnodb.conversations.in_mem_1_optim_conv import (
            InMem1OptimizationConversation,
        )

        return InMem1OptimizationConversation(
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    elif ctx.db_storage == DBStorage.SSD:
        from synnodb.conversations.ssd_1_st_opt_conv import SSD1STOptimConv

        return SSD1STOptimConv(
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    else:
        raise Exception(f"Unsupported db_storage for optim conversation: {ctx.db_storage}")


def _factory_make_mt(ctx: "FrameworkContext"):
    optim_conv_args = build_optim_conv_args(ctx)

    if ctx.db_storage == DBStorage.IN_MEMORY:
        from synnodb.conversations.in_mem_2_mt_conv import InMem2MTConversation

        return InMem2MTConversation(
            benchmark=ctx.args.benchmark,
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    elif ctx.db_storage == DBStorage.SSD:
        from synnodb.conversations.ssd_2_mt_conv import SSD2MTOptConv

        return SSD2MTOptConv(
            benchmark=ctx.args.benchmark,
            optim_conv_args=optim_conv_args,
            **ctx.auto_conversation_args,
            **ctx.conv_args,
        )
    else:
        raise Exception(f"Unsupported db_storage for make_mt conversation: {ctx.db_storage}")


def _factory_check_sf(ctx: "FrameworkContext"):
    from synnodb.conversations.check_sf_correctness_conv import CheckSFCorrectnessConv

    assert ctx.args.target_sf is not None, (
        "target_sf must be provided for check-sf correctness conversation"
    )
    return CheckSFCorrectnessConv(
        query_ids=ctx.query_list,
        target_sf=ctx.args.target_sf,
        bespoke_storage=ctx.args.bespoke_storage,
        db_storage=ctx.db_storage,
        **ctx.auto_conversation_args,
        **ctx.conv_args,
    )


# ------------------------------- registration -------------------------------
register_stage(Stage(
    name="createStoragePlan",
    usecases=_OLAP,
    build_config=_config_storage_plan,
    prepare=prepare_storage_plan,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory_storage_plan,
    result=_build_storage_plan,
))

register_stage(Stage(
    name="createBaseImpl",
    usecases=_OLAP,
    build_config=_config_base_impl,
    prepare=prepare_base,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory_base_impl,
    result=_build_base_impl,
))

register_stage(Stage(
    name="runOptimLoop",
    usecases=_OLAP,
    build_config=_config_optim,
    prepare=prepare_optim,
    needs_parallelism=False,
    be_relaxed_supervision=True,
    factory=_factory_optim,
    result=_build_optimized,
))

register_stage(Stage(
    name="addMultiThreading",
    usecases=_OLAP,
    build_config=_config_make_mt,
    prepare=prepare_mt,
    needs_parallelism=True,
    be_relaxed_supervision=True,
    factory=_factory_make_mt,
    result=_build_multithreaded,
))

# Dynamic prepare: CHECK_SF replays the source run's prepare. The source stage
# name is on the RunConfig; prepare_replay_source_run resolves that stage and
# runs its prepare, and main.py resolves parallelism from it too.
register_stage(Stage(
    name="checkSfCorrectness",
    usecases=_OLAP,
    build_config=_config_check_sf,
    prepare=prepare_replay_source_run,
    needs_parallelism=False,
    be_relaxed_supervision=False,
    factory=_factory_check_sf,
    result=_build_correctness,
))
