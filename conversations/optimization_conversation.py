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
from tools.validate.query_validator_class import QueryValidator
from utils.utils import DBStorage

logger = logging.getLogger(__name__)

# Number of queries to instrument with timing in one LLM interaction.
QUERIES_PER_TIMING_BATCH = 3


@dataclass
class OptimConvArgs:
    query_ids: List[str]
    verify_sf_list: List[float]
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
        self.verify_sf_list = optim_conv_args.verify_sf_list
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

        # retrieve sample plans / instantiations for the queries
        self.sample_plan_dict = dict()
        for query_id in self.query_ids:
            instantiations, _ = self.query_validator._get_instantiations(
                scale_factor=self.benchmark_sf,
                query_id=[query_id],
                trace_mode=False,
            )

            if self.plan_source is None:
                pass
            elif self.plan_source == "umbra":
                plan = instantiations[0].umbra_plan
                assert plan is not None
                self.sample_plan_dict[query_id] = (
                    cleanup_umbra_plan(plan) if self.cleanup_plans else plan
                )
            elif self.plan_source == "duckdb":
                plan = instantiations[0].duckdb_plan
                self.sample_plan_dict[query_id] = (
                    cleanup_duckdb_plan(plan) if self.cleanup_plans else plan
                )
            else:
                raise ValueError(
                    f"Unknown plan source {self.plan_source} for sample plans."
                )

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

                # stage.sf overrides the default benchmark_sf for reference runtime collection
                stage_sf = stage.sf if stage.sf is not None else self.benchmark_sf

                # collect initial runtime for this query before starting with the first stage of optimization
                _, metrics, _ = self.run_tool.run(
                    scale_factor=stage_sf, query_id=[query_id], optimize=True
                )
                assert metrics is not None

                try:
                    impl_rt_s, _, _ = extract_speedup_of_last_snapshot(
                        statistics=metrics,
                        query=query_id,
                        current_reference_scalefactor=stage_sf,
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
                        scale_factor=stage_sf,
                        query_id=[query_id],
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
                    scale_factor=self.benchmark_sf, query_id=None, optimize=True
                )
            )

        return stage_end_msg, stage_end_metrics, stage_end_tracing_output
