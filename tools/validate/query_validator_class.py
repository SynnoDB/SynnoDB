import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple

from agents import custom_span

from observability.benchmark.systems.umbra import UmbraRunner
from observability.logging.truncate_csv import truncate_csvs_recursively
from synth_framework.runtime_tracker import RuntimeTracker
from tools.validate.duckdb_connection_manager import DuckDBConnectionManager
from tools.validate.query_cache import QueryCache, QueryInstantiation
from tools.validate.run_and_check_queries import (
    Measurement,
    ValidationOutput,
    assemble_error,
    assemble_exec,
    check_output_correctness,
)
from tools.validate.validate_cache_type import ValidateCacheType
from utils import utils
from utils.utils import DBStorage
from workloads.workload_provider import QueryBatch

logger = logging.getLogger(__name__)

PIN_CORE = 3


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
        benchmark: str,
        gen_query_fn: Callable,
        sf_list: List[float],
        parquet_path: str,
        wandb_pin_worker: bool,
        all_query_ids: List[str],
        num_random_query_instantiations: int,
        query_cache_dir: Path,
        validate_cache_dir: Path | None,
        workspace_path: Path,
        db_storage: DBStorage,
        disk_db_dir: Optional[Path] = None,
        git_snapshotter: Optional[Any] = None,
        output_stdout_stderr: bool = False,  # whether to include stdout and stderr in the validation result message also in case of correct validation (not only in case of errors)
        output_stdout_stderr_for_max_sf: bool = False,  # include stdout and stderr for max scale factor
        runtime_tracker: Optional[RuntimeTracker] = None,
        do_not_cache: bool = False,
        only_from_cache: bool = False,
        run_umbra_as_well: bool = False,  # whether to run UMBRA plans for validation.
        rep1_for_max_sf: bool = False,  # whether to use 1 repetition for the largest scale factor - e.g. for build improvement we don't care about query runtime, only build runtime
        num_threads: int = 1,  # number of threads for the bespoke run tool; used to key query cache and fetch MT DuckDB reference runtimes. 1 = single threaded
        core_ids: Optional[
            List[int]
        ] = None,  # list of core ids to pin the MT implementation to - if None, no pinning is applied; only used for the multi-threading optimization conversation mode
        max_snapshot_csv_size_mb: Optional[
            float
        ] = None,  # if set, result CSVs in workspace_path are truncated to this size right before the post-run snapshot so they don't bloat snapshots
    ):
        self.benchmark = benchmark
        self.all_query_ids = all_query_ids
        self.workspace_path = workspace_path
        self.runtime_tracker = runtime_tracker
        self.query_cache_dir = query_cache_dir
        self.validate_cache_dir = validate_cache_dir
        self.num_random_query_instantiations = num_random_query_instantiations
        self.git_snapshotter = git_snapshotter
        self.wandb_pin_worker = wandb_pin_worker
        self.output_stdout_stderr = output_stdout_stderr
        self.output_stdout_stderr_for_max_sf = output_stdout_stderr_for_max_sf
        self.do_not_cache = do_not_cache
        self.rep1_for_max_sf = rep1_for_max_sf
        self.num_threads = num_threads
        self.core_ids = core_ids
        self.parquet_path = parquet_path
        self.gen_query_fn = gen_query_fn
        self.run_umbra_as_well = run_umbra_as_well
        self.only_from_cache = only_from_cache
        self.max_snapshot_csv_size_mb = max_snapshot_csv_size_mb
        self.db_storage = db_storage
        self.disk_db_dir = disk_db_dir

        # Create DuckDB connection managers each scale factor
        self.duckdb_con: Dict[float, DuckDBConnectionManager] = dict()

        self._init_with_sf_list(sf_list)

    def _init_with_sf_list(self, sf_list: list[float]):
        self.sf_list = sf_list
        for sf in sf_list:
            if self.num_threads == 1:
                self.duckdb_con[sf] = DuckDBConnectionManager(
                    benchmark=self.benchmark,
                    pre_load_duckdb_tables=False,
                    parquet_path=self.parquet_path,
                    sf=sf,
                    pin_worker=self.wandb_pin_worker,
                    pin_core=PIN_CORE,
                    num_threads=1,
                    db_storage=self.db_storage,
                    disk_db_dir=self.disk_db_dir,
                )
            else:
                self.duckdb_con[sf] = DuckDBConnectionManager(
                    benchmark=self.benchmark,
                    pre_load_duckdb_tables=False,
                    parquet_path=self.parquet_path,
                    sf=sf,
                    pin_worker=False,
                    pin_core=None,
                    num_threads=self.num_threads,
                    db_storage=self.db_storage,
                    disk_db_dir=self.disk_db_dir,
                )

        umbra_runner = None
        if self.run_umbra_as_well:
            umbra_runner = UmbraRunner(
                parquet_path=Path(self.parquet_path),
                benchmark=self.benchmark,
                scale_factors=sf_list,
                container_num_cores=self.num_threads,
                container_pin_core_id_start=self.core_ids[0] if self.core_ids else 4,
                allow_auto_restarts=True,
                db_storage=self.db_storage,
                disk_db_dir=self.disk_db_dir,
            )

        # Pre-generate all query instantiations and execute them with DuckDB
        # Results are cached in the QueryCache for efficient validation
        logger.info("Initializing query cache with pre-generated instantiations...")
        self.query_cache = QueryCache(
            gen_query_fn=self.gen_query_fn,
            query_ids=self.all_query_ids,
            sf_list=sf_list,
            num_instantiations_per_query=self.num_random_query_instantiations,
            duckdb_managers=self.duckdb_con,
            cache_dir=self.query_cache_dir,
            run_umbra_plans=self.run_umbra_as_well,
            umbra_runner=umbra_runner,
            only_from_cache=self.only_from_cache,
            num_threads=self.num_threads,
            do_not_cache=self.do_not_cache,
            db_storage=self.db_storage,
        )

        # after initializing the query cache, we cleanup the DuckDB connections
        for duckdb_con in self.duckdb_con.values():
            duckdb_con.clear_mem_footprint(
                including_disk=False
            )  # only prune from memory

        if self.run_umbra_as_well:
            assert umbra_runner is not None
            umbra_runner.stop()

        if self.validate_cache_dir is not None:
            utils.create_dir_and_set_permissions(self.validate_cache_dir)

    def exec_and_validate(
        self,
        exec_callback_fn: Callable[..., ExecCallbackResult],
        query_batch: QueryBatch,
        compile_key_hash: str,
        trace_mode: bool,
        other_config: Dict[str, Any] = {},
        skip_validate: bool = False,
        only_from_cache: bool = False,
        recompile_if_necessary_callback: Optional[
            Callable
        ] = None,  # will internally check if recompilation is necessary (i.e. if compile result was from cache) and call the callback if it is necessary
        num_threads_for_logging: int | None = None,
    ) -> Tuple[str, bool, Dict[str, Any], bool, Optional[str]]:
        with custom_span(
            f"exec_and_validate ({query_id if query_id is not None else 'all queries'}, sf={scale_factor}, trace_mode={trace_mode}, {'no-validate' if skip_validate else ''})",
        ):
            logger.debug(f"Run with timeout: {query_batch.timeout_s} seconds")

            result, cache_path, hash_payload, hash = self._check_answer_from_cache(
                query_batch=query_batch,
                skip_validate=skip_validate,
                other_config=other_config,
                stop_on_first_error=True,
                compile_key_hash=compile_key_hash,
                num_threads=num_threads_for_logging
                if num_threads_for_logging is not None
                else self.num_threads,
            )

            query_id = list(set([entry.query_id for entry in query_batch.query_list]))

            replayed_from_cache = False
            if result is not None:
                # Found in cache
                msg = result.outputs
                success = result.success
                metrics = result.metrics
                trace_output = result.trace_output

                replayed_from_cache = True

                # fallback for old cache state where query_ids_executed was not stored - extract from query cache
                if "validation/query_ids_executed" not in metrics:
                    exec_list = list(
                        query_id if query_id is not None else self.all_query_ids
                    )
                    metrics["validation/query_ids_executed"] = exec_list

                    logger.debug(f"Log: {exec_list} / query id: {query_id}")

                if "validation/total_umbra_runtime_ms" not in metrics:
                    # Fallback for old cache entries: derive
                    # total_umbra_runtime_ms and per-query
                    # validation/query_XYZ/umbra_runtime_ms from the query
                    # cache, using the same instantiation set (incl.
                    # repetitions) the original run would have used. Mirrors
                    # check_output_correctness: per-query average umbra exec
                    # time, summed only if every executed query has umbra
                    # times recorded; otherwise None.
                    try:
                        _, instantiations, _ = (
                            self._get_instantiations_and_convert_to_arg_list(
                                scale_factor=scale_factor,
                                query_id=query_id,
                                repetitions=repetitions,
                                trace_mode=trace_mode,
                            )
                        )

                        umbra_rt_lists: DefaultDict[str, List[Optional[float]]] = (
                            defaultdict(list)
                        )
                        for inst in instantiations:
                            umbra_rt_lists[inst.query_id].append(
                                inst.umbra_exec_time_ms / 1000
                                if inst.umbra_exec_time_ms is not None
                                else None  # this is ns / although named as ms
                            )

                        avg_umbra_rts: Dict[str, float] = {}
                        for qid, rts in umbra_rt_lists.items():
                            non_none_rts = [rt for rt in rts if rt is not None]
                            if len(non_none_rts) == len(rts) and len(rts) > 0:
                                avg_umbra_rts[qid] = sum(non_none_rts) / len(rts)

                        if len(avg_umbra_rts) == len(umbra_rt_lists):
                            metrics["validation/total_umbra_runtime_ms"] = sum(
                                avg_umbra_rts.values()
                            )
                        else:
                            metrics["validation/total_umbra_runtime_ms"] = None

                        for qid in umbra_rt_lists:
                            q_3d_str = str(qid).zfill(3)
                            key = f"validation/query_{q_3d_str}/umbra_runtime_ms"
                            if key not in metrics:
                                metrics[key] = avg_umbra_rts.get(qid)
                    except Exception as e:
                        logger.warning(
                            f"Could not derive total_umbra_runtime_ms from query cache for cache entry {cache_path} with hash {hash} and payload {hash_payload}. Error was: {e}"
                        )
                        metrics["validation/total_umbra_runtime_ms"] = None

                if self.runtime_tracker is not None:
                    # add skipped time to the runtime tracker
                    self.runtime_tracker.add_skipped_time(
                        metrics["validation/runtime_seconds"]
                        if "validation/runtime_seconds" in metrics
                        else 0
                    )

                # write to logger
                if hasattr(result, "cmd_output") and result.cmd_output is not None:
                    logger.debug(result.cmd_output)
                else:
                    logger.debug("No cmd output in cache result (old cache)")

            else:
                if only_from_cache:
                    raise Exception(
                        f"Validation result not found in cache for key {compile_key_hash} and only_from_cache is set. Cache path: {cache_path}"
                    )

                # not found in cache - execute and validate
                if recompile_if_necessary_callback is not None:
                    # call recompile_if_necessary_callback to recompile if necessary (i.e. if compile result was from cache)
                    recompile_if_necessary_callback()

                validate_time_start = time.perf_counter()

                # check query-IDs are existing
                all_found = True
                rewritten_query_ids = []
                for q_id in query_id or []:
                    if q_id not in self.all_query_ids:
                        # check if llm accidently calls query with q prefix (e.g. q1 instead of 1) - if yes, auto-rewrite and continue with a warning, otherwise error out
                        if (q_id.startswith("q") or q_id.startswith("Q")) and q_id[
                            1:
                        ] in self.all_query_ids:
                            logger.warning(
                                f"Query ID {q_id} not recognized, but {q_id[1:]} is in the list of known query IDs. Auto rewriting it."
                            )
                            rewritten_query_ids.append(q_id[1:])
                            continue

                        # ERROR: query ID not recognized
                        all_found = False

                        validation_output = ValidationOutput(
                            result_message=f"Error: query_id {q_id} not recognized. Known query IDs: {self.all_query_ids}",
                            correct=False,
                            metrics=assemble_error(
                                log_info=log_info,
                                query_ids_executed=[],
                                exception=True,
                                query_id_not_recognized=q_id,
                            ),
                            trace_output=None,
                        )
                        logger.error(validation_output.result_message)

                        break
                    else:
                        rewritten_query_ids.append(q_id)

                # overwrite query id with potentially rewritten query ids (e.g. "q1" rewritten to "1") for downstream processing
                if query_id is not None:
                    query_id = rewritten_query_ids

                if all_found:
                    # get query instantiations and convert to arg list
                    args_list, instantiations, num_queries = (
                        self._get_instantiations_and_convert_to_arg_list(
                            scale_factor=scale_factor,
                            query_id=query_id,
                            repetitions=repetitions,
                            trace_mode=trace_mode,
                        )
                    )

                    # execute queries via callback
                    exec_result = exec_callback_fn(args_list, timeout_s=timeout)
                    ingest_ms = exec_result.ingest_time_ms
                    exec_query_results = exec_result.query_results
                    exec_trace = _format_query_traces(
                        exec_query_results, instantiations
                    )

                    if not skip_validate:
                        # validate output
                        validation_output = self._validate_query(
                            instantiations=instantiations,
                            query_results=exec_query_results,
                            scale_factor=scale_factor,
                            stdout=exec_result.out,
                            stderr=exec_result.err,
                            cmd=None,
                            stop_on_first_error=True,
                            trace_mode=trace_mode,
                            trace_data=exec_trace,
                            resp=exec_result.resp,
                        )

                    else:
                        logger.warning(
                            f"Skipping correctness validation as requested ({query_id=}, {scale_factor=}). Only return stdout/stderr."
                        )
                        validation_output = ValidationOutput(
                            result_message=f"stdout: {exec_result.out.rstrip()}\nstderr: {exec_result.err.rstrip()}\n{exec_result.resp}",
                            correct=True,
                            metrics=assemble_exec(
                                scale_factor=scale_factor,
                                num_queries_executed=num_queries,
                            ),
                            trace_output=None,
                        )

                    if ingest_ms is not None:
                        validation_output.result_message += (
                            f"Build time (ms): {ingest_ms}\n"
                        )

                else:
                    instantiations = []
                    ingest_ms = None
                    exec_result = None

                # extend metrics
                validation_output.metrics["validation/repetitions"] = repetitions
                validation_output.metrics["validation/instantiations"] = (
                    len(instantiations) / repetitions
                )
                validation_output.metrics["validation/runtime_seconds"] = (
                    time.perf_counter() - validate_time_start
                )
                validation_output.metrics["validation/ingest_time_ms"] = ingest_ms

                # create snapshot of current source code - use response hash as snapshot name
                if not self.do_not_cache and self.git_snapshotter is not None:
                    # Truncate result CSVs immediately before snapshotting so
                    # the snapshot captures the post-truncation tree and
                    # current_hash stays in sync with on-disk state on replay.
                    if self.max_snapshot_csv_size_mb is not None:
                        truncate_csvs_recursively(
                            self.workspace_path,
                            max_size_mb=self.max_snapshot_csv_size_mb,
                        )
                    _, commit = self.git_snapshotter.snapshot(hash)
                else:
                    commit = None

                if exec_result is not None:
                    cmd_output = f"stdout:\n{exec_result.out.rstrip()}\nstderr:\n{exec_result.err.rstrip()}\n{exec_result.resp}"
                    logger.debug(cmd_output)
                else:
                    cmd_output = None

                # write to cache
                if cache_path is not None and not self.do_not_cache:
                    assert commit is not None, (
                        "Git snapshot commit is None, but cache path is not None and do_not_cache is False. This should not happen, as the cache key is based on the git snapshot hash. Please check the GitSnapshotter implementation."
                    )
                    utils.dump_pickle(
                        cache_path,
                        ValidateCacheType(
                            outputs=validation_output.result_message,
                            success=validation_output.correct,
                            metrics=validation_output.metrics,
                            hash_payload=hash_payload,
                            snapshot_hash=commit,
                            trace_output=validation_output.trace_output,
                            cmd_output=cmd_output,
                        ),
                        do_not_cache=self.do_not_cache,
                    )
                    logger.debug(f"Saved validation result to cache: {cache_path}")

                # extract info
                metrics = validation_output.metrics
                msg = validation_output.result_message
                success = validation_output.correct
                trace_output = validation_output.trace_output

            metrics["validation/skip_validate"] = skip_validate

        # compute speedup
        if "validation/total_speedup" in metrics:
            total_speedup = f"{metrics['validation/total_speedup']:.2f}"
        else:
            total_speedup = "N/A"
        optimize_flags = other_config["optimize"]

        logger.info(
            f"Validate Tool Result: {'correct' if success else 'incorrect'} (Query ID: {query_id}, Scale Factor: {scale_factor}, Replayed from cache: {replayed_from_cache}, trace_mode: {trace_mode}, optim-flags: {optimize_flags}, num_threads: {num_threads_for_logging if num_threads_for_logging is not None else 'unknown'}) - Total Speedup: {total_speedup}x"
        )

        # truncate msg if too long for logging
        if len(msg) > 1000:
            shortened_msg = msg[:1000] + "...(truncated)"
        else:
            shortened_msg = msg

        with custom_span(
            f"exec_and_validate [result] ({'correct' if success else 'incorrect'}, {'replayed from cache' if replayed_from_cache else ''})",
            {
                "result": shortened_msg,
                "sql": query_id,
                "git snapshot": self.git_snapshotter.current_hash
                if self.git_snapshotter is not None
                else None,
            },
        ):
            return msg, success, metrics, replayed_from_cache, trace_output

    def _show_stdout(self, sf: float):
        return self.output_stdout_stderr or (
            sf == self.sf_list[-1] and self.output_stdout_stderr_for_max_sf
        )

    def _check_answer_from_cache(
        self,
        skip_validate: bool,
        other_config: Dict[str, Any],
        stop_on_first_error: bool,
        compile_key_hash: str,
        num_threads: int,
        query_batch: QueryBatch,
    ) -> Tuple[ValidateCacheType | None, Optional[Path], str, str | None]:
        if self.git_snapshotter is not None:
            hash_payload = {
                "query_batch": query_batch.to_dict(),
                "snapshotter_hash": self.git_snapshotter.current_hash,
                "skip_validate": skip_validate,
                "stop_on_first_error": stop_on_first_error,
                "wandb_pin_worker": self.wandb_pin_worker,
                "wandb_pin_core": PIN_CORE,
                "num_random_query_instantiations": self.num_random_query_instantiations,
                "compile_key_hash": compile_key_hash,
                "num_threads": num_threads,
                "db_storage": self.db_storage.value,
                **other_config,
            }

            stable_payload = utils.stable_json(hash_payload)
            hash = utils.sha256(stable_payload)

            if self.validate_cache_dir is None:
                cache_path = None
            else:
                cache_path = _cache_path_for_hash(self.validate_cache_dir, hash)

            # check validation-tool cache - replay validation result from val-tool cache if available
            if cache_path is not None and cache_path.exists():
                cached: Optional[ValidateCacheType] = utils.load_pickle(
                    cache_path, ValidateCacheType
                )
                assert cached is not None
                logger.debug(f"Loaded validation result from cache: {cache_path}")

                # restore snapshot hash
                self.git_snapshotter.restore(cached.snapshot_hash)

                return (
                    cached,
                    cache_path,
                    stable_payload,
                    hash,
                )
            else:
                # logger.info(f"No matching validation-tool cache found at {cache_path=}")
                pass

        else:
            logger.warning(
                "I don't know the current code version because GitSnapshotter is None. Hence I can't search for matching validation-tool cache."
            )
            cache_path = None
            stable_payload = ""
            hash = None

        return None, cache_path, stable_payload, hash

    def _get_queries_executed_from_queryid_arg(
        self, query_id: Optional[List[str]]
    ) -> List[str]:
        if isinstance(query_id, list):
            filtered_queries = query_id
        elif query_id is None:
            filtered_queries = self.all_query_ids
        else:
            raise ValueError(
                f"Unexpected query_id type: {type(query_id)}. Expected list or None."
            )

        assert len(filtered_queries) > 0
        return filtered_queries

    def _get_instantiations(
        self,
        scale_factor: float,
        query_id: Optional[List[str]],
        trace_mode: bool,
    ) -> Tuple[List[QueryInstantiation], int]:
        # determine which queries to execute
        executed_queries = self._get_queries_executed_from_queryid_arg(query_id)

        assert scale_factor in self.sf_list, (
            f"Scale factor {scale_factor} not in configured list."
        )

        # Sample query instantiations from cache
        instantions = self.query_cache.get_instantiations(
            scale_factor=scale_factor,
            query_id=executed_queries,
            num_samples=1 if trace_mode else None,
        )

        if len(executed_queries) > 0:
            assert len(instantions) > 0, (
                f"No query instantiations found for scale factor {scale_factor} and query_id {query_id}."
            )

        return instantions, len(executed_queries)

    def _get_instantiations_and_convert_to_arg_list(
        self,
        scale_factor: float,
        query_id: list[str] | None,
        repetitions: int,
        trace_mode: bool,
    ) -> Tuple[List[str], List[QueryInstantiation], int]:
        instantiations, num_queries = self._get_instantiations(
            scale_factor=scale_factor,
            query_id=query_id,
            trace_mode=trace_mode,
        )

        if trace_mode and isinstance(query_id, list):
            # in trace mode we only want to execute one instantiation per query to keep runtime low and avoid executing multiple times, as we mainly care about the trace output and not the validation confidence or runtime
            assert len(instantiations) == len(query_id), (
                f"In trace mode, expected exactly one instantiation per query. Got {len(instantiations)} instantiations for {len(query_id)} queries."
            )

        # Prepare arguments for implementation
        args_list = format_args_string(
            query_list=[inst.query_id for inst in instantiations],
            placeholder_list=[inst.placeholders for inst in instantiations],
        )

        # add repetitions to args list if repetitions > 1
        repeated_args_list = []
        repeated_instantiations = []
        for arg, inst in zip(args_list, instantiations):
            repeated_args_list.extend([arg] * repetitions)
            repeated_instantiations.extend([inst] * repetitions)

        return repeated_args_list, repeated_instantiations, num_queries

    def _validate_query(
        self,
        log_info: dict[str, str],
        instantiations: List[QueryInstantiation],
        query_results: List[QueryResult],
        stdout: str,
        stderr: str,
        scale_factor: float,
        cmd: Optional[str],
        trace_mode: bool,
        stop_on_first_error: bool = True,
        trace_data: str = "",
        resp: str = "",
    ) -> ValidationOutput:
        query_ids_executed = sorted(
            list(set([inst.query_id for inst in instantiations]))
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
            requested = ", ".join(f"Q{i.query_id}" for i in instantiations)
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
                    log_info=log_info,
                    query_ids_executed=query_ids_executed,
                    exception=True,
                    query_id=None,
                ),
            )

        if len(query_results) != len(instantiations):
            # Partial batch: the C++ side dropped some queries from the
            # request stream (e.g. malformed line that failed iss parsing).
            # The first un-returned slot is where the gap starts.
            crashed_idx = len(query_results)
            assert crashed_idx < len(instantiations), (
                f"Unexpectedly got more query results ({len(query_results)}) than instantiations ({len(instantiations)})."
            )
            crashed_qid = instantiations[crashed_idx].query_id
            crashed_at = (
                f" First missing slot: run #{crashed_idx + 1} (Q{crashed_qid})."
                if crashed_qid is not None
                else ""
            )
            return ValidationOutput(
                result_message=(
                    f"Error: unexpected number of query results{from_cmd_str}. "
                    f"Got {len(query_results)} but expected {len(instantiations)}."
                    f"{crashed_at}{resp_block}{per_query_block}"
                ),
                correct=False,
                metrics=assemble_error(
                    log_info=log_info,
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
            failed_qid = instantiations[first_failed_idx].query_id
            return ValidationOutput(
                result_message=(
                    f"Error: one or more queries threw an exception"
                    f"{from_cmd_str}.{resp_block}\n{per_query_errors}"
                ),
                correct=False,
                metrics=assemble_error(
                    log_info=log_info,
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
            for i, (inst, qr) in enumerate(zip(instantiations, query_results))
        ]

        # validate with duckdb
        out_path = self.workspace_path / "results"
        out_path.mkdir(parents=True, exist_ok=True)
        return check_output_correctness(
            log_info=log_info,
            instantiations=instantiations,
            measurements=measurements,
            out_path=out_path,
            cmd=cmd,
            stop_on_first_error=stop_on_first_error,
            all_query_ids=self.all_query_ids,
            stdout=stdout if self._show_stdout(scale_factor) else None,
            stderr=stderr if self._show_stdout(scale_factor) else None,
            trace_mode=trace_mode,
            trace_data=trace_data,
        )


def _cache_path_for_hash(validate_cache_dir: Path, hash: str) -> Path:
    return validate_cache_dir / f"{hash}.pkl"


# separate args by , and add double quotes around each arg (except for IN lists which start with '(')
def format_args_string(
    query_list: List[str], placeholder_list: List[Dict[str, Any]]
) -> List[str]:
    args_list = []
    for qid_str, placeholders in zip(query_list, placeholder_list):
        args_list.append(format_args_element(qid_str, placeholders))
    return args_list


def format_args_element(qid_str: str, placeholders: Dict[str, Any]) -> str:
    # generate random req-id
    # req_id = date_time + random int, to ensure uniqueness across different runs and queries
    req_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(1, 100000)}"

    # Don't add double quotes to IN lists (they start with '(')
    formatted_values = []
    for value in placeholders.values():
        if isinstance(value, str) and value.startswith("("):
            # IN list - don't add quotes
            formatted_values.append(value)
        else:
            # Regular value - add quotes
            formatted_values.append(f'"{value}"')

    return f"{qid_str} {req_id} {' '.join(formatted_values)}"


def _format_query_traces(query_results: list[QueryResult], instantiations: list) -> str:
    """Format per-query trace data from the query_results JSON array into a single string.

    query_results may be shorter than instantiations when the C++ child crashed
    or timed out before serialising all results — _validate_query reports that
    case separately, so here we just format whatever traces we got. The opposite
    (more results than instantiations) should be impossible: stale pipe data is
    caught by the batch_id check in hotpatch_proc.
    """
    assert len(query_results) <= len(instantiations), (
        f"Got more query_results ({len(query_results)}) than instantiations "
        f"({len(instantiations)}) — likely stale pipe data that bypassed the batch_id check."
    )
    parts = []
    for qr, inst in zip(query_results, instantiations):
        trace = qr.trace
        if trace is not None and trace.strip() != "":
            parts.append(f"--- Query {inst.query_id} ({qr.elapsed_ms}ms) ---\n{trace}")
    return "\n".join(parts)
