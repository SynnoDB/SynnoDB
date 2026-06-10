import logging
from typing import List

from conversations.in_mem_2_mt_conv import InMem2MTConversation
from conversations.prompts_gen import (
    load_expert_knowledge,
    optim2_prompt_introduce_threading,
    optim_prompt_w_expert_knowledge,
    optim_prompt_w_human_reference,
    optim_prompt_w_trace,
)
from conversations.stage_config import StageConfig, StaticStageConfig

logger = logging.getLogger(__name__)


class SSD2MTOptConv(InMem2MTConversation):
    """Second optimization round: add multi-threading to the existing implementation.

    Starts from a snapshot produced by OptimizationConversation (single-threaded).
    The generated C++ code reads CORE_IDS from the environment at runtime to
    configure both the thread pool size and CPU pinning.

    Inherits all measurement/revert infrastructure and overrides:
      - stage definitions  → multi-threading specific stages
      - run()              → no timing-instrumentation setup
                             (already done in round 1)
    """

    def __init__(self, benchmark_sf_pre: float, **kwargs):
        super().__init__(**kwargs)
        self.benchmark_sf_pre = benchmark_sf_pre  # pre added mt

    def _build_stages(
        self,
        query_id: str,
        mandatory_constraints: str,
        general_pretext: str,
    ) -> List[StageConfig]:
        """Round 2 stages for a single query:

        1. Introduce multi-threading — picks the parallelization pattern based on
           the single-threaded trace inherited from round 1.
        2. Trace-driven tuning (MT-aware) — per-thread profile attribution.
        3. Expert-knowledge application — apply best practices in the MT regime.
        4. Human-reference polish — final pass.

        Stages 2-4 used to live in round 1 (single-threaded). They moved here so
        the LLM tunes against a real MT bottleneck profile rather than an
        artificially serial one.
        """
        sf_pre = self.benchmark_sf_pre

        # load expert knowledge once - shared across all query optimization stages
        expert_knowledge = load_expert_knowledge(
            persistent_storage=self.persistent_storage
        )

        return [
            StaticStageConfig(
                descriptor=f"Introduce Multi-Threading ({query_id})",
                # Stage 1: pick the parallelization pattern. Tracing data from
                # round 1 (single-threaded) is fed in so the LLM can classify
                # the bottleneck as I/O-bound vs CPU-bound before choosing.
                get_prompt_with_tracing=lambda _sf, _rt, _tracing_data: (
                    optim2_prompt_introduce_threading(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=_rt,
                        sf=_sf,
                        general_pretext=general_pretext,
                        storage_is_bespoke=self.bespoke_storage,
                        thread_pool_filename=self.file_paths["thread_pool_filename"],
                        db_loader_header_filename=self.file_paths["builder_hpp_path"],
                        run_tool_sf=_sf,
                        persistent_storage=self.persistent_storage,
                        tracing_data=_tracing_data,
                    )
                ),
                max_turns=150,
                sf=sf_pre,
            ),
            StaticStageConfig(
                descriptor=f"Optim w. Tracing Stats MT-aware ({query_id})",
                # Stage 2: MT-aware trace tuning. Targets thread skew, lock
                # contention, and per-thread bottlenecks. Prompt now treats
                # the trace as per-thread data, not aggregate wall time.
                get_prompt_with_tracing=lambda _sf, _rt, _tracing_data: (
                    optim_prompt_w_trace(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=_rt,
                        sf=_sf,
                        storage_is_bespoke=self.bespoke_storage,
                        tracing_data=_tracing_data,
                        general_pretext=general_pretext,
                        model=self.model,
                        persistent_storage=self.persistent_storage,
                    )
                ),
                max_turns=125,
            ),
            StaticStageConfig(
                descriptor=f"Optim w. Expert Knowledge ({query_id})",
                # Stage 3: apply domain-expert best practices in the MT regime.
                get_prompt=lambda _sf, _rt: optim_prompt_w_expert_knowledge(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    expert_knowledge=expert_knowledge,
                    current_rt_ms=_rt,
                    sf=_sf,
                    storage_is_bespoke=self.bespoke_storage,
                    general_pretext=general_pretext,
                    model=self.model,
                    persistent_storage=self.persistent_storage,
                ),
                max_turns=150,
            ),
            StaticStageConfig(
                descriptor=f"Optim w. Human Reference ({query_id})",
                # Stage 4: final polish in the style of Thomas Neumann / Matthias Jasny.
                get_prompt_with_tracing=lambda _sf, _rt, _tracing_data: (
                    optim_prompt_w_human_reference(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=_rt,
                        sf=_sf,
                        storage_is_bespoke=self.bespoke_storage,
                        tracing_data=_tracing_data,
                        general_pretext=general_pretext,
                        model=self.model,
                        num_turns=125,
                    )
                ),
                max_turns=125,
            ),
        ]
