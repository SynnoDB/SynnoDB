"""Stage list of the runOptimLoop conversation (single-threaded optimization).

The pre-optimization setup (timing instrumentation) is shared; the per-query
optimization stages differ by storage plane: the in-memory round runs the full
four-stage ladder, the SSD round runs the sample-plan stage only (inner-loop
tuning, expert knowledge, and the human-reference polish live in the MT round,
where the bottleneck profile is no longer distorted by single-thread serial
I/O).
"""

import logging

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import (
    load_expert_knowledge,
    optim_prompt_add_timings_per_query,
    optim_prompt_add_timings_pretext,
    optim_prompt_constraints,
    optim_prompt_pretext,
    optim_prompt_pretext_optim,
    optim_prompt_w_expert_knowledge,
    optim_prompt_w_human_reference,
    optim_prompt_w_sample_plan,
    optim_prompt_w_trace,
)
from synnodb.conversations.stage_items import (
    AssertCorrect,
    Benchmark,
    Compact,
    PerQueryLoop,
    PromptStage,
    StageItem,
    SupervisionHorizon,
    ValidateOn,
)
from synnodb.tools.run_tool_mode import RunToolMode

logger = logging.getLogger(__name__)

# Number of queries to instrument with timing in one LLM interaction.
QUERIES_PER_TIMING_BATCH = 3


def assemble_pre_optim_stages(ctx: ConvContext, pretext: str) -> list[StageItem]:
    """Return the ordered flat list of setup stages before per-query optimization."""
    add_timings_prompt_pretext = optim_prompt_add_timings_pretext()

    stage_list: list[StageItem] = [
        ValidateOn(),
        Benchmark(),
    ]

    for i in range(0, len(ctx.query_ids), QUERIES_PER_TIMING_BATCH):
        qids = ctx.query_ids[i : i + QUERIES_PER_TIMING_BATCH]
        qids_str = ", ".join(qids)
        is_first = i == 0

        def _timings_prompt(
            _exec_settings, _rt, *, qids_str=qids_str, is_first=is_first
        ):
            prompt = optim_prompt_add_timings_per_query(
                qids_str=qids_str,
                refer_to_prev_queries=not is_first,
            )
            return (add_timings_prompt_pretext + "\n" + prompt) if is_first else prompt

        def _validate_timings(*, qids=qids) -> str | None:
            for trace_mode in [False, True]:
                _, metrics, tracing_output = ctx.run_tool.run(
                    mode=RunToolMode.EXHAUSTIVE,
                    optimize=True,
                    query_ids=qids,
                    trace_mode=trace_mode,
                    external_call=True,
                )
                if metrics is None or not metrics["validation/correct"]:
                    return (
                        f"The implementation produces incorrect results for queries "
                        f"{', '.join(qids)} (trace_mode={trace_mode}). "
                        f"Please fix the timing instrumentation to not break query correctness."
                    )
            return None

        stage_list.extend(
            [
                PromptStage(
                    descriptor=f"Add Timings for Queries {qids_str}",
                    get_prompt=_timings_prompt,
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                    post_stage_validate=_validate_timings,
                ),
                Compact(),
            ]
        )

    stage_list.append(SupervisionHorizon())
    stage_list.append(Benchmark())

    return stage_list


def build_query_stages(
    ctx: ConvContext,
    query_id: str,
    mandatory_constraints: str,
    general_pretext: str,
    plan_source: str,
) -> list[StageItem]:
    """Return the ordered list of optimization stages for a single query."""
    sample_plan = ctx.reference_plans(source=plan_source)[query_id]
    stage_exec_settings = ctx.sample_exec_settings()

    sample_plan_stage = PromptStage(
        descriptor=f"Optim w. Sample Plan ({query_id})",
        # Stage 1: use the sample plan for cardinality / optimizer hints. On the
        # in-memory plane the current runtime is not yet known; on SSD the
        # runtime and fresh trace data let the LLM spot algorithmic mismatches
        # (e.g. an operator dominating runtime that the sample plan does not
        # expect).
        get_prompt_with_tracing=lambda exec_settings, rt, tracing_data: (
            optim_prompt_w_sample_plan(
                query_id=query_id,
                constraints_str=mandatory_constraints,
                query_plan=sample_plan,
                current_rt_ms=rt,
                current_exec_settings=exec_settings,
                engine=plan_source,
                general_pretext=general_pretext,
                model=ctx.model,
                persistent_storage=ctx.persistent_storage,
                tracing_data=tracing_data,
            )
        ),
        exec_settings=stage_exec_settings,
    )

    if ctx.persistent_storage:
        # SSD round 1 (single-threaded, algorithmic): Sample Plan only.
        return [sample_plan_stage]

    # load expert knowledge once - shared across all query optimization stages
    expert_knowledge = load_expert_knowledge(persistent_storage=ctx.persistent_storage)

    return [
        sample_plan_stage,
        PromptStage(
            descriptor=f"Optim w. Tracing Stats ({query_id})",
            # Stage 2: use tracing statistics; target 10x improvement.
            get_prompt_with_tracing=lambda exec_settings, rt, tracing_data: (
                optim_prompt_w_trace(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    current_rt_ms=rt,
                    current_exec_settings=exec_settings,
                    storage_is_bespoke=ctx.bespoke_storage,
                    tracing_data=tracing_data,
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
            # Stage 3: apply domain-expert best practices; target 2x improvement.
            get_prompt=lambda _exec_settings, rt: optim_prompt_w_expert_knowledge(
                query_id=query_id,
                constraints_str=mandatory_constraints,
                expert_knowledge=expert_knowledge,
                current_rt_ms=rt,
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
            get_prompt_with_tracing=lambda exec_settings, rt, tracing_data: (
                optim_prompt_w_human_reference(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    current_rt_ms=rt,
                    current_exec_settings=exec_settings,
                    storage_is_bespoke=ctx.bespoke_storage,
                    tracing_data=tracing_data,
                    general_pretext=general_pretext,
                    model=ctx.model,
                    num_turns=125,
                )
            ),
            max_turns=125,
            exec_settings=stage_exec_settings,
        ),
    ]


def build(ctx: ConvContext, *, plan_source: str = "umbra") -> list[StageItem]:
    queries_path = ctx.filenames.queries_path
    query_impl_path = ctx.filenames.query_impl_path
    builder_path = ctx.filenames.builder_path
    # describe the optimization problem
    pretext_optim = optim_prompt_pretext_optim(
        bespoke_storage=ctx.bespoke_storage,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
        persistent_storage=ctx.persistent_storage,
    )

    # what the agent is allowed to change in the codebase to optimize performance
    mandatory_constraints = optim_prompt_constraints(
        allow_storage_changes=ctx.bespoke_storage,
        persistent_storage=ctx.persistent_storage,
    )

    pre_optim_stages = assemble_pre_optim_stages(
        ctx,
        optim_prompt_pretext(
            queries_path=queries_path,
            num_queries=len(ctx.query_ids),
            query_impl_path=query_impl_path,
            builder_path=builder_path,
        ),
    )

    return [
        AssertCorrect(),
        *pre_optim_stages,
        PerQueryLoop(
            build=lambda query_id, loop_ctx: build_query_stages(
                loop_ctx,
                query_id,
                mandatory_constraints,
                general_pretext=pretext_optim,
                plan_source=plan_source,
            ),
        ),
    ]
