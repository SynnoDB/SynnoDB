from __future__ import annotations

import functools
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cpp_runner.compiler.compiler_cached import CachedCompiler
from cpp_runner.hotpatch.hotpatch_proc import HotpatchProc, HotpatchProcRunResult
from cpp_runner.hotpatch.pool import HotpatchPool
from observability.logging.run_stats_collector import RunStatsCollector
from tools.run_tool_mode import RunToolMode
from tools.validate.query_validator_class import (
    ExecCallbackResult,
    QueryValidator,
)
from tools.validate.run_and_check_queries import assemble_error
from utils.json_utils import json_dumps
from utils.utils import DBStorage
from workloads.workload_provider import QueryBatch, WorkloadProvider

logger = logging.getLogger(__name__)


@dataclass
class RunWorkerResult:
    msg: str
    success: bool
    metrics: Optional[Dict] = None
    resp: Optional[str] = None
    out: Optional[str] = None
    err: Optional[str] = None
    trace_output: Optional[str] = None
    query_batch: QueryBatch | None = None
    query_results: list | None = None
    ingest_time_ms: Optional[float] = None


class RunTool:
    """Runs the database and executes a query by id"""

    parse_out_and_validate_output: bool = True

    def __init__(
        self,
        workload_provider: WorkloadProvider,
        cwd: Path,
        dataset_name: str,
        base_parquet_dir: str
        | Path,  # must contain per scale-factors subdirs: e.g. base_parquet_dir/sf1/, base_parquet_dir/sf10/..., each containing the corresponding parquet files for the scale factor
        db_storage: DBStorage,
        compiler: CachedCompiler,
        run_stats_collector: RunStatsCollector | None,
        query_validator: Optional[QueryValidator] = None,
        parse_out_and_validate_output: bool = True,
        validate_output_truncation: Optional[
            int
        ] = 10000,  # restrict output to 10000 chars ~ 2.5 Thousand tokens
        compile_output_truncation: Optional[
            int
        ] = 10000,  # restrict output to 10000 chars ~ 2.5 Thousand tokens
        parallelism: bool = False,
        core_ids: Optional[List[int]] = None,
        delete_result_csv_before_execution: bool = True,  # this is important to make sure that we do not read old results from previous runs
        memory_budget_mb: (
            int | None
        ) = None,  # total RAM budget for the child engine; drives RLIMIT_AS and the generated frame-pool size. Only needed for disk-based storage runs.
        include_mem_budget_for_in_mem_in_hashes: bool = False,  # opt-in: include memory_budget_mb in the validate-cache hash even for IN_MEMORY storage (legacy behaviour, useful for hitting old caches)
    ):
        self.workload_provider = workload_provider
        self.cwd = cwd
        self.dataset_name = dataset_name
        self.base_parquet_dir = base_parquet_dir

        self.compiler = compiler
        self.query_validator: Optional[QueryValidator] = query_validator
        self.run_stats_collector = run_stats_collector
        self.parse_out_and_validate_output = parse_out_and_validate_output
        self.validate_output_truncation = validate_output_truncation
        self.compile_output_truncation = compile_output_truncation
        self.parallelism = parallelism
        self.core_ids = core_ids
        self.delete_result_csv_before_execution = delete_result_csv_before_execution
        self.memory_budget_mb = memory_budget_mb
        self.db_storage = db_storage
        self.include_mem_budget_for_in_mem_in_hashes = (
            include_mem_budget_for_in_mem_in_hashes
        )
        # Intentionally keep self.memory_budget_mb=None when caller passed
        # None: it propagates into the validate-cache hash payload (so the key
        # is stable across machines), while the buffer-pool / RLIMIT_AS sites
        # below already guard on None and fall back to HotpatchProc's own
        # 90%-of-phys-RAM default at runtime.

    def run(
        self,
        mode: RunToolMode,
        optimize: bool,
        query_ids: list[str] | None = None,
        trace_mode: bool = False,  # set trace flag
        external_call: bool = False,  # only for logging purposes
        echo_output: bool = False,  # print stdout and co directly
    ) -> Tuple[str, Optional[Dict], str | None]:
        try:
            run_result = self.run_worker(
                mode=mode,
                optimize=optimize,
                query_ids=query_ids,
                trace_mode=trace_mode,
                external_call=external_call,
                echo_output=echo_output,
            )
        except FileNotFoundError:
            raise Exception(
                "db executable not found. This shoud not happen - recompile should be done if answered from cache"
            )

        # truncate trace output if necessary
        trace_output = run_result.trace_output

        # apply capping
        if trace_output is not None:
            if (
                len(trace_output) > 10000
            ):  # restrict output to 10000 chars ~ 2.5 Thousand tokens
                trace_output = trace_output[:10000] + "\n...[truncated]..."

        return run_result.msg, run_result.metrics, trace_output

    def run_worker(
        self,
        mode: RunToolMode,
        optimize: bool,
        query_ids: list[str] | None = None,
        trace_mode: bool = False,  # set trace flag
        force_compile: bool = False,
        external_call: bool = False,
        current_git_snapshot: Optional[
            str
        ] = None,  # for external instrumentation: e.g. from benchmarking script (will not use git snapshotter)
        echo_output: bool = False,  # print stdout and co directly
        parallelism: bool | None = None,
        core_ids: list[int] | None = None,
    ) -> RunWorkerResult:
        if isinstance(query_ids, list) and len(query_ids) == 0:
            # rewrite to None for easier handling
            query_ids = None

        # rewrite query-ids
        if query_ids is not None:
            available_query_ids = self.workload_provider.query_ids
            rewritten_query_ids = []

            for q_id in query_ids:
                if q_id in available_query_ids:
                    rewritten_query_ids.append(q_id)
                else:
                    # check if llm accidently calls query with q prefix (e.g. q1 instead of 1) - if yes, auto-rewrite and continue with a warning, otherwise error out
                    if (q_id.startswith("q") or q_id.startswith("Q")) and q_id[
                        1:
                    ] in available_query_ids:
                        logger.warning(
                            f"Query ID {q_id} not recognized, but {q_id[1:]} is in the list of known query IDs. Auto rewriting it."
                        )
                        rewritten_query_ids.append(q_id[1:])
                        continue

                    # ERROR: query ID not recognized
                    return RunWorkerResult(
                        msg=f"Error: Query ID {q_id} not recognized. Available query IDs are: {available_query_ids}",
                        err=f"Query ID {q_id} not recognized. Available query IDs are: {available_query_ids}",
                        success=False,
                    )

            query_ids = rewritten_query_ids

        current_parallelism = (
            parallelism if parallelism is not None else self.parallelism
        )
        current_core_ids = core_ids if core_ids is not None else self.core_ids
        current_num_threads = (
            len(current_core_ids)
            if current_parallelism and current_core_ids is not None
            else 1
        )
        if current_parallelism:
            assert current_core_ids is not None, (
                "core_ids must be provided if parallelism is enabled"
            )

        # local definition overwrites global
        extra_env = dict()
        if current_parallelism:
            assert current_core_ids is not None
            extra_env["CORE_IDS"] = ",".join(str(c) for c in current_core_ids)
        else:
            extra_env["CORE_IDS"] = (
                "1"  # pass a single core. Just to ensure that it is not falling back to "use all cores" in case the thread-pool is already implemented.
            )

        logger.info(
            f"Run with: {query_ids=} {mode=} {self.dataset_name=} {trace_mode=} {optimize=} {self.base_parquet_dir=} num_threads={len(current_core_ids) if current_parallelism else '1'} mem_limit={self.memory_budget_mb}"  # type: ignore
        )

        # Delete any result CSV files written by a previous run so we never
        # accidentally read stale results if the child crashes before writing
        # fresh ones.
        if self.delete_result_csv_before_execution:
            delete_result_csv_files(self.cwd)

        #################
        # COMPILATION
        #################

        # set compile mode
        self.compiler.set_compile_options(optimize=optimize, trace_mode=trace_mode)

        import time as _time

        logger.info(f"build_cached: starting compilation (trace={trace_mode})")
        _compile_start = _time.monotonic()
        err, compile_used_cache, compile_key_hash = self.compiler.build_cached(
            skip_cache=force_compile,
            current_git_snapshot=current_git_snapshot,
        )
        logger.info(
            f"build_cached: done in {_time.monotonic() - _compile_start:.1f}s (cached={compile_used_cache})"
        )

        if err is not None:
            # report stats
            # assemble validation error
            metrics = assemble_error(
                exec_settings=None,
                query_ids_executed=query_ids if query_ids is not None else [],
            )
            metrics["type"] = "validate"
            metrics["validation/compile_with_optimize"] = optimize
            metrics["validation/trace_mode"] = trace_mode
            metrics["validation/compile_error"] = True
            metrics["validation/external_call"] = external_call
            if self.run_stats_collector is not None:
                self.run_stats_collector.log_metrics_callback(
                    metrics, log_and_increment=True
                )
                self.run_stats_collector.add_to_activity_summary(
                    "Run Tool called: failed with compile error"
                )

            # do compile truncations
            if self.compile_output_truncation is not None:
                if len(err) > self.compile_output_truncation:
                    err = err[: self.compile_output_truncation] + "\n...[truncated]..."
            logger.error(f"Compile error: {err}")
            return RunWorkerResult(
                msg=err,
                err=err,
                success=False,
                metrics=metrics,
            )

        assert compile_key_hash is not None, (
            "compile_key_hash should not be None if compile did not return an error. This should not happen."
        )

        #################
        # PRODUCE WORKLOAD
        #################

        query_batches = self.workload_provider.produce_workload(
            run_mode=mode,
            num_threads=current_num_threads,
            core_ids=current_core_ids,
            query_ids=query_ids,
        )

        #################
        # RUN & VALIDATE QUERIES
        #################

        result_list = []
        for batch in query_batches:
            result = self.run_query_batch(
                batch,
                echo_output=echo_output,
                compile_used_cache=compile_used_cache,
                current_git_snapshot=current_git_snapshot,
                optimize=optimize,
                trace_mode=trace_mode,
                compile_key_hash=compile_key_hash,
                general_extra_env=extra_env,
                external_call=external_call,
                current_parallelism=current_parallelism,
                current_core_ids=current_core_ids,
                run_tool_mode=mode,
            )  # TODO: compile used cache does not update - e.g. in the first iteration it will compile, pass info to second iteration.
            result_list.append(result)

            if not result.success:
                # early return
                break

        return result_list[-1]

    def run_query_batch(
        self,
        batch: QueryBatch,
        echo_output: bool,
        compile_used_cache: bool,
        current_git_snapshot: Optional[str],
        optimize: bool,
        trace_mode: bool,
        compile_key_hash: str,
        general_extra_env: dict[str, str],
        external_call: bool,
        current_parallelism: bool,
        run_tool_mode: RunToolMode,
        current_core_ids: list[int] | None,
    ) -> RunWorkerResult:
        # assemble call cmd
        cmd = f"./db {batch.cli_call_args}"

        # start with general extra env passed to the function - create copy
        extra_env = dict(general_extra_env)

        if batch.extra_env is not None:
            # ensure no overlap
            assert not set(extra_env.keys()).intersection(batch.extra_env.keys()), (
                f"extra_env keys {set(extra_env.keys())} and batch.extra_env keys {set(batch.extra_env.keys())} overlap. This should not happen."
            )
            extra_env.update(batch.extra_env)

        pool_key = cmd

        # Bound the child's virtual memory via RLIMIT_AS to the same budget the
        # generated frame pool is sized from. mmap_col regions and frame
        # allocations both count against this; the kernel rejects allocations
        # that would exceed it. When no explicit budget is set, fall back to a
        # 90%-of-phys-RAM safety cap so a runaway child cannot OOM the host.
        # NOTE: this fallback is intentionally computed here rather than baked
        # into self.memory_budget_mb earlier — keeping self.memory_budget_mb=None
        # in that case preserves a stable validate-cache hash across hosts
        # (SC_PHYS_PAGES varies slightly between machines / reboots).
        hp_kwargs: dict = {
            "echo_output": False,
            "cwd": self.cwd,
        }

        memory_limit_mb = batch.general_system_config.memory_limit_mb
        if memory_limit_mb is not None:
            pool_key += f"|memory_budget_mb={memory_limit_mb}"
            hp_kwargs["memory_limit_bytes"] = memory_limit_mb * 1024 * 1024
        else:
            phys_ram_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            hp_kwargs["memory_limit_bytes"] = int(phys_ram_bytes * 0.9)

        runner = HotpatchPool.get(
            pool_key,
            factory=lambda: HotpatchProc(cmd, **hp_kwargs),
        )

        # validate output correctness
        # in case query-validator is not provided or manual-stdin args are provided, just execute without validation
        if self.query_validator is not None:

            def fn_compile_callback():
                if compile_used_cache:
                    # compile was answered from cache! I.e. no up-to-date db file was built. We have to recompile to make sure up-to-date db file is present for execution
                    logger.info(
                        "Compile was from cache, recompiling without cache to make sure up-to-date db binary is present for execution"
                    )
                    _, _, _ = self.compiler.build_cached(
                        skip_cache=True,
                        write_cache=False,  # do not update cache entry with new compile - keep the old one.(just in case compile output is not deterministic - we don't want to break our caching chain with a non-deterministic compile output)
                        current_git_snapshot=current_git_snapshot,
                    )

            execute_fn = functools.partial(
                call_hotpatch_proc,
                runner=runner,
                extra_env=extra_env,
                echo_output=echo_output,
            )

            # this branch is cached via validation tool cache
            val_result = self.query_validator.exec_and_validate(
                exec_callback_fn=execute_fn,
                query_batch=batch,
                other_config={
                    "optimize": optimize,
                    "memory_budget_mb": self.memory_budget_mb,
                },
                skip_validate=not self.parse_out_and_validate_output,
                compile_key_hash=compile_key_hash,  # via this hash we ensure val is correctly chained to cache.
                recompile_if_necessary_callback=fn_compile_callback,
                trace_mode=trace_mode,
            )

            msg = val_result.message
            success = val_result.success
            metrics = val_result.metrics
            trace_output = val_result.trace_output
            resp = val_result.resp
            stdout = val_result.stdout
            stderr = val_result.stderr
            ingest_time_ms = val_result.ingest_time_ms

            if success and run_tool_mode == RunToolMode.INGEST:
                assert val_result.ingest_time_ms is not None, (
                    "ingest_time_ms should be set in ingest mode. This should not happen."
                )
                msg = f"Query results are correct. Ingest/build time: {val_result.ingest_time_ms:.2f}ms with call args:\n```\n{json_dumps(asdict(batch.exec_settings), indent=2)}\n```\nStdout:\n```\n{stdout}\n```\n\nStderr:\n```\n{stderr}\n```"

            # this assertion does unfortunately not work: it is valid that args for validate change, but compile is the same. E.g. different scale factors.
            # assert compile_used_cache == val_result.replayed_from_cache, (
            #     "Inconsistent cache usage between compile and execute. This should always be chained! If this happens, potentially a change in the wrapper code/... happened. Please delete both cache entries (compile & exec), check your changes and re-run."
            # )
            if val_result.replayed_from_cache:
                assert compile_used_cache, (
                    "Inconsistent cache usage between compile and execute: if exec was cached then compile also needs to be cached. This should always be chained! If this happens, potentially a change in the wrapper code/... happened. Please delete the corresponding cache entry (validate cache), check your changes and re-run."
                )
            query_results = None
        else:
            # this branch is not cached
            logger.warning(
                "No query validator provided, just executing the query without validation!"
            )

            args_list = [entry.query_args for entry in batch.query_list]

            run_result: ExecCallbackResult = call_hotpatch_proc(
                runner=runner,
                extra_env=batch.extra_env if batch.extra_env is not None else {},
                echo_output=echo_output,
                args_list=args_list,
                timeout_s=batch.timeout_s,
            )

            msg = f"stdout: {run_result.out.rstrip()}\nstderr: {run_result.err.rstrip()}\n{run_result.resp}"
            trace_output = ",".join(qr.trace for qr in run_result.query_results)
            resp = run_result.resp
            stdout = run_result.out
            stderr = run_result.err
            ingest_time_ms = run_result.ingest_time_ms

            # extract queries with error
            per_query_errors = [qr.error for qr in run_result.query_results if qr.error]
            if len(per_query_errors) > 0:
                first_failed_idx = next(
                    i for i, qr in enumerate(run_result.query_results) if qr.error
                )
                query_ids_executed = []
                for line in args_list:
                    parts = line.split(maxsplit=1)
                    query_ids_executed.append(parts[0] if parts else "")
                failed_query_id = (
                    query_ids_executed[first_failed_idx]
                    if first_failed_idx < len(query_ids_executed)
                    else None
                )
                msg = "Error: one or more queries threw an exception.\n" + msg
                metrics = assemble_error(
                    exec_settings=batch.exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=failed_query_id,
                )
                success = False
                query_results = None
            else:
                metrics = dict()
                if run_result.query_results:
                    metrics["run/total_rt"] = sum(
                        qr.elapsed_ms for qr in run_result.query_results
                    )
                success = True
                query_results = run_result.query_results

        # report stats
        assert isinstance(metrics, Dict), (
            f"Metrics should be a dict at this point, got {type(metrics)}. This should not happen."
        )
        metrics["type"] = "validate"
        metrics["validation/compile_with_optimize"] = optimize
        metrics["validation/trace_mode"] = trace_mode
        metrics["validation/external_call"] = external_call
        metrics["validation/parallelism"] = current_parallelism
        metrics["validation/core_ids"] = current_core_ids
        if self.run_stats_collector is not None:
            self.run_stats_collector.log_metrics_callback(
                metrics, log_and_increment=True
            )

            self.run_stats_collector.add_to_activity_summary(
                f"Run Tool called: {'success' if success else 'incorrect query output'}"
            )

        # perform truncation
        if self.validate_output_truncation is not None:
            if len(msg) > self.validate_output_truncation:
                msg = msg[: self.validate_output_truncation] + "\n...[truncated]..."

        return RunWorkerResult(
            msg=msg,
            metrics=metrics,
            resp=resp,
            out=stdout,
            err=stderr,
            trace_output=trace_output,
            query_batch=batch,
            query_results=query_results,
            success=success,
            ingest_time_ms=ingest_time_ms,
        )

    def __call__(
        self,
        mode: str,
        optimize: bool,
        query_ids: Optional[List[str]] = None,
        trace_mode: bool = False,  # sets trace flag for the run
    ) -> str:
        # update tool interface

        if mode in ["fast_check", "FAST_CHECK"]:
            rt_mode = RunToolMode.FAST_CHECK
        elif mode in ["exhaustive", "EXHAUSTIVE"]:
            rt_mode = RunToolMode.EXHAUSTIVE
        elif mode in ["benchmark", "BENCHMARK"]:
            rt_mode = RunToolMode.BENCHMARK
        elif mode in ["ingest", "INGEST"]:
            rt_mode = RunToolMode.INGEST
        else:
            return f"Invalid mode specified. Available: fast_check, exhaustive, benchmark, ingest. Got {mode}."

        return self.run(
            mode=rt_mode,
            optimize=optimize,
            query_ids=query_ids,
            trace_mode=trace_mode,
        )[0]


def delete_result_csv_files(workspace_path: Path):
    # delete all .csv files from prior runs
    csv_files = list(workspace_path.rglob("result*.csv"))
    if len(csv_files) > 0:
        logger.info(f"Deleting existing result-csv files ({len(csv_files)} files).")
        for csv_file in csv_files:
            csv_file.unlink()


# callback executing the query
def call_hotpatch_proc(
    runner: HotpatchProc,
    args_list: List[str],
    timeout_s: int,
    extra_env: dict[str, str],
    echo_output: bool,
) -> ExecCallbackResult:
    # query_lines are bundled into the RUN message atomically, replacing
    # the old separate runner.send() loop that could leave lines buffered
    # in stdin and consumed by a later invocation.
    hotpatch_proc_run_result: HotpatchProcRunResult = runner.run(
        timeout=timeout_s,
        query_lines=args_list,
        run_env=extra_env,
        echo_output=echo_output,
    )

    if len(hotpatch_proc_run_result.query_results) == 0 and (
        # "terminate called after throwing an instance of 'std::bad_alloc'"
        # in hotpatch_proc_run_result.stderr or
        "std::bad_alloc" in hotpatch_proc_run_result.stdout
        or "std::bad_alloc" in hotpatch_proc_run_result.stderr
    ):
        # retry
        logger.warning(
            "Process likely killed due to OOM (std::bad_alloc). Retrying once..."
        )
        hotpatch_proc_run_result: HotpatchProcRunResult = runner.run(
            timeout=timeout_s,
            query_lines=args_list,
            run_env=extra_env,
            echo_output=echo_output,
        )

    resp = hotpatch_proc_run_result.response
    out = hotpatch_proc_run_result.stdout
    err = hotpatch_proc_run_result.stderr

    # extract ingest time from output and cache it in runner (this is not always executed again, since we are hotpatching and only rerun ingest if necessary)
    # search for ingest line matching "Ingest ms: <num>"
    ingest_key = "Ingest ms:"
    ingest_lines = [
        line for line in (out + err).splitlines() if line.startswith(ingest_key)
    ]
    if len(ingest_lines) == 0:
        ingest_time_ms = runner.last_ingest_time_ms  # fallback to last ingest time if not found in output (e.g. because of output truncation)

        if ingest_time_ms == -1:
            # error during first ingest run
            assert "builder start" in out.lower() or "builder start" in err.lower(), (
                "Ingest time not found in output and no cached ingest time available. This should not happen - at least one of them should be available. Output:\n"
                + f"STDERR:\n{err}\nSTDOUT:\n{out}\nResp:\n{resp}"
            )
            logger.info("Build phase failed during invocation. LLM has to fix it.")
            ingest_time_ms = -1
        # else:
        #     logger.debug(
        #         "reusing cached ingest time from runner: %.2fms", ingest_time_ms
        #     )
    else:
        assert len(ingest_lines) == 1, (
            "Multiple ingest lines found in program stdout. "
            "Expected exactly one line like: 'Ingest ms: <num>'.\n"
            + f"STDERR:\n{err}\nSTDOUT:\n{out}\nResp:\n{resp}"
        )
        ingest_time_ms_str = ingest_lines[0].strip()
        ingest_time_ms_str = (
            ingest_time_ms_str[len("Ingest ms:") :].strip().strip(":").strip()
        )
        ingest_time_ms = float(ingest_time_ms_str)

        # cache ingest time in runner for future request
        runner.last_ingest_time_ms = ingest_time_ms

    logger.info(f"resp={resp.rstrip()}")
    return ExecCallbackResult(
        resp=resp,
        out=out,
        err=err,
        ingest_time_ms=ingest_time_ms,
        query_results=hotpatch_proc_run_result.query_results,
    )
