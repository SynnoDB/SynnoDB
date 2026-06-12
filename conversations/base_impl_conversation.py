import functools
import logging
from pathlib import Path
from typing import Callable, Optional

from conversations.checkpointed_conversation import (
    CheckpointedConversation,
    extract_speedup_of_last_snapshot,
)
from conversations.conversation import (
    BENCHMARK_MARKER,
    COMPACTION_MARKER,
    VALIDATE_OFF,
    VALIDATE_ON,
    VALIDATE_OUTPUT_STDOUT_OFF,
)
from conversations.filenames import get_filenames
from conversations.prompts_gen import (
    base_check_correctness_all_prompt,
    base_exec_validate_for_query_prompt,
    base_exec_validate_prompt,
    base_fix_slow_queries_prompt,
    base_impl_query_prompt,
    base_impl_storage,
    base_optimize_build_prompt,
    base_optimize_build_simple,
    base_planner_prompt,
    base_run_all_and_fix_prompt,
)
from conversations.stage_config import (
    DynamicStageConfig,
    StageConfig,
    StaticStageConfig,
)
from conversations.supervision_agent import SUPERVISION_STAGE_VISIBILITY_MARKER
from tools.run_tool_mode import RunToolMode
from utils.utils import DBStorage

logger = logging.getLogger(__name__)


class BaseImplConversation(CheckpointedConversation):
    def __init__(
        self,
        verify_sf_list: list[float],
        max_scale_factor: int | float,
        benchmark: str,
        workspace_path: Path,
        sql_dict: dict[str, str],
        db_storage: DBStorage,
        read_storage_plan: bool = False,
        sample_query_args_dict: Optional[dict[str, str]] = None,
        use_master_prompt: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.verify_sf_list = verify_sf_list
        self.max_scale_factor = max_scale_factor
        self.benchmark = benchmark
        self.read_storage_plan = read_storage_plan
        self.sample_query_args_dict = sample_query_args_dict
        self.workspace_path = workspace_path
        self.use_master_prompt = use_master_prompt
        self.sql_dict = sql_dict
        self.persistent_storage = db_storage in [DBStorage.LABSTORE, DBStorage.SSD]

    def _validate_no_crazy_slow_queries(
        self, sf: float, query_ids: list[str], query_impl_path: str, builder_path: str
    ) -> str | None:
        """Check all queries for critical slowness (speedup < 0.2x vs DuckDB).

        Returns a fix prompt if any slow queries are found, None otherwise.
        """
        slow_queries = []
        for qid in query_ids:
            _, metrics, _ = self.run_tool.run(
                mode=RunToolMode.EXHAUSTIVE,
                optimize=True,
                query_ids=[qid],
                trace_mode=False,
                external_call=True,
            )
            if metrics is None:
                continue
            try:
                impl_rt_s, duckdb_rt_s, speedup = extract_speedup_of_last_snapshot(
                    metrics, qid, sf
                )
                if speedup < 0.2:
                    slow_queries.append(
                        (qid, impl_rt_s * 1000, duckdb_rt_s * 1000, speedup)
                    )
            except (AssertionError, KeyError) as e:
                logger.warning(f"Could not extract speedup for query {qid}: {e}")

        if not slow_queries:
            return None

        logger.warning(
            f"Found {len(slow_queries)} critically slow queries: "
            + ", ".join(
                f"{qid} ({speedup:.3f}x)" for qid, _, _, speedup in slow_queries
            )
        )
        return base_fix_slow_queries_prompt(
            slow_queries,
            query_impl_path=query_impl_path,
            builder_path=builder_path,
        )

    async def run(self):
        # reset used prompts to empty and start from the beginning of the conversation
        self.used = []

        # fetch stages
        stage_list = self.assemble_stages()

        # register planned steps
        if self.supervision_agent is not None:
            self.supervision_agent.register_workload_info(stage_list)

        if self.use_master_prompt:
            prompt_prefix = "# MASTER PROMPT:\n You are an autonomous agent - solve your task without asking questions! Do everything that is necessary to solve your task (including invoking Shell, Apply-Patch, Compile and Run Tools). You will not get external feedback. Solve your task.\n\nYour task:\n"
        else:
            prompt_prefix = None

        await self._run_stages(stage_list, prompt_pretext=prompt_prefix)

    def assemble_stages(self) -> list[StageConfig | str]:

        def _base_impl_descriptor(qid: str) -> str:
            return f"base impl Q{qid}"

        def _base_impl_prompt(_sf, _rt, *, idx: int, qid: str, sql: str):
            return base_impl_query_prompt(
                is_first_query=(idx == 0),
                query_id=qid,
                sample_query_args_dict=self.sample_query_args_dict,
                queries_path=queries_path,
                args_path=args_path,
                builder_path=builder_path,
                query_impl_path=query_impl_path,
                sql=sql,
                persistent_storage=self.persistent_storage,
            )

        def _exec_validate_prompt(_sf, _rt, *, qid: str):
            return base_exec_validate_for_query_prompt(
                query_id=qid,
                sf_verify_str=sf_verify_str,
                builder_path=builder_path,
                sql=self.sql_dict[f"Q{qid}"],
                show_ssd_error_hints=self.persistent_storage,
            )

        # ==========
        # Paths & Co
        # ==========

        # assemble sf verify string
        if len(self.verify_sf_list) == 1:
            sf_verify_str = str(self.verify_sf_list[0])
        elif len(self.verify_sf_list) == 2:
            sf_verify_str = f"{self.verify_sf_list[0]} and {self.verify_sf_list[1]}"
        else:
            sf_verify_str = (
                ", ".join(map(str, self.verify_sf_list[:-1]))
                + f", and {self.verify_sf_list[-1]}"
            )

        if self.benchmark == "tpch":
            example_query = "Q42"
            example_query_params = "42"
        elif self.benchmark == "ceb":
            example_query = "Q42a"
            example_query_params = "42a"
        else:
            raise ValueError(f"Unknown benchmark {self.benchmark}")

        # paths
        filenames_dict = get_filenames()
        queries_path = filenames_dict["queries_path"]
        builder_path = filenames_dict["builder_path"]
        builder_cpp_path = filenames_dict["builder_cpp_path"]
        builder_hpp_path = filenames_dict["builder_hpp_path"]
        query_impl_path = filenames_dict["query_impl_path"]
        args_path = filenames_dict["args_path"]
        base_impl_todo_filename = filenames_dict["base_impl_todo_filename"]
        storage_plan_filename = filenames_dict["storage_plan_filename"]

        def _validate_plan_exists() -> str | None:
            plan_path = self.workspace_path / base_impl_todo_filename
            if plan_path.exists():
                return None  # plan exists, proceed to next stage
            else:
                # return prompt to llm to fix this.
                logger.error(
                    f"Todo file {plan_path} does not exist. Reprompting the LLM now"
                )
                return f"Your task was to create an implementation plan. However, no implementation plan called `{base_impl_todo_filename}` exists in your workspace. Please create a plan and write it to `{base_impl_todo_filename}` before proceeding to the implementation stage."

        # =========
        # Stage Defs
        # =========

        stage_list = [
            VALIDATE_OFF,
            StaticStageConfig(
                descriptor="base impl planner",
                get_prompt=lambda _sf, _rt: base_planner_prompt(
                    queries_path=queries_path,
                    num_queries=len(self.all_query_ids),
                    builder_path=builder_path,
                    read_storage_plan=self.read_storage_plan,
                    storage_plan_path=storage_plan_filename,
                    query_impl_path=query_impl_path,
                    example_query=example_query,
                    example_query_params=example_query_params,
                    args_path=args_path,
                    parquet_path=parquet_path.as_posix(),
                    base_impl_todo_file=base_impl_todo_filename,
                    persistent_storage=self.persistent_storage,
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                # if a plan exists, validate that it is correct before proceeding to impl stage. If it is not correct, go back to planner stage
                post_stage_validate=_validate_plan_exists,
            ),
            StaticStageConfig(
                descriptor="base impl storage",
                get_prompt=lambda _sf, _rt: base_impl_storage(
                    builder_path=builder_path,
                    query_impl_path=query_impl_path,
                    base_impl_todo_file=base_impl_todo_filename,
                    args_path=args_path,
                    persistent_storage=self.persistent_storage,
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
            ),
            COMPACTION_MARKER,
            OptimizeBuildStage(
                builder_path=builder_path,
                sf_list=self.verify_sf_list + [self.max_scale_factor],
                get_current_benchmark_result_or_rerun_callback=self.get_current_benchmark_result_or_rerun,
                persistent_storage=self.persistent_storage,
            ),
            ValidateAndFixStage(
                sf_list=self.verify_sf_list + [self.max_scale_factor],
                get_current_benchmark_result_or_rerun_callback=self.get_current_benchmark_result_or_rerun,
                query_impl_path=query_impl_path,
                args_path=args_path,
                builder_path=builder_path,
                persistent_storage=self.persistent_storage,
            ),
            COMPACTION_MARKER,
            VALIDATE_ON,
            VALIDATE_OUTPUT_STDOUT_OFF,
        ]

        for i, query_id in enumerate(self.all_query_ids):

            def _validate_query_impl_exists(qid) -> str | None:
                impl_path = self.workspace_path / f"query{qid}.cpp"
                if impl_path.exists():
                    return None  # impl exists, proceed to next stage
                else:
                    # return prompt to llm to fix this.
                    logger.error(
                        f"Query implementation file {impl_path} does not exist. Reprompting the LLM now"
                    )
                    return f"You were supposed to implement query {qid} in a file called `query{qid}.cpp`. However, no such file exists in your workspace. Please implement the query and write it to `query{qid}.cpp` before proceeding to the execution & validation stage."

            stage_list.extend(
                [
                    StaticStageConfig(
                        descriptor=f"base impl Q{query_id}",
                        get_prompt=functools.partial(
                            _base_impl_prompt,
                            idx=i,
                            qid=query_id,
                            sql=self.sql_dict[f"Q{query_id}"],
                        ),
                        measure_performance_after_stage=False,
                        auto_revert_on_regression=False,
                        post_stage_validate=functools.partial(
                            _validate_query_impl_exists, qid=query_id
                        ),
                    ),
                    StaticStageConfig(
                        descriptor=f"base impl exec & validate Q{query_id}",
                        get_prompt=functools.partial(
                            _exec_validate_prompt,
                            qid=query_id,
                        ),
                        measure_performance_after_stage=True,
                        measure_perf_qid=query_id,
                        auto_revert_on_regression=False,
                        feedback_on_incorrect=True,  # go into retry loop if query is incorrect
                        post_stage_validate=functools.partial(
                            self._validate_no_crazy_slow_queries,
                            sf=self.verify_sf_list[-1],
                            query_ids=[query_id],
                            query_impl_path=query_impl_path,
                            builder_path=builder_path,
                        ),
                    ),
                    COMPACTION_MARKER,
                ]
            )

        stage_list.append(SUPERVISION_STAGE_VISIBILITY_MARKER)

        # post impl stages.
        stage_list.extend(
            [
                StaticStageConfig(
                    descriptor="base check correctness all",
                    get_prompt=lambda _sf, _rt: base_check_correctness_all_prompt(
                        sf_verify_str=sf_verify_str,
                    ),
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                    post_stage_validate=functools.partial(
                        self._validate_no_crazy_slow_queries,
                        sf=self.verify_sf_list[-1],
                        query_ids=self.all_query_ids,
                        query_impl_path=query_impl_path,
                        builder_path=builder_path,
                    ),
                ),
                StaticStageConfig(
                    descriptor="run all queries and fix any errors",
                    get_prompt=lambda _sf, _rt: base_run_all_and_fix_prompt(
                        max_scale_factor=self.max_scale_factor,
                        query_impl_path=query_impl_path,
                    ),
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                    post_stage_validate=functools.partial(
                        self._validate_no_crazy_slow_queries,
                        sf=self.benchmark_sf,
                        query_ids=self.all_query_ids,
                        query_impl_path=query_impl_path,
                        builder_path=builder_path,
                    ),
                ),
                BENCHMARK_MARKER,
                # VALIDATE_MAX_SF_REP1_ON,  # only single repetition with largest (benchmarking) scale factor - we don't care if measured query rt is noisy
                VALIDATE_OUTPUT_STDOUT_OFF,
                # VALIDATE_OUTPUT_STDOUT_MAXSF_ON,  # include stdout and stderr for largest (benchmarking) scale factor to have more info in case of regressions
                StaticStageConfig(
                    descriptor="optimize build",
                    get_prompt=lambda _sf, _rt: base_optimize_build_prompt(
                        sf_verify_str=sf_verify_str,
                        max_scale_factor=self.max_scale_factor,
                        builder_path_cpp=builder_cpp_path,
                        builder_path_hpp=builder_hpp_path,
                        persistent_storage=self.persistent_storage,
                    ),
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                ),
                # VALIDATE_OUTPUT_STDOUT_MAXSF_OFF,
                # VALIDATE_MAX_SF_REP1_OFF,
                BENCHMARK_MARKER,
            ]
        )

        return stage_list


class OptimizeBuildStage(DynamicStageConfig):
    def __init__(
        self,
        builder_path: str,
        sf_list: list[float],
        get_current_benchmark_result_or_rerun_callback: Callable,
        persistent_storage: bool,
    ):
        super().__init__(descriptor="optimize build", max_turns=None)
        self.builder_path = builder_path
        self.sf_list = sf_list
        self.get_current_benchmark_result_or_rerun_callback = (
            get_current_benchmark_result_or_rerun_callback
        )
        self.executed = False
        self.persistent_storage = persistent_storage

    def next_prompt(self) -> Optional[str]:
        # run only once
        if self.executed:
            return None
        self.executed = True

        # fetch last validate results or rerun benchmark if not available
        last_validate = self.get_current_benchmark_result_or_rerun_callback(
            self.sf_list[-1],
            False,
            None,
        )

        # extract ingest time
        assert "validation/ingest_time_ms" in last_validate, (
            f"Ingest time not found in benchmark results: {last_validate}"
        )
        ingest_time_ms = last_validate["validation/ingest_time_ms"]

        # generate prompt
        prompt = base_optimize_build_simple(
            builder_path=self.builder_path,
            sf_list=sorted(list(set(self.sf_list))),
            current_ingest_time_ms=ingest_time_ms,
            current_ingest_sf=self.sf_list[-1],
            persistent_storage=self.persistent_storage,
        )
        return prompt


class ValidateAndFixStage(DynamicStageConfig):
    def __init__(
        self,
        sf_list: list[float],
        get_current_benchmark_result_or_rerun_callback: Callable,
        query_impl_path: str,
        args_path: str,
        builder_path: str,
        persistent_storage: bool,
    ):
        super().__init__(descriptor="exec & validate", max_turns=None)
        self.executed = False
        self.sf_list = sf_list
        self.get_current_benchmark_result_or_rerun_callback = (
            get_current_benchmark_result_or_rerun_callback
        )
        self.query_impl_path = query_impl_path
        self.args_path = args_path
        self.builder_path = builder_path
        self.persistent_storage = persistent_storage

    def next_prompt(self) -> Optional[str]:
        # run only once
        if self.executed:
            return None
        self.executed = True

        # check if for all scale factors the validation is correct
        all_correct = True
        for sf in self.sf_list:
            last_validate = self.get_current_benchmark_result_or_rerun_callback(
                sf,
                False,
                None,
            )

            if not last_validate or not last_validate.get("validation/correct", False):
                all_correct = False
                break

        if all_correct:
            logger.info("All validations passed, no need to fix.")
            return None

        prompt = base_exec_validate_prompt(
            sf_verify_str=", ".join(map(str, self.sf_list)),
            max_scale_factor=self.sf_list[-1],
            query_impl_path=self.query_impl_path,
            args_path=self.args_path,
            show_ssd_error_hints=self.persistent_storage,
            builder_path=self.builder_path,
        )
        return prompt
