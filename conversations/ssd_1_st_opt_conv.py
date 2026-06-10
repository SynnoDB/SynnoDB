import logging
from typing import List

from conversations.in_mem_1_optim_conv import InMem1OptimizationConversation
from conversations.prompts_gen import (
    optim_prompt_w_sample_plan,
)
from conversations.stage_config import StaticStageConfig

logger = logging.getLogger(__name__)

# Number of queries to instrument with timing in one LLM interaction.
QUERIES_PER_TIMING_BATCH = 3


class SSD1STOptimConv(InMem1OptimizationConversation):
    def _build_stages(
        self,
        query_id: str,
        mandatory_constraints: str,
        general_pretext: str,
    ) -> List[StaticStageConfig]:
        """Round 1 (single-threaded, algorithmic): Sample Plan only.

        Inner-loop tuning, expert-knowledge application, and the human-reference
        polish all live in round 2 (multi-threading), where the bottleneck profile
        is no longer distorted by single-thread serial I/O. See
        ``Optimization2Conversation._build_stages``.
        """
        sample_plan = self.sample_plan_dict[query_id]

        assert self.plan_source is not None, (
            "Plan source must be specified to build optimization stages."
        )

        return [
            StaticStageConfig(
                descriptor=f"Optim w. Sample Plan ({query_id})",
                # Single-threaded algorithmic stage: pick join order / strategy,
                # filter order, aggregation strategy, storage access pattern.
                # Trace data is now collected fresh and fed into the prompt so the
                # LLM can spot algorithmic mismatches (e.g. an operator dominating
                # runtime that the sample plan does not expect).
                get_prompt_with_tracing=lambda sf, rt, tracing_data: (
                    optim_prompt_w_sample_plan(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        query_plan=sample_plan,
                        sf=sf,
                        engine=self.plan_source,  # type: ignore
                        general_pretext=general_pretext,
                        current_rt_ms=rt,
                        model=self.model,
                        tracing_data=tracing_data,
                        sf_list=self.verify_sf_list,
                        persistent_storage=self.persistent_storage,
                    )
                ),
            ),
        ]
