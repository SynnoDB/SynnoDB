"""Stage list of the createStoragePlan conversation."""

import logging

import litellm

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import gen_storage_plan_prompt
from synnodb.conversations.stage_items import PromptStage, StageItem
from synnodb.utils.model_setup import setup_model_config

logger = logging.getLogger(__name__)


def _judge_storage_plan(ctx: ConvContext, schema: str, plan_text: str) -> str | None:
    """One-off LLM check: does ``plan_text`` look like a real storage-layout plan
    for ``schema``? Returns None if it looks valid, else a short reason it doesn't.

    A single lightweight completion (no tools, no session, not cached) - separate
    from the main conversation so a bad judgment can't derail the stage's turn
    budget. Still resolves api_base/api_key the same way the main conversation's
    model wrapper does (setup_model_config), so a self-hosted/local model actually
    gets reached instead of silently falling through to the provider's cloud
    default, and reports its cost to the run's stats collector.
    """
    judge_prompt = (
        "You are reviewing a storage-layout design document an engineer just wrote "
        "for a columnar query engine, given the schema below. Judge ONLY whether the "
        "document is a real, substantive storage-layout plan (describes column types, "
        "encodings, and layout choices for the given schema) - not whether it is optimal.\n\n"
        f"--- schema ---\n{schema}\n--- end schema ---\n\n"
        f"--- storage_plan.txt ---\n{plan_text}\n--- end storage_plan.txt ---\n\n"
        "Respond with exactly one line: `VALID` if it is a real, substantive plan, or "
        "`INVALID: <one-sentence reason>` if it is empty, boilerplate, or does not "
        "describe a storage layout for this schema."
    )

    _use_litellm, model_name, api_key, _openai_client, api_base = setup_model_config(
        ctx.model
    )

    try:
        response = litellm.completion(
            model=model_name,
            messages=[{"role": "user", "content": judge_prompt}],
            max_tokens=200,
            api_key=api_key,
            api_base=api_base,
        )
        verdict = response.choices[0].message.content.strip()
        cost = litellm.completion_cost(response)
    except Exception as e:
        logger.warning(
            f"Storage plan validity judge call failed ({e}); skipping check."
        )
        return None

    logger.info(f"Storage plan judge cost: ${cost:.6f}")
    run_stats_collector = ctx.run_tool.run_stats_collector
    if run_stats_collector is not None:
        run_stats_collector.add_to_activity_summary(
            f"Storage plan judge: {verdict.splitlines()[0]} (${cost:.6f})"
        )

    if verdict.upper().startswith("VALID"):
        return None
    return verdict


def build(ctx: ConvContext) -> list[StageItem]:
    storage_plan_filename = ctx.filenames.plan_filename
    schema = ctx.workload_provider.dataset_schema

    def _validate_storage_plan() -> str | None:
        plan_path = ctx.workspace_path / storage_plan_filename
        if not plan_path.exists():
            logger.error(
                f"Storage plan {plan_path} does not exist. Reprompting the LLM now."
            )
            return (
                f"Your task was to create a storage layout summary. However, no file "
                f"called `{storage_plan_filename}` exists in your workspace. Please "
                f"write the storage layout summary to `{storage_plan_filename}` before proceeding."
            )

        plan_text = plan_path.read_text(encoding="utf-8").strip()
        if not plan_text:
            logger.error(f"Storage plan {plan_path} is empty. Reprompting the LLM now.")
            return (
                f"The file `{storage_plan_filename}` exists but is empty. Please write "
                f"an actual storage layout summary to `{storage_plan_filename}` before proceeding."
            )

        invalid_reason = _judge_storage_plan(ctx, schema, plan_text)
        if invalid_reason is not None:
            logger.error(f"Storage plan {plan_path} judged invalid: {invalid_reason}")
            return (
                f"The storage layout summary in `{storage_plan_filename}` was reviewed "
                f"and found insufficient: {invalid_reason}\nPlease revise "
                f"`{storage_plan_filename}` to address this before proceeding."
            )
        return None

    return [
        PromptStage(
            descriptor="generate storage plan",
            get_prompt=lambda _sf, _rt: gen_storage_plan_prompt(
                queries_filename=ctx.filenames.queries_path,
                schema=schema,
                storage_plan_filename=storage_plan_filename,
                persistent_storage=ctx.persistent_storage,
                num_threads=ctx.threads,
            ),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            post_stage_validate=_validate_storage_plan,
            max_turns=ctx.max_turns,
        ),
    ]
