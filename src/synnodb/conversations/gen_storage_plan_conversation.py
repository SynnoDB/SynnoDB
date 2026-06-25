import logging
from pathlib import Path

from synnodb.conversations.checkpointed_conversation import CheckpointedConversation
from synnodb.conversations.filenames import get_filenames
from synnodb.conversations.prompts_gen import gen_storage_plan_prompt
from synnodb.conversations.stage_config import StaticStageConfig
from synnodb.observability.logging.debug_logger import DebugLogger
from synnodb.utils.utils import DBStorage, storage_label
from synnodb.workloads.workload_provider import Workload

logger = logging.getLogger(__name__)


class GenStoragePlanConversation(CheckpointedConversation):
    def __init__(
        self,
        benchmark: Workload,
        schema: str,
        workspace_path: Path,
        db_storage: DBStorage,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.benchmark = benchmark
        self.schema = schema
        self.workspace_path = workspace_path
        self.db_storage = db_storage

    async def run(self):
        self.used = []
        self.run_stats_collector.debug_logger = DebugLogger(
            category="storage_plan",
            storage=storage_label(self.db_storage),
            model=self.run_stats_collector.model,
            base_dir=self.workspace_path / "debug_logs",
        )
        try:
            await self._run_stages(self.assemble_stages())
        finally:
            self.run_stats_collector.debug_logger = None

    def assemble_stages(self):
        filenames = get_filenames()
        queries_filename = filenames["queries_path"]
        storage_plan_filename = filenames["plan_filename"]

        def _validate_storage_plan_exists() -> str | None:
            plan_path = self.workspace_path / storage_plan_filename
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
            StaticStageConfig(
                descriptor="generate storage plan",
                get_prompt=lambda _sf, _rt: gen_storage_plan_prompt(
                    queries_filename=queries_filename,
                    schema=self.schema,
                    storage_plan_filename=storage_plan_filename,
                    persistent_storage=self.db_storage
                    in [DBStorage.LABSTORE, DBStorage.SSD],
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                post_stage_validate=_validate_storage_plan_exists,
            ),
        ]
