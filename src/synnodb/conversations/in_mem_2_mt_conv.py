import logging
from typing import List, Optional

from synnodb.conversations.conversation import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_ON,
)
from synnodb.conversations.optimization_conversation import OptimizationConversation
from synnodb.conversations.prompts_gen import (
    optim2_prompt_check_large_sf,
    optim2_prompt_constraints,
    optim2_prompt_optimize_w_trace,
    optim_prompt_pretext_optim,
)
from synnodb.conversations.stage_config import StageConfig, StaticStageConfig
from synnodb.tools.run import delete_result_files
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

logger = logging.getLogger(__name__)


class InMem2MTConversation(OptimizationConversation):
    """Second optimization round: tune already parallel-ready in-memory queries.

    Base in-memory implementations are generated through the shared query pool
    from the start. They are validated at CORE_IDS=1, then this round simply
    runs the same code with more CORE_IDS and optimizes bottlenecks such as skew,
    contention, and memory bandwidth. It should not introduce a separate MT code
    path or add the thread pool; those are framework/base responsibilities.

    Inherits all measurement/revert infrastructure and overrides:
      - stage definitions  → MT-specific tuning stages
      - run()              → no timing-instrumentation setup
                             (already done in round 1)
    """

    def __init__(self, benchmark: str, **kwargs):
        super().__init__(**kwargs)
        self.benchmark = benchmark

    def _build_stages(
        self,
        query_id: str,
        mandatory_constraints: str,
        general_pretext: str,
    ) -> List[StageConfig]:
        """Tuning stages for an already parallel-ready query."""

        configs: list[StaticStageConfig] = [
            StaticStageConfig(
                descriptor=f"Optimize Parallel-Ready MT w. Trace ({query_id})",
                get_prompt_with_tracing=lambda _exec_settings, rt, tracing_data: (
                    optim2_prompt_optimize_w_trace(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=rt,
                        tracing_data=tracing_data,
                        general_pretext=general_pretext,
                        storage_is_bespoke=self.bespoke_storage,
                        single_threaded_rt_ms=self.single_threaded_rt_ms[query_id],
                    )
                ),
                max_turns=175,
            ),
        ]

        return configs

    def _assemble_pre_stages(
        self, mandatory_constraints: str, general_pretext: str
    ) -> List[StageConfig | str]:
        return []

    # shared with ssd-optim conversation
    async def run(self) -> Optional[List[str]]:
        self.used = []

        queries_path = self.file_paths["queries_path"]
        query_impl_path = self.file_paths["query_impl_path"]
        builder_path = self.file_paths["builder_path"]
        # describe the optimization problem (same as round 1)
        pretext_optim = optim_prompt_pretext_optim(
            bespoke_storage=self.bespoke_storage,
            query_impl_path=query_impl_path,
            builder_path=builder_path,
            persistent_storage=self.persistent_storage,
        )

        # multi-threading constraints (replaces the single-threaded constraints)
        mandatory_constraints = optim2_prompt_constraints(
            allow_storage_changes=self.bespoke_storage,
            persistent_storage=self.persistent_storage,
        )

        # ensure the starting implementation (from round 1) is still correct
        correct, _, _ = self._check_correctness(self.query_ids, trace_mode=False)
        assert correct, (
            "Initial implementation does not produce correct results. "
            "Please fix it before starting multi-threading optimization."
        )

        # turn on validation and stdout output
        await self._exec(VALIDATE_ON, None, current_stage_nr=0)
        await self._exec(VALIDATE_OUTPUT_STDOUT_ON, None, current_stage_nr=1)

        # measure single-threaded runtimes at the benchmark scale to use as a
        # baseline for the multi-threading optimization
        self.single_threaded_rt_ms = {}
        _, metrics, _ = self.run_tool.run(
            mode=RunToolMode.BENCHMARK,
            optimize=True,
            query_ids=self.query_ids,
            external_call=True,
        )
        assert metrics is not None
        for query_id in self.query_ids:
            q_3d_str = query_id.zfill(3)
            key = f"validation/query_{q_3d_str}/impl_runtime_ms"
            assert key in metrics, (
                f"Expected metric {key} not found in {metrics.keys()}"
            )
            self.single_threaded_rt_ms[query_id] = metrics[key]

        # tracing instrumentation is already present from round 1; no need to re-add it.
        # delete any stale result files before starting the loop
        delete_result_files(self.run_tool.cwd)

        # cleanup up supervision agent horizon - there are no clear outlined stages following
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info([])

        # assemble and run the multi-threading optimization stages for all queries
        pre_stages = self._assemble_pre_stages(
            mandatory_constraints=mandatory_constraints,
            general_pretext=pretext_optim,
        )
        await self._run_stages(
            pre_stages,
            stage_nr_offset=2,
        )

        branch_anchor_stage_nr = 2 + len(pre_stages)
        # The SDK branch helper copies turns strictly before the requested turn.
        # Branching from this no-op anchor keeps the anchor out of per-query branches.
        await self._exec(
            (
                "We are about to create one conversation branch per query for the "
                "multi-threading tuning loop. The base implementation is already "
                "parallel-ready through the shared query pool. Do not inspect files, "
                "do not use tools, and do not change code. Reply exactly: Ready for "
                "per-query branches."
            ),
            "Branch Anchor",
            current_stage_nr=branch_anchor_stage_nr,
            max_turns=5,
        )

        # run the shared per-query optimization loop
        optim_stage_offset = branch_anchor_stage_nr + 1
        stage_end_msg, _, _ = await self._run_optimization_loop(
            mandatory_constraints=mandatory_constraints,
            pretext_optim=pretext_optim,
            start_stage_nr=optim_stage_offset,
        )
        per_query_stage_count = len(
            self._build_stages(
                self.query_ids[0],
                mandatory_constraints,
                general_pretext=pretext_optim,
            )
        )

        # run a check at a large scale factor to confirm the optimized
        # implementation is correct and performant beyond the default benchmark
        # scale. We drive this off the workload provider: temporarily raise its
        # BENCHMARK scale factor, run the check, then restore the default.
        assert isinstance(self._olap_provider, OLAPWorkloadProvider)
        default_benchmark_sf = self._olap_provider.benchmark_sf
        # Large-scale check SF travels with the workload (TPC-H: 100, CEB: 10), read off
        # the provider's spec. Previously `100 if self.benchmark == "tpch" else 10`, which
        # always yielded 10 because self.benchmark is a Workload enum, not a str. Fall back
        # to the configured benchmark SF for a workload that declares no large_check_sf.
        large_sf = self._olap_provider.spec.large_check_sf
        if large_sf is None:
            large_sf = default_benchmark_sf
        self._olap_provider.set_benchmark_sf(large_sf)

        await self._run_stages(
            stage_list=[
                COMPACTION_MARKER,
                StaticStageConfig(
                    descriptor="Check large SF",
                    get_prompt=lambda _exec_settings, _rt: optim2_prompt_check_large_sf(
                        general_pretext=pretext_optim,
                        constraints_str=mandatory_constraints,
                        storage_is_bespoke=self.bespoke_storage,
                    ),
                    max_turns=125,
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                ),
                BENCHMARK_MARKER,
            ],
            stage_nr_offset=optim_stage_offset
            + len(self.query_ids) * per_query_stage_count,
        )

        # restore the default benchmark scale factor for any subsequent runs
        self._olap_provider.set_benchmark_sf(default_benchmark_sf)

        logger.info(f"Final validation metrics after MT optimization: {stage_end_msg}")

        used = await self.ask_to_finish_and_save()
        return used
