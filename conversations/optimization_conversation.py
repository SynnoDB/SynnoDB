import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from conversations.checkpointed_conversation import (
    CheckpointedConversation,
    extract_speedup_of_last_snapshot,
)
from conversations.conversation import (
    COMPACTION_MARKER,
)
from conversations.filenames import get_filenames
from conversations.stage_config import StaticStageConfig
from conversations.utils.cleanup_plans import (
    cleanup_duckdb_plan,
    cleanup_umbra_plan,
)
from tools.run import delete_result_csv_files
from tools.run_tool_mode import RunToolMode
from tools.validate.query_validator_class import QueryValidator
from utils.utils import DBStorage
from workloads.system_factory import System

logger = logging.getLogger(__name__)

# Number of queries to instrument with timing in one LLM interaction.
QUERIES_PER_TIMING_BATCH = 3


@dataclass
class OptimConvArgs:
    query_ids: List[str]
    query_validator: QueryValidator
    model: str
    db_storage: DBStorage
    bespoke_storage: bool = True
    plan_source: Optional[str] = (
        "umbra"  # "umbra" or "duckdb" - determines where the initial sample plans are sourced from; this only affects the first optimization stage and does not impact the overall conversation structure
    )
    cleanup_plans: bool = True  # whether to apply plan cleanup to the sample plans before showing them to the LLM


class OptimizationConversation(CheckpointedConversation):
    def __init__(
        self,
        optim_conv_args: OptimConvArgs,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.query_ids = optim_conv_args.query_ids
        self.bespoke_storage = optim_conv_args.bespoke_storage
        self.model = optim_conv_args.model
        self.query_validator = optim_conv_args.query_validator
        self.persistent_storage = optim_conv_args.db_storage in [
            DBStorage.LABSTORE,
            DBStorage.SSD,
        ]
        self.cleanup_plans = optim_conv_args.cleanup_plans

        self.file_paths = get_filenames()
        self.plan_source = optim_conv_args.plan_source

        assert not self.replay, (
            "Replay mode is not supported for OptimizationConversation. Use replay_cache if you want to replay from cache without user interaction."
        )

        # retrieve reference sample plans / runtimes for the queries from the
        # workload provider (exec-config source) + query-execution cache.
        self.sample_plan_dict: Dict[str, str | dict] = dict()
        self.reference_rt_ms: Dict[str, float] = dict()
        self._init_reference_plans_and_runtimes()

    def _init_reference_plans_and_runtimes(self) -> None:
        """Populate ``sample_plan_dict`` / ``reference_rt_ms`` from the reference engine.

        The workload provider is the single source of exec-configs: we produce a
        BENCHMARK-mode batch (at the provider's ``benchmark_sf``) and resolve it
        against the query-execution cache, which returns the reference engine's
        query plan and runtime for each query (executing + caching on a miss).
        """
        if self.plan_source is None:
            return

        if self.plan_source == "umbra":
            system = System.UMBRA
            cleanup_fn = cleanup_umbra_plan
        elif self.plan_source == "duckdb":
            system = System.DUCKDB
            cleanup_fn = cleanup_duckdb_plan
        else:
            raise ValueError(
                f"Unknown plan source {self.plan_source} for sample plans."
            )

        # one BENCHMARK batch at the provider's benchmark scale factor
        batches = self._olap_provider.produce_workload(
            run_mode=RunToolMode.BENCHMARK,
            query_ids=self.query_ids,
            num_threads=1,
            core_ids=None,
        )
        assert len(batches) == 1, (
            f"BENCHMARK mode should emit exactly one batch, got {len(batches)}"
        )

        results = self.query_validator.query_execution_cache.lookup_or_execute_query_batch(
            batches[0], system
        )

        # take the first result per query (BENCHMARK repeats the same query)
        for res in results:
            query_id = res.query_entry.query_id
            if query_id in self.sample_plan_dict:
                continue
            plan = res.plan
            assert plan is not None, (
                f"Reference engine {system} returned no plan for query {query_id}."
            )
            self.sample_plan_dict[query_id] = (
                cleanup_fn(plan) if self.cleanup_plans else plan
            )
            self.reference_rt_ms[query_id] = res.exec_time_ms

    def _build_stages(
        self, query_id: str, mandatory_constraints: str, general_pretext: str
    ) -> List[StaticStageConfig]:
        raise NotImplementedError(
            "Subclasses must implement _build_stages to define the stages of the optimization conversation."
        )

    async def _run_optimization_loop(
        self,
        mandatory_constraints: str,
        pretext_optim: str,
        start_stage_nr: int = 0,
    ):
        """Core per-query optimization loop shared across optimization rounds.

        Builds stages, creates per-query conversation branches, and iterates
        stage-by-stage across all queries.  Returns ``(stage_end_msg, stage_end_metrics)``
        from the final full-benchmark run.
        """
        per_query_stages: Dict[str, List[StaticStageConfig]] = {}
        per_query_branch: Dict[str, str] = {}

        last_turn_nr = await self.agent_sdk_wrapper.get_conversation_turns()

        logger.debug(
            f"Collect statistics of initial implementation for all queries ({len(self.query_ids)})."
        )
        for query_id in self.query_ids:
            per_query_stages[query_id] = self._build_stages(
                query_id, mandatory_constraints, general_pretext=pretext_optim
            )

            # Switch back to main branch
            await self.agent_sdk_wrapper.switch_to_conversation_branch("main")

            # create conversation branches for each query
            try:
                per_query_branch[
                    query_id
                ] = await self.agent_sdk_wrapper.create_conversation_branch_from_turn(
                    turn_nr=last_turn_nr, branch_name=f"query_{query_id}_{last_turn_nr}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to create conversation branch for query {query_id} from turn {last_turn_nr}: {e}"
                )
                logger.error(await self.agent_sdk_wrapper.get_conversation_turns())
                raise e

        logger.debug(f"Branches created for all queries: {per_query_branch}")

        num_stages = len(per_query_stages[self.query_ids[0]])

        # clear query rt log
        self.query_rt_log: Dict[str, float] = dict()

        for stage_id in range(num_stages):
            for query_idx, query_id in enumerate(self.query_ids):
                # switch to the conversation branch for this query
                await self.agent_sdk_wrapper.switch_to_conversation_branch(
                    per_query_branch[query_id]
                )

                stage = per_query_stages[query_id][stage_id]
                current_stage_nr = (
                    start_stage_nr + stage_id * len(self.query_ids) + query_idx
                )

                # collect initial runtime for this query before starting with the first stage of optimization
                _, metrics, _ = self.run_tool.run(
                    mode=RunToolMode.BENCHMARK, query_ids=[query_id], optimize=True
                )
                assert metrics is not None

                try:
                    impl_rt_s, _, _ = extract_speedup_of_last_snapshot(
                        statistics=metrics,
                        query=query_id,
                    )
                    self.query_rt_log[query_id] = impl_rt_s
                except AssertionError as e:
                    logger.warning(
                        f"Failed to extract speedup for query {query_id}: {e}"
                    )
                    # lookup runtime from a past run
                    impl_rt_s = self.query_rt_log[query_id]

                if stage.get_prompt_with_tracing is not None:
                    # collect tracing data
                    _, _, tracing_output = self.run_tool.run(
                        mode=RunToolMode.BENCHMARK,
                        query_ids=[query_id],
                        trace_mode=True,
                        optimize=True,
                    )  # collect fresh tracing stats
                    if tracing_output is None:
                        logger.warning(
                            f"Trace-mode run for query {query_id} produced no output (likely crashed). Using placeholder."
                        )
                        tracing_output = (
                            "(Tracing data unavailable -- the trace-mode run crashed.)"
                        )
                else:
                    tracing_output = None

                # run the stage - includes automatic reverts if regressions are detected
                await self._run_stage_with_revert_monitoring(
                    query_id=query_id,
                    stage_config=stage,
                    prompt_pretext=None,
                    rt_before_s=impl_rt_s,
                    tracing_data=tracing_output,
                    current_stage_nr=current_stage_nr,
                )

                # delete result.csv files
                delete_result_csv_files(self.run_tool.cwd)

                await self._exec(
                    COMPACTION_MARKER, "compaction", current_stage_nr=current_stage_nr
                )

            # perform full benchmarking across all queries at the end of the stage
            delete_result_csv_files(self.run_tool.cwd)
            stage_end_msg, stage_end_metrics, stage_end_tracing_output = (
                self.run_tool.run(
                    mode=RunToolMode.BENCHMARK, query_ids=None, optimize=True
                )
            )

        return stage_end_msg, stage_end_metrics, stage_end_tracing_output
