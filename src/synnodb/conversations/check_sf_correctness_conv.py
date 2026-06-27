import logging
from typing import List, Optional

from synnodb.conversations.checkpointed_conversation import CheckpointedConversation
from synnodb.conversations.filenames import get_filenames
from synnodb.conversations.prompts_gen import (
    optim2_prompt_check_large_sf,
    optim_prompt_constraints,
    optim_prompt_pretext_optim,
)
from synnodb.conversations.stage_config import StaticStageConfig
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

logger = logging.getLogger(__name__)


class CheckSFCorrectnessConv(CheckpointedConversation):
    """Lightweight conversation: verify correctness at a target scale factor.

    Single-stage conversation that reuses ``optim2_prompt_check_large_sf`` to ask
    the agent to validate the existing implementation at ``target_sf`` and fix
    any scaling issues (e.g. int32 overflow) found along the way.
    """

    def __init__(
        self,
        *,
        query_ids: List[str],
        target_sf: float,
        bespoke_storage: bool,
        db_storage: DBStorage,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.query_ids = query_ids
        self.target_sf = target_sf
        self.bespoke_storage = bespoke_storage
        self.persistent_storage = db_storage in [DBStorage.LABSTORE, DBStorage.SSD]
        self.file_paths = get_filenames()

    async def run(self) -> Optional[List[str]]:
        self.used = []

        general_pretext = optim_prompt_pretext_optim(
            bespoke_storage=self.bespoke_storage,
            query_impl_path=self.file_paths["query_impl_path"],
            builder_path=self.file_paths["builder_path"],
            persistent_storage=self.persistent_storage,
        )
        mandatory_constraints = optim_prompt_constraints(
            allow_storage_changes=self.bespoke_storage,
            persistent_storage=self.persistent_storage,
        )

        # drive the target scale factor through the workload provider: BENCHMARK
        # mode will then emit target_sf for this correctness check
        assert isinstance(self._olap_provider, OLAPWorkloadProvider)
        self._olap_provider.set_benchmark_sf(self.target_sf)

        stage = StaticStageConfig(
            descriptor=f"Check correctness at sf {self.target_sf}",
            get_prompt=lambda _exec_settings, _rt: optim2_prompt_check_large_sf(
                general_pretext=general_pretext,
                constraints_str=mandatory_constraints,
                storage_is_bespoke=self.bespoke_storage,
            ),
            max_turns=150,
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
        )

        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info([stage])

        await self._run_stages([stage])

        return await self.ask_to_finish_and_save()
