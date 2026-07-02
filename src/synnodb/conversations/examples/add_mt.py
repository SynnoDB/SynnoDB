"""Stage list to add multi-threading (second optimization round).

In-memory: base implementations are generated parallel-ready through the shared
query pool, so this round simply runs the same code with more CORE_IDS and
tunes bottlenecks such as skew, contention, and memory bandwidth (a single
trace-driven stage per query). SSD still uses the legacy staged introduction
path (thread-pool intro plus the full tuning ladder); it has its own
storage-specific templates/prompts and is intentionally left unchanged.
"""

import logging

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import (
    load_expert_knowledge,
    optim2_prompt_add_threadpool,
    optim2_prompt_check_large_sf,
    optim2_prompt_constraints,
    optim2_prompt_introduce_threading,
    optim2_prompt_optimize_w_trace,
    optim_prompt_pretext_optim,
    optim_prompt_w_expert_knowledge,
    optim_prompt_w_human_reference,
    optim_prompt_w_trace,
)
from synnodb.conversations.stage_items import (
    AssertCorrect,
    Benchmark,
    Compact,
    MeasureBaselines,
    PerQueryLoop,
    PromptStage,
    StageItem,
    ValidateOn,
    ValidateStdoutOn,
)

logger = logging.getLogger(__name__)


def _assemble_pre_stages(
    ctx: ConvContext, mandatory_constraints: str, general_pretext: str
) -> list[StageItem]:
    if not ctx.persistent_storage:
        # in-memory base implementations are already parallel-ready
        return []

    # SSD keeps the legacy staged thread-pool introduction.
    return [
        PromptStage(
            descriptor="Add ThreadPool",
            get_prompt=lambda _exec_settings, _rt: optim2_prompt_add_threadpool(
                db_loader_filename=ctx.filenames.builder_hpp_path,
                thread_pool_filename=ctx.filenames.thread_pool_filename,
                general_pretext=general_pretext,
                constraints_str=mandatory_constraints,
                storage_is_bespoke=ctx.bespoke_storage,
            ),
            max_turns=100,
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
        ),
        Compact(),
    ]


def build_query_stages(
    ctx: ConvContext,
    query_id: str,
    mandatory_constraints: str,
    general_pretext: str,
) -> list[StageItem]:
    stage_exec_settings = ctx.sample_exec_settings()

    if not ctx.persistent_storage:
        # Tuning stages for an already parallel-ready query.
        return [
            PromptStage(
                descriptor=f"Optimize Parallel-Ready MT w. Trace ({query_id})",
                get_prompt_with_tracing=lambda exec_settings, rt, tracing_data: (
                    optim2_prompt_optimize_w_trace(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=rt,
                        current_exec_settings=exec_settings,
                        tracing_data=tracing_data,
                        general_pretext=general_pretext,
                        storage_is_bespoke=ctx.bespoke_storage,
                        single_threaded_rt_ms=ctx.single_threaded_rt_ms[query_id],
                    )
                ),
                max_turns=175,
                exec_settings=stage_exec_settings,
            ),
        ]

    # SSD round 2 stages for a single query:
    #
    # 1. Introduce multi-threading - picks the parallelization pattern based on
    #    the single-threaded trace inherited from round 1.
    # 2. Trace-driven tuning (MT-aware) - per-thread profile attribution.
    # 3. Expert-knowledge application - apply best practices in the MT regime.
    # 4. Human-reference polish - final pass.
    #
    # Stages 2-4 used to live in round 1 (single-threaded). They moved here so
    # the LLM tunes against a real MT bottleneck profile rather than an
    # artificially serial one.

    # load expert knowledge once - shared across all query optimization stages
    expert_knowledge = load_expert_knowledge(persistent_storage=ctx.persistent_storage)

    return [
        PromptStage(
            descriptor=f"Introduce Multi-Threading ({query_id})",
            # Stage 1: pick the parallelization pattern. Tracing data from
            # round 1 (single-threaded) is fed in so the LLM can classify
            # the bottleneck as I/O-bound vs CPU-bound before choosing.
            get_prompt_with_tracing=lambda _exec_settings, _rt, _tracing_data: (
                optim2_prompt_introduce_threading(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    current_rt_ms=_rt,
                    general_pretext=general_pretext,
                    storage_is_bespoke=ctx.bespoke_storage,
                    thread_pool_filename=ctx.filenames.thread_pool_filename,
                    db_loader_header_filename=ctx.filenames.builder_hpp_path,
                    persistent_storage=ctx.persistent_storage,
                    tracing_data=_tracing_data,
                )
            ),
            max_turns=150,
        ),
        PromptStage(
            descriptor=f"Optim w. Tracing Stats MT-aware ({query_id})",
            # Stage 2: MT-aware trace tuning. Targets thread skew, lock
            # contention, and per-thread bottlenecks. Prompt now treats
            # the trace as per-thread data, not aggregate wall time.
            get_prompt_with_tracing=lambda exec_settings, _rt, _tracing_data: (
                optim_prompt_w_trace(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    current_rt_ms=_rt,
                    current_exec_settings=exec_settings,
                    storage_is_bespoke=ctx.bespoke_storage,
                    tracing_data=_tracing_data,
                    general_pretext=general_pretext,
                    model=ctx.model,
                    persistent_storage=ctx.persistent_storage,
                )
            ),
            max_turns=125,
            exec_settings=stage_exec_settings,
        ),
        PromptStage(
            descriptor=f"Optim w. Expert Knowledge ({query_id})",
            # Stage 3: apply domain-expert best practices in the MT regime.
            get_prompt=lambda _exec_settings, _rt: optim_prompt_w_expert_knowledge(
                query_id=query_id,
                constraints_str=mandatory_constraints,
                expert_knowledge=expert_knowledge,
                current_rt_ms=_rt,
                storage_is_bespoke=ctx.bespoke_storage,
                general_pretext=general_pretext,
                model=ctx.model,
                persistent_storage=ctx.persistent_storage,
            ),
            max_turns=150,
        ),
        PromptStage(
            descriptor=f"Optim w. Human Reference ({query_id})",
            # Stage 4: final polish in the style of Thomas Neumann / Matthias Jasny.
            get_prompt_with_tracing=lambda exec_settings, _rt, _tracing_data: (
                optim_prompt_w_human_reference(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    current_rt_ms=_rt,
                    current_exec_settings=exec_settings,
                    storage_is_bespoke=ctx.bespoke_storage,
                    tracing_data=_tracing_data,
                    general_pretext=general_pretext,
                    model=ctx.model,
                    num_turns=125,
                )
            ),
            max_turns=125,
            exec_settings=stage_exec_settings,
        ),
    ]


def build(ctx: ConvContext) -> list[StageItem]:
    query_impl_path = ctx.filenames.query_impl_path
    builder_path = ctx.filenames.builder_path
    # describe the optimization problem (same as round 1)
    pretext_optim = optim_prompt_pretext_optim(
        bespoke_storage=ctx.bespoke_storage,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
        persistent_storage=ctx.persistent_storage,
    )

    # multi-threading constraints (replaces the single-threaded constraints)
    mandatory_constraints = optim2_prompt_constraints(
        allow_storage_changes=ctx.bespoke_storage,
        persistent_storage=ctx.persistent_storage,
    )

    pre_stages = _assemble_pre_stages(
        ctx,
        mandatory_constraints=mandatory_constraints,
        general_pretext=pretext_optim,
    )

    return [
        # ensure the starting implementation (from round 1) is still correct
        AssertCorrect(),
        # turn on validation and stdout output
        ValidateOn(),
        ValidateStdoutOn(),
        # measure single-threaded runtimes at the benchmark scale to use as a
        # baseline for the multi-threading optimization (the tuning-stage
        # prompts close over ctx.single_threaded_rt_ms)
        MeasureBaselines(into="single_threaded_rt_ms"),
        # tracing instrumentation is already present from round 1
        *pre_stages,
        PerQueryLoop(
            build=lambda query_id, loop_ctx: build_query_stages(
                loop_ctx,
                query_id,
                mandatory_constraints,
                general_pretext=pretext_optim,
            ),
            # the conversation has no disposable turn at the branch point, so
            # the loop must emit its no-op anchor turn before branching
            branch_anchor=True,
        ),
        # check at a large scale factor that the optimized implementation is
        # correct and performant beyond the default benchmark scale
        Compact(),
        PromptStage(
            descriptor="Check large SF",
            get_prompt=lambda _exec_settings, _rt: optim2_prompt_check_large_sf(
                general_pretext=pretext_optim,
                constraints_str=mandatory_constraints,
                storage_is_bespoke=ctx.bespoke_storage,
            ),
            max_turns=125,
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            benchmark_sf="large_check",
        ),
        Benchmark(benchmark_sf="large_check"),
    ]
