"""Stage list of the createBaseImpl conversation, plus its dynamic stages
(OptimizeBuildStage / ValidateAndFixStage)."""

import functools
import logging
from typing import Optional

from synnodb.conversations.conversation_engine import (
    extract_speedup_of_last_snapshot,
)
from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.prompts_gen import (
    base_check_correctness_all_prompt,
    base_exec_validate_for_query_prompt,
    base_exec_validate_prompt,
    base_fix_slow_queries_prompt,
    base_impl_query_prompt,
    base_impl_storage,
    base_optimize_build,
    base_planner_prompt,
    base_run_all_and_fix_prompt,
)
from synnodb.conversations.stage_items import (
    Benchmark,
    Compact,
    DynamicStageConfig,
    PromptStage,
    StageItem,
    SupervisionHorizon,
    ValidateOff,
    ValidateOn,
    ValidateStdoutOff,
)
from synnodb.tools.run import RunTool
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_spec import get_workload_spec

logger = logging.getLogger(__name__)


def _validate_no_crazy_slow_queries(
    ctx: ConvContext, query_ids: list[str], query_impl_path: str, builder_path: str
) -> str | None:
    """Check all queries for critical slowness (speedup < 0.2x vs DuckDB).

    Returns a fix prompt if any slow queries are found, None otherwise.
    """
    slow_queries = []
    for qid in query_ids:
        _, metrics, _ = ctx.run_tool.run(
            mode=RunToolMode.BENCHMARK,
            optimize=True,
            query_ids=[qid],
            trace_mode=False,
            external_call=True,
        )
        if metrics is None:
            continue
        try:
            impl_rt_s, duckdb_rt_s, speedup = extract_speedup_of_last_snapshot(
                metrics, qid
            )
            if speedup < 0.2:
                assert duckdb_rt_s is not None, "DuckDB runtime is None"
                slow_queries.append(
                    (qid, impl_rt_s * 1000, duckdb_rt_s * 1000, speedup)
                )
        except (AssertionError, KeyError) as e:
            logger.warning(f"Could not extract speedup for query {qid}: {e}")

    if not slow_queries:
        return None

    logger.warning(
        f"Found {len(slow_queries)} critically slow queries: "
        + ", ".join(f"{qid} ({speedup:.3f}x)" for qid, _, _, speedup in slow_queries)
    )
    return base_fix_slow_queries_prompt(
        slow_queries,
        query_impl_path=query_impl_path,
        builder_path=builder_path,
    )


def build(ctx: ConvContext) -> list[StageItem]:
    sample_query_args_dict = ctx.sample_query_args()

    def _base_impl_prompt(_exec_settings, _rt, *, idx: int, qid: str, sql: str):
        return base_impl_query_prompt(
            is_first_query=(idx == 0),
            query_id=qid,
            sample_query_args_dict=sample_query_args_dict,
            queries_path=queries_path,
            args_path=args_path,
            builder_path=builder_path,
            query_impl_path=query_impl_path,
            sql=sql,
            persistent_storage=ctx.persistent_storage,
            num_threads=ctx.threads,
        )

    def _exec_validate_prompt(_exec_settings, _rt, *, qid: str):
        return base_exec_validate_for_query_prompt(
            query_id=qid,
            run_tool_mode=RunToolMode.EXHAUSTIVE,
            builder_path=builder_path,
            sql=ctx.sql_dict[f"Q{qid}"],
            show_ssd_error_hints=ctx.persistent_storage,
        )

    # ==========
    # Paths & Co
    # ==========

    # Planner-prompt parameterization comes from the workload spec, not a per-
    # benchmark branch (so a new workload supplies its own example + schema table).
    spec = get_workload_spec(ctx.workload.value)
    example_query = spec.example_query
    example_query_params = spec.example_query_params
    schema_example_table = spec.schema_example_table

    # paths
    queries_path = ctx.filenames.queries_path
    builder_path = ctx.filenames.builder_path
    builder_cpp_path = ctx.filenames.builder_cpp_path
    builder_hpp_path = ctx.filenames.builder_hpp_path
    query_impl_path = ctx.filenames.query_impl_path
    args_path = ctx.filenames.args_path
    base_impl_todo_filename = ctx.filenames.base_impl_todo_filename
    storage_plan_filename = ctx.filenames.storage_plan_filename

    def _validate_plan_exists() -> str | None:
        plan_path = ctx.workspace_path / base_impl_todo_filename
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

    stage_list: list[StageItem] = [
        ValidateOff(),
        PromptStage(
            descriptor="base impl planner",
            get_prompt=lambda _exec_settings, _rt: base_planner_prompt(
                queries_path=queries_path,
                num_queries=len(ctx.query_ids),
                builder_path=builder_path,
                read_storage_plan=ctx.bespoke_storage,
                storage_plan_path=storage_plan_filename,
                query_impl_path=query_impl_path,
                example_query=example_query,
                example_query_params=example_query_params,
                args_path=args_path,
                parquet_path=ctx.sample_exec_settings().parquet_dir.as_posix(),
                base_impl_todo_file=base_impl_todo_filename,
                persistent_storage=ctx.persistent_storage,
                schema_example_table=schema_example_table,
            ),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            # if a plan exists, validate that it is correct before proceeding to impl stage. If it is not correct, go back to planner stage
            post_stage_validate=_validate_plan_exists,
            max_turns=ctx.max_turns,
        ),
        PromptStage(
            descriptor="base impl storage",
            get_prompt=lambda _exec_settings, _rt: base_impl_storage(
                builder_path=builder_path,
                query_impl_path=query_impl_path,
                base_impl_todo_file=base_impl_todo_filename,
                args_path=args_path,
                persistent_storage=ctx.persistent_storage,
            ),
            measure_performance_after_stage=False,
            auto_revert_on_regression=False,
            max_turns=ctx.max_turns,
        ),
        Compact(),
        OptimizeBuildStage(
            builder_path_cpp=builder_cpp_path,
            builder_path_hpp=builder_hpp_path,
            run_tool=ctx.run_tool,
            persistent_storage=ctx.persistent_storage,
            allow_storage_restructuring=True,
            storage_plan_filename=storage_plan_filename,
            base_impl_todo_filename=base_impl_todo_filename,
            num_threads=ctx.threads,
            max_turns=ctx.max_turns,
        ),
        ValidateAndFixStage(
            run_tool=ctx.run_tool,
            query_impl_path=query_impl_path,
            args_path=args_path,
            builder_path=builder_path,
            persistent_storage=ctx.persistent_storage,
            max_turns=ctx.max_turns,
        ),
        Compact(),
        ValidateOn(),
        ValidateStdoutOff(),
    ]

    for i, query_id in enumerate(ctx.query_ids):

        def _validate_query_impl_exists(qid) -> str | None:
            impl_path = ctx.workspace_path / f"query{qid}.cpp"
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
                PromptStage(
                    descriptor=f"base impl Q{query_id}",
                    get_prompt=functools.partial(
                        _base_impl_prompt,
                        idx=i,
                        qid=query_id,
                        sql=ctx.sql_dict[f"Q{query_id}"],
                    ),
                    measure_performance_after_stage=False,
                    auto_revert_on_regression=False,
                    post_stage_validate=functools.partial(
                        _validate_query_impl_exists, qid=query_id
                    ),
                    max_turns=ctx.max_turns,
                ),
                PromptStage(
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
                        _validate_no_crazy_slow_queries,
                        ctx,
                        query_ids=[query_id],
                        query_impl_path=query_impl_path,
                        builder_path=builder_path,
                    ),
                    max_turns=ctx.max_turns,
                ),
                Compact(),
            ]
        )

    stage_list.append(SupervisionHorizon())

    # post impl stages.
    stage_list.extend(
        [
            PromptStage(
                descriptor="base check correctness all",
                get_prompt=lambda _exec_settings, _rt: (
                    base_check_correctness_all_prompt(
                        run_tool_mode=RunToolMode.EXHAUSTIVE,
                        query_ids=ctx.query_ids,
                    )
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                post_stage_validate=functools.partial(
                    _validate_no_crazy_slow_queries,
                    ctx,
                    query_ids=ctx.query_ids,
                    query_impl_path=query_impl_path,
                    builder_path=builder_path,
                ),
                max_turns=ctx.max_turns,
            ),
            PromptStage(
                descriptor="run all queries and fix any errors",
                get_prompt=lambda _exec_settings, _rt: base_run_all_and_fix_prompt(
                    run_tool_mode=RunToolMode.EXHAUSTIVE,
                    query_impl_path=query_impl_path,
                    query_ids=ctx.query_ids,
                ),
                measure_performance_after_stage=False,
                auto_revert_on_regression=False,
                post_stage_validate=functools.partial(
                    _validate_no_crazy_slow_queries,
                    ctx,
                    query_ids=ctx.query_ids,
                    query_impl_path=query_impl_path,
                    builder_path=builder_path,
                ),
                max_turns=ctx.max_turns,
            ),
            Benchmark(),
            # VALIDATE_MAX_SF_REP1_ON,  # only single repetition with largest (benchmarking) scale factor - we don't care if measured query rt is noisy
            ValidateStdoutOff(),
            # VALIDATE_OUTPUT_STDOUT_MAXSF_ON,  # include stdout and stderr for largest (benchmarking) scale factor to have more info in case of regressions
            OptimizeBuildStage(
                builder_path_cpp=builder_cpp_path,
                builder_path_hpp=builder_hpp_path,
                run_tool=ctx.run_tool,
                persistent_storage=ctx.persistent_storage,
                allow_storage_restructuring=False,
                storage_plan_filename=storage_plan_filename,
                base_impl_todo_filename=base_impl_todo_filename,
                num_threads=ctx.threads,
                max_turns=ctx.max_turns,
            ),
            # VALIDATE_OUTPUT_STDOUT_MAXSF_OFF,
            # VALIDATE_MAX_SF_REP1_OFF,
            Benchmark(),
        ]
    )

    return stage_list


class OptimizeBuildStage(DynamicStageConfig):
    def __init__(
        self,
        builder_path_cpp: str,
        builder_path_hpp: str,
        run_tool: RunTool,
        persistent_storage: bool,
        allow_storage_restructuring: bool,
        storage_plan_filename: str,
        base_impl_todo_filename: str,
        num_threads: int,
        max_turns: int | None = None,
    ):
        super().__init__(descriptor="optimize build", max_turns=max_turns)
        self.builder_path_cpp = builder_path_cpp
        self.builder_path_hpp = builder_path_hpp
        self.run_tool = run_tool
        self.executed = False
        self.persistent_storage = persistent_storage
        self.allow_storage_restructuring = allow_storage_restructuring
        self.storage_plan_filename = storage_plan_filename
        self.base_impl_todo_filename = base_impl_todo_filename
        self.num_threads = num_threads

    def next_prompt(self) -> Optional[str]:
        if self.executed:
            return None
        self.executed = True

        run_result = self.run_tool.run_worker(
            mode=RunToolMode.INGEST,
            optimize=True,
            query_ids=None,
            trace_mode=False,
            external_call=True,
        )

        assert run_result.ingest_time_ms is not None, (
            "Ingest time must be available for optimize build stage"
        )
        assert run_result.query_batch is not None, (
            "Query batch must be available for optimize build stage"
        )

        return base_optimize_build(
            builder_path_cpp=self.builder_path_cpp,
            builder_path_hpp=self.builder_path_hpp,
            current_ingest_time_ms=run_result.ingest_time_ms,
            current_exec_config=run_result.query_batch.exec_settings,
            persistent_storage=self.persistent_storage,
            allow_storage_restructuring=self.allow_storage_restructuring,
            storage_plan_filename=self.storage_plan_filename,
            base_impl_todo_filename=self.base_impl_todo_filename,
            # Ground the optimizer in the engine's actual operating envelope, not the raw
            # host: the serving parallelism it is built/validated for and the memory ceiling
            # the build runs under. Advertising the whole machine invites oversubscription and
            # memory-for-speed trades that overrun the cgroup budget and abort the build.
            serving_threads=self.num_threads,
            memory_budget_mb=self.run_tool.memory_budget_mb,
        )


class ValidateAndFixStage(DynamicStageConfig):
    def __init__(
        self,
        run_tool: RunTool,
        query_impl_path: str,
        args_path: str,
        builder_path: str,
        persistent_storage: bool,
        max_turns: int | None = None,
    ):
        super().__init__(descriptor="exec & validate", max_turns=max_turns)
        self.executed = False
        self.run_tool = run_tool
        self.query_impl_path = query_impl_path
        self.args_path = args_path
        self.builder_path = builder_path
        self.persistent_storage = persistent_storage

    def next_prompt(self) -> Optional[str]:
        # run only once
        if self.executed:
            return None
        self.executed = True

        result = self.run_tool.run_worker(
            mode=RunToolMode.EXHAUSTIVE,
            optimize=True,
            query_ids=None,
            trace_mode=False,
        )

        if result.success:
            logger.info("All validations passed, no need to fix.")
            return None

        prompt = base_exec_validate_prompt(
            query_impl_path=self.query_impl_path,
            args_path=self.args_path,
            show_ssd_error_hints=self.persistent_storage,
            builder_path=self.builder_path,
        )
        return prompt
