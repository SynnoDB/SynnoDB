"""Stage list of the checkSfCorrectness conversation.

Single-stage conversation that reuses ``optim2_prompt_check_large_sf`` to ask
the agent to validate the existing implementation at ``target_sf`` and fix any
scaling issues (e.g. int32 overflow) found along the way.
"""

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import (
    optim2_prompt_check_large_sf,
    optim_prompt_constraints,
    optim_prompt_pretext_optim,
)
from synnodb.conversations.stage_items import PromptStage, StageItem


def build(ctx: ConvContext, *, target_sf: float) -> list[StageItem]:
    general_pretext = optim_prompt_pretext_optim(
        bespoke_storage=ctx.bespoke_storage,
        query_impl_path=ctx.filenames.query_impl_path,
        builder_path=ctx.filenames.builder_path,
        persistent_storage=ctx.persistent_storage,
    )
    mandatory_constraints = optim_prompt_constraints(
        allow_storage_changes=ctx.bespoke_storage,
        persistent_storage=ctx.persistent_storage,
    )

    return [
        PromptStage(
            descriptor=f"Check correctness at sf {target_sf}",
            get_prompt=lambda _exec_settings, _rt: optim2_prompt_check_large_sf(
                general_pretext=general_pretext,
                constraints_str=mandatory_constraints,
                storage_is_bespoke=ctx.bespoke_storage,
            ),
            max_turns=150,
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            # drive the target scale factor through the workload provider:
            # BENCHMARK mode then emits target_sf for this correctness check
            benchmark_sf=target_sf,
        )
    ]
