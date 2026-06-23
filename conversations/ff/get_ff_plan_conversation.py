import logging
from pathlib import Path

from conversations.checkpointed_conversation import CheckpointedConversation
from conversations.ff.prompts_gen import gen_ff_plan_prompt
from conversations.stage_config import StaticStageConfig
from workloads.workload_provider import Workload

logger = logging.getLogger(__name__)


class GenFFPlanConversation(CheckpointedConversation):
    def __init__(
        self,
        benchmark: Workload,
        schema: str,
        workspace_path: Path,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.benchmark = benchmark
        self.schema = schema
        self.workspace_path = workspace_path

    async def run(self):
        self.used = []
        await self._run_stages(self.assemble_stages())

    def assemble_stages(self):
        queries_filename = "queries.md"
        file_format_plan_filename = "file_format_plan.txt"

        def _validate_file_format_plan_exists() -> str | None:
            plan_path = self.workspace_path / file_format_plan_filename
            if plan_path.exists():
                return None
            logger.error(
                f"File format plan {plan_path} does not exist. Reprompting the LLM now."
            )
            return (
                f"Your task was to create a file format plan. However, no file "
                f"called `{file_format_plan_filename}` exists in your workspace. Please "
                f"write the file format plan to `{file_format_plan_filename}` before proceeding."
            )

        return [
            StaticStageConfig(
                descriptor="generate file format plan",
                get_prompt=lambda _sf, _rt: gen_ff_plan_prompt(
                    queries_filename=queries_filename,
                    schema=self.schema,
                    file_format_plan_filename=file_format_plan_filename,
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                post_stage_validate=_validate_file_format_plan_exists,
            ),
        ]
