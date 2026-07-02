"""Stage list of the createStoragePlan conversation."""

import logging

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import gen_storage_plan_prompt
from synnodb.conversations.stage_items import PromptStage, StageItem

logger = logging.getLogger(__name__)


def build(ctx: ConvContext) -> list[StageItem]:
    storage_plan_filename = ctx.filenames.plan_filename

    def _validate_storage_plan_exists() -> str | None:
        plan_path = ctx.workspace_path / storage_plan_filename
        if plan_path.exists():
            return None
        logger.error(
            f"Storage plan {plan_path} does not exist. Reprompting the LLM now."
        )
        return (
            f"Your task was to create a storage layout summary. However, no file "
            f"called `{storage_plan_filename}` exists in your workspace. Please "
            f"write the storage layout summary to `{storage_plan_filename}` before proceeding."
        )

    return [
        PromptStage(
            descriptor="generate storage plan",
            get_prompt=lambda _sf, _rt: gen_storage_plan_prompt(
                queries_filename=ctx.filenames.queries_path,
                schema=ctx.workload_provider.dataset_schema,
                storage_plan_filename=storage_plan_filename,
                persistent_storage=ctx.persistent_storage,
                num_threads=ctx.threads,
            ),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            post_stage_validate=_validate_storage_plan_exists,
            max_turns=ctx.max_turns,
        ),
    ]
