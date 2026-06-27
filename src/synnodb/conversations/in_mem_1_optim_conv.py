import logging
from typing import List, Optional

from synnodb.conversations.conversation import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_ON,
)
from synnodb.conversations.optimization_conversation import OptimizationConversation
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
from synnodb.conversations.stage_config import StageConfig, StaticStageConfig
from synnodb.conversations.supervision_agent import SUPERVISION_STAGE_VISIBILITY_MARKER
from synnodb.tools.run import delete_result_csv_files
from synnodb.tools.run_tool_mode import RunToolMode

logger = logging.getLogger(__name__)

# Number of queries to instrument with timing in one LLM interaction.
QUERIES_PER_TIMING_BATCH = 3


class InMem1OptimizationConversation(OptimizationConversation):
    def assemble_pre_optim_stages(
        self,
        pretext: str,
    ) -> list[StageConfig | str]:
        """Return the ordered flat list of setup stages before per-query optimization."""
        # pinning_prompt = optim_prompt_pinning(core_id=3)
        add_timings_prompt_pretext = optim_prompt_add_timings_pretext()

        stage_list: list[StageConfig | str] = [
            # pinning done by the framework.
            # StaticStageConfig(
            #     descriptor="Pinning",
            #     get_prompt=lambda _rt: pretext + "\n" + pinning_prompt,
            #     measure_performance_after_stage=False,
            #     auto_revert_on_regression=False,
            # ),
            VALIDATE_ON,
            BENCHMARK_MARKER,
        ]

        for i in range(0, len(self.query_ids), QUERIES_PER_TIMING_BATCH):
            qids = self.query_ids[i : i + QUERIES_PER_TIMING_BATCH]
            qids_str = ", ".join(qids)
            is_first = i == 0

            def _timings_prompt(_exec_settings, _rt, *, qids_str=qids_str, is_first=is_first):
                prompt = optim_prompt_add_timings_per_query(
                    qids_str=qids_str,
                    refer_to_prev_queries=not is_first,
                )
                return (
                    (add_timings_prompt_pretext + "\n" + prompt) if is_first else prompt
                )

            def _validate_timings(*, qids=qids) -> str | None:
                for trace_mode in [False, True]:
                    _, metrics, tracing_output = self.run_tool.run(
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
                    StaticStageConfig(
                        descriptor=f"Add Timings for Queries {qids_str}",
                        get_prompt=_timings_prompt,
                        measure_performance_after_stage=False,
                        auto_revert_on_regression=False,
                        post_stage_validate=_validate_timings,
                    ),
                    COMPACTION_MARKER,
                ]
            )

        def _assert_tracing_output() -> str | None:
            # run in trace mode and check that trace data was returned via the pipe
            _, _, trace_output = self.run_tool.run(
                mode=RunToolMode.BENCHMARK,
                optimize=True,
                query_ids=None,
                trace_mode=True,
                external_call=True,
            )

            if not trace_output or trace_output.strip() == "":
                return (
                    "Tracing output is empty after the stage that adds timings. "
                    "Please ensure the tracing instrumentation is working correctly and producing output for optimization."
                )
            return None

        stage_list.append(SUPERVISION_STAGE_VISIBILITY_MARKER)
        stage_list.append(BENCHMARK_MARKER)

        return stage_list

    def _build_stages(
        self,
        query_id: str,
        mandatory_constraints: str,
        general_pretext: str,
    ) -> List[StaticStageConfig]:
        """Return the ordered list of optimization stages for a single query."""
        sample_plan = self.sample_plan_dict[query_id]

        # load expert knowledge once - shared across all query optimization stages
        expert_knowledge = load_expert_knowledge(
            persistent_storage=self.persistent_storage
        )

        assert self.plan_source is not None, (
            "Plan source must be specified to build optimization stages."
        )

        return [
            StaticStageConfig(
                descriptor=f"Optim w. Sample Plan ({query_id})",
                # Stage 1: use the DuckDB sample plan for cardinality / optimizer hints.
                # The current runtime is not yet known, so `_rt` is ignored.
                get_prompt_with_tracing=lambda _exec_settings, _rt, _tracing_data: (
                    optim_prompt_w_sample_plan(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        query_plan=sample_plan,
                        current_rt_ms=_rt,
                        engine=self.plan_source,  # type: ignore
                        general_pretext=general_pretext,
                        model=self.model,
                        persistent_storage=self.persistent_storage,
                        tracing_data=_tracing_data,
                    )
                ),
            ),
            StaticStageConfig(
                descriptor=f"Optim w. Tracing Stats ({query_id})",
                # Stage 2: use tracing statistics; target 10x improvement.
                get_prompt_with_tracing=lambda _exec_settings, rt, tracing_data: (
                    optim_prompt_w_trace(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=rt,
                        storage_is_bespoke=self.bespoke_storage,
                        tracing_data=tracing_data,
                        general_pretext=general_pretext,
                        model=self.model,
                        persistent_storage=self.persistent_storage,
                    )
                ),
                max_turns=125,
            ),
            StaticStageConfig(
                descriptor=f"Optim w. Expert Knowledge ({query_id})",
                # Stage 3: apply domain-expert best practices; target 2x improvement.
                get_prompt=lambda _exec_settings, rt: optim_prompt_w_expert_knowledge(
                    query_id=query_id,
                    constraints_str=mandatory_constraints,
                    expert_knowledge=expert_knowledge,
                    current_rt_ms=rt,
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
                get_prompt_with_tracing=lambda _exec_settings, rt, tracing_data: (
                    optim_prompt_w_human_reference(
                        query_id=query_id,
                        constraints_str=mandatory_constraints,
                        current_rt_ms=rt,
                        storage_is_bespoke=self.bespoke_storage,
                        tracing_data=tracing_data,
                        general_pretext=general_pretext,
                        model=self.model,
                        num_turns=125,
                    )
                ),
                max_turns=125,
            ),
        ]

    async def run(self) -> Optional[List[str]]:
        # reset used prompts to empty and start from the beginning of the conversation
        self.used = []

        queries_path = self.file_paths["queries_path"]
        query_impl_path = self.file_paths["query_impl_path"]
        builder_path = self.file_paths["builder_path"]
        # describe the optimization problem
        pretext_optim = optim_prompt_pretext_optim(
            bespoke_storage=self.bespoke_storage,
            query_impl_path=query_impl_path,
            builder_path=builder_path,
            persistent_storage=self.persistent_storage,
        )

        # what the agent is allowed to change in the codebase to optimize performance
        mandatory_constraints = optim_prompt_constraints(
            allow_storage_changes=self.bespoke_storage,
            persistent_storage=self.persistent_storage,
        )

        # ensure initial implementation is correct
        correct, _, _ = self._check_correctness(self.query_ids, trace_mode=False)
        assert correct, (
            "Initial implementation does not produce correct results according to the validation tool. Please fix the implementation until it is correct before starting with optimization."
        )

        preoptim_stage_list = self.assemble_pre_optim_stages(
            optim_prompt_pretext(
                queries_path=queries_path,
                num_queries=len(self.query_ids),
                query_impl_path=query_impl_path,
                builder_path=builder_path,
            ),
        )
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info(preoptim_stage_list)

        await self._run_stages(preoptim_stage_list)

        # delete result.csv files before starting the optimization loop
        delete_result_csv_files(self.run_tool.cwd)

        # cleanup up supervision agent horizon - there are no clear outlined stages following
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info([])

        stage_end_msg, _, _ = await self._run_optimization_loop(
            mandatory_constraints=mandatory_constraints,
            pretext_optim=pretext_optim,
            start_stage_nr=len(preoptim_stage_list),
        )

        logger.info(f"Final validation metrics after optimization: {stage_end_msg}")

        # signal this is the end of the conversation - save the used prompts
        used = await self.ask_to_finish_and_save()

        return used
