"""Stage list to create a basic implementation, plus its dynamic stages
(OptimizeBuildStage / ValidateAndFixStage)."""

import functools
import logging
from typing import Optional

from synnodb.conversations.conv_context import ConvContext
from synnodb.conversations.conversation_engine import (
    ValidationStillFailsException,
    extract_speedup_of_last_snapshot,
)
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
    base_validate_mt_prompt,
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
from synnodb.tools.data_inspect import subset_menu_for
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
            bespoke_rt_s, duckdb_rt_s, speedup = extract_speedup_of_last_snapshot(
                metrics, qid
            )
            if speedup < 0.2:
                assert duckdb_rt_s is not None, "DuckDB runtime is None"
                slow_queries.append(
                    (qid, bespoke_rt_s * 1000, duckdb_rt_s * 1000, speedup)
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
            storage_plan_filename=storage_plan_filename,
            base_impl_todo_filename=base_impl_todo_filename,
            read_storage_plan=ctx.bespoke_storage,
            lang=ctx.lang_profile,
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
                num_threads=ctx.threads,
                serve_from=spec.serve_from.value,
                schema_ddl=spec.schema(),
                data_subsets_note=subset_menu_for(ctx.workload_provider),
                lang=ctx.lang_profile,
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
                storage_plan_filename=storage_plan_filename,
                read_storage_plan=ctx.bespoke_storage,
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
            query_file = ctx.filenames.query_file(qid)
            impl_path = ctx.workspace_path / query_file
            if impl_path.exists():
                return None  # impl exists, proceed to next stage
            else:
                # return prompt to llm to fix this.
                logger.error(
                    f"Query implementation file {impl_path} does not exist. Reprompting the LLM now"
                )
                return f"You were supposed to implement query {qid} in a file called `{query_file}`. However, no such file exists in your workspace. Please implement the query and write it to `{query_file}` before proceeding to the execution & validation stage."

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
        ]
    )

    # In-memory generation already ran at the serving parallelism (ctx.threads), so the
    # base impl was exercised multi-threaded throughout. This gate is the authoritative
    # force-live safety sweep: a data race is nondeterministic, so a lucky pass on a
    # cached per-query validation during generation could have hidden one. It re-validates
    # each query live at ctx.threads and fixes any that diverge. In-memory only (SSD base
    # impls are not parallel-ready) and skipped when the engine is served single-threaded.
    if ctx.threads > 1 and not ctx.persistent_storage:
        stage_list.append(
            ValidateMultiThreadedStage(
                run_tool=ctx.run_tool,
                num_threads=ctx.threads,
                query_ids=ctx.query_ids,
                builder_path=builder_path,
                max_turns=ctx.max_turns,
                query_file_pattern=ctx.filenames.query_file_pattern,
            )
        )

    stage_list.extend(
        [
            Benchmark(),
            # # VALIDATE_MAX_SF_REP1_ON,  # only single repetition with largest (benchmarking) scale factor - we don't care if measured query rt is noisy
            # ValidateStdoutOff(),
            # # VALIDATE_OUTPUT_STDOUT_MAXSF_ON,  # include stdout and stderr for largest (benchmarking) scale factor to have more info in case of regressions
            # OptimizeBuildStage(
            #     builder_path_cpp=builder_cpp_path,
            #     builder_path_hpp=builder_hpp_path,
            #     run_tool=ctx.run_tool,
            #     persistent_storage=ctx.persistent_storage,
            #     allow_storage_restructuring=False,
            #     storage_plan_filename=storage_plan_filename,
            #     base_impl_todo_filename=base_impl_todo_filename,
            #     num_threads=ctx.threads,
            #     max_turns=ctx.max_turns,
            # ),
            # # VALIDATE_OUTPUT_STDOUT_MAXSF_OFF,
            # # VALIDATE_MAX_SF_REP1_OFF,
            # Benchmark(),
        ]
    )

    return stage_list


class OptimizeBuildStage(DynamicStageConfig):
    # The preceding "base impl storage" stage has no gate confirming the build actually
    # compiles/runs before handing off here, so a broken db_loader.cpp is a normal, expected
    # state to see on entry - retry with the compile/run error fed back to the LLM instead of
    # crashing, and give up loudly only after repeated failures.
    MAX_INGEST_FIX_ATTEMPTS = 3

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
        stage_threads: int | None = None,
        max_turns: int | None = None,
    ):
        super().__init__(
            descriptor="optimize ingest", max_turns=max_turns, threads=stage_threads
        )
        self.builder_path_cpp = builder_path_cpp
        self.builder_path_hpp = builder_path_hpp
        self.run_tool = run_tool
        self.executed = False
        self.ingest_fix_attempts = 0
        self.persistent_storage = persistent_storage
        self.allow_storage_restructuring = allow_storage_restructuring
        self.storage_plan_filename = storage_plan_filename
        self.base_impl_todo_filename = base_impl_todo_filename
        self.num_threads = num_threads

    def next_prompt(self) -> Optional[str]:
        if self.executed:
            return None

        run_result = self.run_tool.run_worker(
            mode=RunToolMode.INGEST,
            optimize=True,
            query_ids=None,
            trace_mode=False,
            external_call=True,
        )

        # `success` reflects query *correctness*, which is irrelevant here: no query has
        # been implemented yet at this point in the stage list (that happens later, via
        # ValidateAndFixStage and the per-query stages). Query stubs failing validation is
        # normal and must not block this stage - only a missing ingest_time_ms means the
        # build/ingest step itself didn't complete (compile error, crash, timeout).
        if run_result.ingest_time_ms is None:
            self.ingest_fix_attempts += 1
            if self.ingest_fix_attempts > self.MAX_INGEST_FIX_ATTEMPTS:
                raise ValidationStillFailsException(
                    "Ingest/build still does not compile or run cleanly after "
                    f"{self.ingest_fix_attempts - 1} fix attempts. Last error:\n"
                    f"{run_result.err or run_result.msg}"
                )
            err = run_result.err or run_result.msg or "unknown error"
            return (
                "The build does not compile or run cleanly yet, so its ingest time "
                f"cannot be measured for the optimize-build baseline:\n\n{err}\n\n"
                "Fix `db_loader.cpp`/`db_loader.hpp` (and the storage plan if needed) so "
                "the build compiles and the ingest run completes. Do not start on query "
                "implementations yet - the ingest run will be retried automatically."
            )

        self.executed = True
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
        stage_threads: int | None = None,
    ):
        super().__init__(
            descriptor="exec & validate", max_turns=max_turns, threads=stage_threads
        )
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


class ValidateMultiThreadedStage(DynamicStageConfig):
    """Per-query multi-threaded correctness gate for the finished base impl.

    For in-memory engines the generation stages already run at the serving thread count,
    so the engine's ``parallel_for`` / ``parallel_reduce`` paths were exercised throughout
    generation. This gate is still the authoritative catch: per-query validation during
    generation goes through the correctness cache, and a data race is nondeterministic - a
    lucky pass can be cached and replayed, hiding the race. This gate runs at the run's
    DEFAULT thread count (the serving target, ``self.num_threads``) with force-live
    execution (bypassing that cache): it walks the queries and runs each one there - a
    query still correct costs no LLM turn, while one whose result diverges gets a fix loop
    scoped to that query, re-validated at that thread count after every edit and giving up
    loudly after ``MAX_FIX_ATTEMPTS``.

    The run tool is at its default thread count here (in-memory generation stages set no
    override; SSD stages that forced serial only did so for their own span), so both this
    gate's checks AND the LLM's own ``run`` calls during a fix execute at the serving
    parallelism - a single-threaded run cannot reproduce, or confirm the fix of, a race.

    The walk is forward-only: once a query passes it is not re-checked. A fix is
    normally scoped to that query's own file; a fix that instead edits the shared
    builder could in principle regress an already-passed query, which this
    single-sweep pass would not catch (an accepted, rare tradeoff).
    """

    MAX_FIX_ATTEMPTS = 3

    def __init__(
        self,
        run_tool: RunTool,
        num_threads: int,
        query_ids: list[str],
        builder_path: str,
        max_turns: int | None = None,
        query_file_pattern: str = "query{qid}.cpp",
    ):
        super().__init__(
            descriptor="validate multi-threaded correctness", max_turns=max_turns
        )
        self.run_tool = run_tool
        self.num_threads = num_threads
        self.query_ids = list(query_ids)
        self.builder_path = builder_path
        self.query_file_pattern = query_file_pattern
        self.idx = 0  # index of the query currently under validation
        self.fix_attempts = 0  # fix attempts for the query at self.idx

    def next_prompt(self) -> Optional[str]:
        assert self.num_threads >= 1, (
            f"Num threads must be at least 1. Given: {self.num_threads}"
        )
        if self.num_threads <= 1:
            return None

        # This gate runs at the run's DEFAULT thread count (the serving target). In-memory
        # generation stages set no override, so the run tool is already at its default here,
        # and both this gate's runs and the LLM's own `run` calls execute multi-threaded -
        # a single-threaded run cannot reproduce, or confirm the fix of, a data race.
        while self.idx < len(self.query_ids):
            qid = self.query_ids[self.idx]
            # force_live is disabled: this gate replays QueryValidator's cache when a prior
            # validation of this exact build+query exists. Ideally a data race would be re-run
            # live every attempt (a lucky pass - from the multi-threaded per-query generation
            # validation, the LLM's own run during a fix, or an earlier check of this snapshot -
            # could otherwise be replayed as the gate's verdict), but forcing a live re-execution
            # re-snapshots an already-captured build and trips the snapshot-name uniqueness assert.
            # Re-enable once snapshot() tolerates re-snapshotting an existing content-addressed name.
            result = self.run_tool.run_worker(
                mode=RunToolMode.EXHAUSTIVE,
                optimize=True,
                query_ids=[qid],
                trace_mode=False,
                force_live=False,
            )

            if result.success:
                logger.info("Query %s correct at %d threads.", qid, self.num_threads)
                self.idx += 1
                self.fix_attempts = 0
                continue

            self.fix_attempts += 1
            error = result.msg or result.err or "results diverged from the reference"
            if self.fix_attempts > self.MAX_FIX_ATTEMPTS:
                raise ValidationStillFailsException(
                    f"Query {qid} still produces incorrect results at {self.num_threads} "
                    f"threads after {self.fix_attempts - 1} fix attempt(s) - a data "
                    f"race in its parallelization remains. Last error:\n{error}"
                )

            return base_validate_mt_prompt(
                query_id=qid,
                num_threads=self.num_threads,
                builder_path=self.builder_path,
                error=error,
                query_file=self.query_file_pattern.format(qid=qid),
            )

        return None
