import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agents import custom_span

from observability.logging.truncate_csv import truncate_csvs_recursively
from synth_framework.runtime_tracker import RuntimeTracker
from tools.validate.run_and_check_queries import (
    Measurement,
    ValidationOutput,
    assemble_error,
    assemble_exec,
    check_output_correctness,
)
from utils import utils
from workloads.query_execution_cache import QueryExecutionCache
from workloads.workload_provider import (
    ExecSettings,
    QueryBatch,
    QueryEntry,
)

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    query_id: str
    req_id: str
    trace: str
    elapsed_ms: float
    error: str = ""


@dataclass
class ExecCallbackResult:
    resp: str
    out: str
    err: str
    ingest_time_ms: float
    query_results: list[QueryResult]


@dataclass
class ExecValidateResult:
    """Unified outcome of exec_and_validate.

    The exact same object is returned to the caller and pickled to the
    validation cache, so every field here is replayed verbatim on a cache hit.
    `replayed_from_cache` and `snapshot_hash` are cache bookkeeping; everything
    else is the actual run/validation result.
    """

    message: str
    success: bool
    metrics: Dict[str, Any]
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    resp: Optional[str] = None
    ingest_time_ms: Optional[float] = None
    trace_output: Optional[str] = None
    snapshot_hash: Optional[str] = None
    replayed_from_cache: bool = False

    def cmd_output(self) -> str:
        """Combined stdout/stderr/response block, as written to the debug log."""
        return (
            f"stdout:\n{(self.stdout or '').rstrip()}\n"
            f"stderr:\n{(self.stderr or '').rstrip()}\n"
            f"{self.resp or ''}"
        )


def _format_per_query_errors(query_results: list[QueryResult]) -> str:
    """Build a human-readable summary of per-query errors.

    Each error string is already self-identifying ("run #N Q<id>: <msg>",
    set by the per-query try/catch in query_impl.cpp), so we just collect
    the non-empty ones.  Returns an empty string if no per-query errors
    are present.
    """
    lines = [f"  - {qr.error}" for qr in query_results if qr.error]
    if not lines:
        return ""
    return "Per-query errors:\n" + "\n".join(lines)


class QueryValidator:
    ############
    # WARNING: add all call args to cache hash to ensure correct cache hits. If you change the call args, old cache entries will not be hit anymore and validation results will not be replayed from cache anymore until new cache entries for the new call args are generated.
    ############

    def __init__(
        self,
        validate_cache_dir: Path | None,
        workspace_path: Path,
        query_execution_cache: QueryExecutionCache,
        all_query_ids: list[str],
        git_snapshotter: Optional[Any] = None,
        output_stdout_stderr: bool = False,  # whether to include stdout and stderr in the validation result message also in case of correct validation (not only in case of errors)
        runtime_tracker: Optional[RuntimeTracker] = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        max_snapshot_csv_size_mb: Optional[
            float
        ] = None,  # if set, result CSVs in workspace_path are truncated to this size right before the post-run snapshot so they don't bloat snapshots
    ):
        self.workspace_path = workspace_path
        self.runtime_tracker = runtime_tracker
        self.query_execution_cache = query_execution_cache
        self.all_query_ids = all_query_ids
        self.validate_cache_dir = validate_cache_dir
        self.git_snapshotter = git_snapshotter
        self.output_stdout_stderr = output_stdout_stderr
        self.do_not_cache = do_not_cache
        self.only_from_cache = only_from_cache
        self.max_snapshot_csv_size_mb = max_snapshot_csv_size_mb

    def exec_and_validate(
        self,
        exec_callback_fn: Callable[..., ExecCallbackResult],
        query_batch: QueryBatch,
        compile_key_hash: str,
        trace_mode: bool,
        other_config: Dict[str, Any] = {},
        skip_validate: bool = False,
        recompile_if_necessary_callback: Callable
        | None = None,  # will internally check if recompilation is necessary (i.e. if compile result was from cache) and call the callback if it is necessary
    ) -> ExecValidateResult:
        with custom_span(
            f"exec_and_validate ({query_batch.exec_settings}, trace_mode={trace_mode}, {'no-validate' if skip_validate else ''})",
        ):
            logger.debug(f"Run with timeout: {query_batch.timeout_s} seconds")

            query_id = sorted({entry.query_id for entry in query_batch.query_list})

            result, cache_path, hash, hash_payload = self._check_answer_from_cache(
                query_batch=query_batch,
                skip_validate=skip_validate,
                other_config=other_config,
                stop_on_first_error=True,
                compile_key_hash=compile_key_hash,
            )

            if result is not None:
                # Cache hit: the cached object IS the result.
                result.replayed_from_cache = True
                if self.runtime_tracker is not None:
                    self.runtime_tracker.add_skipped_time(
                        result.metrics.get("validation/runtime_seconds", 0)
                    )
                logger.debug(result.cmd_output())
            else:
                result = self._execute_and_validate(
                    exec_callback_fn=exec_callback_fn,
                    query_batch=query_batch,
                    compile_key_hash=compile_key_hash,
                    trace_mode=trace_mode,
                    skip_validate=skip_validate,
                    cache_path=cache_path,
                    snapshot_name=hash,
                    query_id=query_id,
                    recompile_if_necessary_callback=recompile_if_necessary_callback,
                )

            result.metrics["validation/skip_validate"] = skip_validate

        # compute speedup
        if "validation/total_speedup" in result.metrics:
            total_speedup = f"{result.metrics['validation/total_speedup']:.2f}"
        else:
            total_speedup = "N/A"

        logger.info(
            f"Validate Tool Result: {'correct' if result.success else 'incorrect'} (Query ID: {query_id}, ExecSettings: {query_batch.exec_settings}, Replayed from cache: {result.replayed_from_cache}, trace_mode: {trace_mode}, optim-flags: {other_config['optimize']}) - Total Speedup: {total_speedup}x"
        )

        # truncate msg if too long for logging
        shortened_msg = (
            result.message[:1000] + "...(truncated)"
            if len(result.message) > 1000
            else result.message
        )

        with custom_span(
            f"exec_and_validate [result] ({'correct' if result.success else 'incorrect'}, {'replayed from cache' if result.replayed_from_cache else ''})",
            {
                "result": shortened_msg,
                "sql": query_id,
                "git snapshot": self.git_snapshotter.current_hash
                if self.git_snapshotter is not None
                else None,
            },
        ):
            return result

    def _execute_and_validate(
        self,
        exec_callback_fn: Callable[..., ExecCallbackResult],
        query_batch: QueryBatch,
        compile_key_hash: str,
        trace_mode: bool,
        skip_validate: bool,
        cache_path: Optional[Path],
        snapshot_name: str | None,
        query_id: list[str],
        recompile_if_necessary_callback: Callable | None,
    ) -> ExecValidateResult:
        """Run + validate a batch that was not found in cache, then cache it."""
        if self.only_from_cache:
            raise Exception(
                f"Validation result not found in cache for key {compile_key_hash} and only_from_cache is set. Cache path: {cache_path}"
            )

        # recompile if necessary (i.e. if compile result was from cache)
        if recompile_if_necessary_callback is not None:
            recompile_if_necessary_callback()

        validate_time_start = time.perf_counter()

        # execute queries via callback
        args_list = [entry.query_args for entry in query_batch.query_list]
        exec_result = exec_callback_fn(
            args_list=args_list, timeout_s=query_batch.timeout_s
        )
        ingest_ms = exec_result.ingest_time_ms

        if not skip_validate:
            validation_output = self._validate_query(
                exec_settings=query_batch.exec_settings,
                query_batch=query_batch,
                query_execution_cache=self.query_execution_cache,
                query_results=exec_result.query_results,
                stdout=exec_result.out,
                stderr=exec_result.err,
                cmd=None,
                stop_on_first_error=True,
                trace_mode=trace_mode,
                trace_data=_format_query_traces(
                    exec_result.query_results, query_batch.query_list
                ),
                resp=exec_result.resp,
            )
        else:
            logger.warning(
                f"Skipping correctness validation as requested ({query_id=}, {query_batch.exec_settings=}). Only return stdout/stderr."
            )
            validation_output = ValidationOutput(
                result_message=f"stdout: {exec_result.out.rstrip()}\nstderr: {exec_result.err.rstrip()}\n{exec_result.resp}",
                correct=True,
                metrics=assemble_exec(
                    exec_settings=query_batch.exec_settings,
                    num_queries_executed=len(args_list),
                ),
                trace_output=None,
            )

        message = validation_output.result_message
        if ingest_ms is not None:
            message += f"Build time (ms): {ingest_ms}\n"

        metrics = validation_output.metrics
        metrics["validation/executed_queries"] = len(args_list)
        metrics["validation/runtime_seconds"] = (
            time.perf_counter() - validate_time_start
        )
        metrics["validation/ingest_time_ms"] = ingest_ms

        result = ExecValidateResult(
            message=message,
            success=validation_output.correct,
            metrics=metrics,
            stdout=exec_result.out,
            stderr=exec_result.err,
            resp=exec_result.resp,
            ingest_time_ms=ingest_ms,
            trace_output=validation_output.trace_output,
        )

        # create snapshot of current source code - use response hash as snapshot name
        if not self.do_not_cache and self.git_snapshotter is not None:
            # Truncate result CSVs immediately before snapshotting so the
            # snapshot captures the post-truncation tree and current_hash stays
            # in sync with on-disk state on replay.
            if self.max_snapshot_csv_size_mb is not None:
                truncate_csvs_recursively(
                    self.workspace_path, max_size_mb=self.max_snapshot_csv_size_mb
                )
            _, result.snapshot_hash = self.git_snapshotter.snapshot(snapshot_name)

        logger.debug(result.cmd_output())

        # write to cache
        if cache_path is not None and not self.do_not_cache:
            assert result.snapshot_hash is not None, (
                "Git snapshot commit is None, but cache path is not None and do_not_cache is False. This should not happen, as the cache key is based on the git snapshot hash. Please check the GitSnapshotter implementation."
            )
            utils.dump_pickle(cache_path, result, do_not_cache=self.do_not_cache)
            logger.debug(f"Saved validation result to cache: {cache_path}")

        return result

    def _check_answer_from_cache(
        self,
        skip_validate: bool,
        other_config: Dict[str, Any],
        stop_on_first_error: bool,
        compile_key_hash: str,
        query_batch: QueryBatch,
    ) -> tuple[ExecValidateResult | None, Optional[Path], str | None, dict | None]:
        if self.git_snapshotter is None:
            logger.warning(
                "I don't know the current code version because GitSnapshotter is None. Hence I can't search for matching validation-tool cache."
            )
            return None, None, None, None

        hash_payload = {
            "query_batch": utils.stable_json(asdict(query_batch)),
            "snapshotter_hash": self.git_snapshotter.current_hash,
            "skip_validate": skip_validate,
            "stop_on_first_error": stop_on_first_error,
            "compile_key_hash": compile_key_hash,
            **other_config,
        }

        hash = utils.sha256(utils.stable_json(hash_payload))

        if self.validate_cache_dir is None:
            return None, None, hash, hash_payload

        cache_path = _cache_path_for_hash(self.validate_cache_dir, hash)
        if not cache_path.exists():
            return None, cache_path, hash, hash_payload

        # replay validation result from val-tool cache
        cached = utils.load_pickle(cache_path, ExecValidateResult)
        assert cached is not None
        logger.debug(f"Loaded validation result from cache: {cache_path}")

        # restore snapshot hash
        self.git_snapshotter.restore(cached.snapshot_hash)

        return cached, cache_path, hash, hash_payload

    def _validate_query(
        self,
        exec_settings: ExecSettings,
        query_batch: QueryBatch,
        query_execution_cache: QueryExecutionCache,
        query_results: List[QueryResult],
        stdout: str,
        stderr: str,
        cmd: Optional[str],
        trace_mode: bool,
        stop_on_first_error: bool = True,
        trace_data: str = "",
        resp: str = "",
    ) -> ValidationOutput:
        query_ids_executed = sorted(
            list(set([inst.query_id for inst in query_batch.query_list]))
        )

        from_cmd_str = "" if cmd is None else f" from command: {cmd}"

        # The hotpatch_proc response carries any stage-level error message
        # (e.g. "ERROR: query child killed by signal 11 (SIGSEGV)").
        resp_block = f"\nResponse:\n{resp}" if resp else ""
        per_query_errors = _format_per_query_errors(query_results)
        per_query_block = f"\n{per_query_errors}" if per_query_errors else ""

        if len(query_results) == 0:
            # Empty result vector means the C++ child died before
            # serialising results to the trace pipe, almost always an
            # async signal (SIGSEGV, SIGABRT, OOM-kill) that bypassed the
            # per-query try/catch in query_impl.cpp.  All in-memory results
            # are lost, so we cannot identify which query crashed; the
            # signal name in `resp` (if any) plus stderr/stdout are the
            # caller's best clues.
            requested = ", ".join(f"Q{i.query_id}" for i in query_batch.query_list)
            return ValidationOutput(
                result_message=(
                    f"Error: no query results received{from_cmd_str}. "
                    f"The process likely crashed before any results could be "
                    f"serialised (e.g. SIGSEGV, SIGABRT, or OOM-kill). Exact failing query is unknown: {requested}."
                    f"{resp_block}"
                    f"\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                ),
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=None,
                ),
            )

        if len(query_results) != len(query_batch.query_list):
            # Partial batch: the C++ side dropped some queries from the
            # request stream (e.g. malformed line that failed iss parsing).
            # The first un-returned slot is where the gap starts.
            crashed_idx = len(query_results)
            assert crashed_idx < len(query_batch.query_list), (
                f"Unexpectedly got more query results ({len(query_results)}) than query items ({len(query_batch.query_list)})."
            )
            crashed_qid = query_batch.query_list[crashed_idx].query_id
            crashed_at = (
                f" First missing slot: run #{crashed_idx + 1} (Q{crashed_qid})."
                if crashed_qid is not None
                else ""
            )
            return ValidationOutput(
                result_message=(
                    f"Error: unexpected number of query results{from_cmd_str}. "
                    f"Got {len(query_results)} but expected {len(query_batch.query_list)}."
                    f"{crashed_at}{resp_block}{per_query_block}"
                ),
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=crashed_qid,
                ),
            )

        # Per-query errors: at least one query in the batch threw a C++
        # exception (e.g. a parse failure inside parse_qN, or std::exception
        # raised from run_qN).  query_impl.cpp's per-query try/catch caught
        # it and continued, but the result CSV for that query was never written
        if per_query_errors:
            first_failed_idx = next(i for i, qr in enumerate(query_results) if qr.error)
            failed_qid = query_batch.query_list[first_failed_idx].query_id
            return ValidationOutput(
                result_message=(
                    f"Error: one or more queries threw an exception"
                    f"{from_cmd_str}.{resp_block}\n{per_query_errors}"
                ),
                correct=False,
                metrics=assemble_error(
                    exec_settings=exec_settings,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=failed_qid,
                ),
            )

        measurements = [
            Measurement(
                run_nr=i + 1,
                query_id=inst.query_id,
                exec_time=float(qr.elapsed_ms),
            )
            for i, (inst, qr) in enumerate(zip(query_batch.query_list, query_results))
        ]

        # validate with duckdb
        out_path = self.workspace_path / "results"
        out_path.mkdir(parents=True, exist_ok=True)
        return check_output_correctness(
            exec_settings=exec_settings,
            query_batch=query_batch,
            query_execution_cache=query_execution_cache,
            measurements=measurements,
            out_path=out_path,
            cmd=cmd,
            stop_on_first_error=stop_on_first_error,
            all_query_ids=self.all_query_ids,
            stdout=stdout if self.output_stdout_stderr else None,
            stderr=stderr if self.output_stdout_stderr else None,
            trace_mode=trace_mode,
            trace_data=trace_data,
        )


def _cache_path_for_hash(validate_cache_dir: Path, hash: str) -> Path:
    return validate_cache_dir / f"{hash}.pkl"


def _format_query_traces(
    query_results: list[QueryResult], query_items: list[QueryEntry]
) -> str:
    """Format per-query trace data from the query_results JSON array into a single string.

    query_results may be shorter than query_items when the C++ child crashed
    or timed out before serialising all results — _validate_query reports that
    case separately, so here we just format whatever traces we got. The opposite
    (more results than query_items) should be impossible: stale pipe data is
    caught by the batch_id check in hotpatch_proc.
    """
    assert len(query_results) <= len(query_items), (
        f"Got more query_results ({len(query_results)}) than query_items "
        f"({len(query_items)}) — likely stale pipe data that bypassed the batch_id check."
    )
    parts = []
    for qr, inst in zip(query_results, query_items):
        trace = qr.trace
        if trace is not None and trace.strip() != "":
            parts.append(f"--- Query {inst.query_id} ({qr.elapsed_ms}ms) ---\n{trace}")
    return "\n".join(parts)
