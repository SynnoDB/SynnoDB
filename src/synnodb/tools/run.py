from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from synnodb.cpp_runner.compiler.compiler_cached import CachedCompiler
from synnodb.cpp_runner.hotpatch.elf_build_id import read_build_id
from synnodb.cpp_runner.hotpatch.hotpatch_proc import (
    HotpatchProc,
    HotpatchProcRunResult,
)
from synnodb.cpp_runner.hotpatch.pool import HotpatchPool
from synnodb.cpp_runner.runtime_reset import warm_runtime_in_use
from synnodb.observability.logging.run_stats_collector import RunStatsCollector
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.tools.validate.query_validator_class import (
    ExecCallbackResult,
    QueryValidator,
)
from synnodb.tools.validate.run_and_check_queries import assemble_error
from synnodb.utils.core_utils import core_ids_to_env, resolve_target_cores
from synnodb.utils.json_utils import json_dumps
from synnodb.utils.utils import DBStorage
from synnodb.workloads.workload_provider import QueryBatch, WorkloadProvider

logger = logging.getLogger(__name__)


def _env_bool(name: str) -> bool:
    """Parse a boolean environment variable. Unset, empty, "0", "false", "no",
    and "off" (any case) are False; everything else is True. Avoids the trap where
    ``SYNNO_X=0`` reads as truthy under a bare ``os.environ.get``."""
    return os.environ.get(name, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _cgroup_launch_policy(memory_limit_bytes: int) -> Tuple[Dict[str, object], str]:
    """Resolve the opt-in cgroup launch policy (env-driven) into HotpatchProc kwargs
    and a pool-key suffix.

    The suffix MUST be appended to the runner pool key: runners with different launch
    policies must never be shared, or a warm uncapped runner could be reused for a
    capped, require-cgroup run and silently bypass both the memory ceiling and the
    fail-closed check. The shared-parent slice and its budget are part of the policy too:
    a runner warmed under the old per-orchestrator parent must not be reused once a shared
    parent (or a different one / budget) is configured, or it would bypass the aggregate
    slice. Returns ``({}, "")`` when the cgroup path is disabled.
    """
    if not _env_bool("SYNNO_ENABLE_CGROUP"):
        return {}, ""
    require = _env_bool("SYNNO_REQUIRE_CGROUP")
    parent = os.environ.get("SYNNO_CGROUP_PARENT", "").strip()
    parent_max = os.environ.get("SYNNO_CGROUP_PARENT_MAX", "").strip()
    kwargs: Dict[str, object] = {
        "memory_max_bytes": memory_limit_bytes,
        "require_cgroup": require,
    }
    suffix = (
        f"|cgroup_max={memory_limit_bytes}|require={require}"
        f"|parent={parent}|parent_max={parent_max}"
    )
    return kwargs, suffix


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


def _run_outcome_label(
    success: bool,
    metrics: Dict,
    stdout: str | None,
    stderr: str | None,
    msg: str | None,
) -> str:
    """Describe a run's outcome for the activity-summary line the supervisor reads.

    The label previously collapsed EVERY non-compile failure to "incorrect query
    output", so a crash, timeout, or OOM was reported to the supervisor as a wrong
    answer - and the supervisor then wrongly faulted the agent for dismissing a
    transient infra failure as such. Distinguish a genuine correctness mismatch
    from an execution failure using the metrics' error flag (assemble_error sets
    ``validation/error=True`` for a thrown/killed/missing-result run and False for a
    real answer mismatch), and name a timeout / OOM / crash specifically when the
    captured output shows it.
    """
    if success:
        return "success"

    # A run that threw, was killed, or produced no result is an execution failure,
    # not a wrong answer. Only a clean run whose output disagrees with the reference
    # is "incorrect query output".
    if not metrics.get("validation/error"):
        return "incorrect query output"

    haystack = "\n".join(part for part in (stderr, stdout, msg) if part).lower()
    if "bad_alloc" in haystack or "out of memory" in haystack or "oom" in haystack:
        return "run failed (out of memory)"
    if "timeout" in haystack or "timed out" in haystack:
        return "run failed (timeout)"
    if "signal" in haystack or "sigsegv" in haystack or "sigkill" in haystack:
        return "run failed (crash)"
    return "run failed (execution error)"


class RunTool:
    """Runs the database and executes a query by id"""

    parse_out_and_validate_output: bool = True

    def __init__(
        self,
        workload_provider: WorkloadProvider,
        cwd: Path,
        dataset_name: str,
        base_parquet_dir: str
        | Path,  # must contain one subdir per subset, each holding that subset's parquet files: the sampling-fraction convention base_parquet_dir/fraction<f>/ (e.g. fraction1, fraction0.02) or the legacy scale-factor convention base_parquet_dir/sf<N>/ (e.g. sf1, sf10). See find_sf_dir.
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
        num_threads: int = 1,  # run-wide DEFAULT degree of parallelism (a resolved core count, e.g. from resolve_target_cores(ctx.threads)); a per-stage override raises/lowers it via set_active_num_threads
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
        # One canonical thread count. ``default_num_threads`` is the run-wide value
        # (resolved once from SynnoDB(threads=N)); ``_active_num_threads`` is the
        # per-stage override the conversation engine sets for a stage's whole span
        # (LLM ``run`` calls, post-stage validation, benchmark) and clears afterwards.
        self.default_num_threads = num_threads
        self._active_num_threads: int | None = None
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

    @property
    def num_threads(self) -> int:
        """The degree of parallelism in effect: the active per-stage override if one is
        set, else the run-wide default. Both generation runs and the LLM's own ``run``
        tool calls read through this, so a stage-scoped override governs the whole span."""
        return (
            self._active_num_threads
            if self._active_num_threads is not None
            else self.default_num_threads
        )

    def set_active_num_threads(self, num_threads: int | None) -> None:
        """Set (``int``) or clear (``None``) the per-stage thread override. The conversation
        engine applies this on stage entry and restores the previous value after the stage's
        post-stage validation, so every run within the stage shares one thread count."""
        self._active_num_threads = num_threads

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
        force_live: bool = False,  # bypass the validation cache so every query is executed live (paired with force_compile by the publish gate)
        external_call: bool = False,
        current_git_snapshot: Optional[
            str
        ] = None,  # for external instrumentation: e.g. from benchmarking script (will not use git snapshotter)
        echo_output: bool = False,  # print stdout and co directly
        num_threads: int
        | None = None,  # override the effective thread count for THIS call only (else the active per-stage value, else the run default)
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

        # The single thread count in effect for this run: the explicit per-call override,
        # else the active per-stage value, else the run default (RunTool.num_threads).
        current_num_threads = (
            num_threads if num_threads is not None else self.num_threads
        )
        # Concrete cores are derived only here (a pin boundary), through the same
        # resolve_target_cores / core_ids_to_env helpers the router uses at serving time, so
        # generation and serving agree on the exact cores. Serial (1 thread) pins nothing -
        # CORE_IDS="1", the engine's serial fast path - while a multi-threaded run resolves
        # exactly current_num_threads pinned cores (clamped to the machine; count and cores
        # kept in lockstep).
        if current_num_threads > 1:
            current_num_threads, current_core_ids = resolve_target_cores(
                current_num_threads
            )
        else:
            current_core_ids = None
        current_parallelism = current_num_threads > 1

        extra_env = dict()
        extra_env["CORE_IDS"] = core_ids_to_env(current_core_ids)

        logger.info(
            f"Run with: {query_ids=} {mode=} {self.dataset_name=} {trace_mode=} {optimize=} {self.base_parquet_dir=} num_threads={current_num_threads} mem_limit={self.memory_budget_mb}"  # type: ignore
        )

        # Delete any result files written by a previous run so we never
        # accidentally read stale results if the child crashes before writing
        # fresh ones. This start-of-run sweep is the authoritative stale-guard: it is
        # the one that survives this process being killed mid-run.
        delete_result_files(self.cwd)

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
            metrics["validation/replayed_from_cache"] = False
            metrics["validation/compile_with_optimize"] = optimize
            metrics["validation/trace_mode"] = trace_mode
            metrics["validation/run_mode"] = mode.value
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

        # Guard the warm runtime against a concurrent resync for this whole run (see runtime_reset).
        result_list = []
        try:
            with warm_runtime_in_use():
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
                        current_num_threads=current_num_threads,
                        run_tool_mode=mode,
                        force_live=force_live,
                    )  # TODO: compile used cache does not update - e.g. in the first iteration it will compile, pass info to second iteration.
                    result_list.append(result)

                    if not result.success:
                        # early return
                        break
        finally:
            # Sweep any stragglers the per-read deletes cannot reach: an early break
            # (stop_on_first_error) leaves the batch's unread results behind, and a skip_validate
            # run never reads them at all. Keeps the workspace clean between runs.
            delete_result_files(self.cwd)

        return result_list[-1]

    def validate_for_publish(
        self,
        query_ids: list[str],
        *,
        mode: RunToolMode = RunToolMode.EXHAUSTIVE,
        optimize: bool = True,
    ):
        """Validate *query_ids* and return a ``ValidationReceipt`` for the publish gate. The
        receipt records the freshly-compiled build's build-ids, the concrete instantiations
        validated, the scale factors covered, and a pass/fail verdict.

        ``force_compile=True`` rebuilds the binary that publish then ships (so the recorded
        build-ids match what is copied). ``force_live`` is disabled: ideally it would skip the
        validation cache so a since-broken engine could not be blessed by an earlier cached
        success, but forcing a live re-execution re-snapshots an already-captured build and trips
        the snapshot-name uniqueness assert. Re-enable once snapshot() tolerates re-snapshotting
        an existing content-addressed name.
        """
        from synnodb.workloads.validation_receipt import (
            FAIL,
            PASS,
            PLANE_PARQUET,
            PLANE_SHM,
            ValidatedQuery,
            ValidationReceipt,
            engine_build_ids,
        )

        # A receipt must attest to a real correctness check. When answer validation is off
        # (no query_validator, or parse_out_and_validate_output=False as a VALIDATE_OFF run sets),
        # run_worker reports success after merely running the binary - it never compares answers.
        # Minting a "pass" from that would let a wrong-answer engine publish, so refuse instead.
        if self.query_validator is None or not self.parse_out_and_validate_output:
            raise RuntimeError(
                "validate_for_publish cannot mint a publish receipt: answer validation is not "
                "active (needs a query_validator and parse_out_and_validate_output=True). A "
                "receipt must prove correctness, never a bare run."
            )

        result = self.run_worker(
            mode=mode,
            optimize=optimize,
            query_ids=query_ids,
            force_compile=True,
            force_live=False,
        )

        # Enumerate the concrete instantiations the run validated. produce_workload is
        # deterministic (a fixed RNG seed) and is the same function run_worker drives internally,
        # so re-deriving it here records exactly what was executed. Threads/cores do not affect the
        # generated bindings, only the system config, so a 1-thread enumeration is faithful.
        batches = self.workload_provider.produce_workload(
            run_mode=mode, num_threads=1, core_ids=None, query_ids=query_ids
        )
        bindings_by_qid: dict[str, list[dict]] = {}
        scale_factors: list[float] = []
        data_sources: set = set()
        for batch in batches:
            sf = getattr(batch.exec_settings, "scale_factor", None)
            if sf is not None and not any(abs(sf - s) < 1e-9 for s in scale_factors):
                scale_factors.append(float(sf))
            data_sources.add(getattr(batch.exec_settings, "data_source", None))
            for entry in batch.query_list:
                seen = bindings_by_qid.setdefault(entry.query_id, [])
                if entry.placeholders not in seen:
                    seen.append(dict(entry.placeholders))
        validated_queries = tuple(
            ValidatedQuery(qid, tuple(binds)) for qid, binds in bindings_by_qid.items()
        )

        # Record the plane the engine actually ingested over during this validation: a
        # DuckDB-native subset is staged into /dev/shm (the shm hot-load plane), everything else
        # streams from parquet. Publishing keys the served plane off this, so it must be truthful.
        from synnodb.utils.utils import DataSource

        plane = PLANE_SHM if DataSource.DUCKDB in data_sources else PLANE_PARQUET

        snapshotter = getattr(self.query_validator, "git_snapshotter", None)
        snapshot_id = (
            getattr(snapshotter, "current_hash", None) if snapshotter else None
        )

        return ValidationReceipt(
            snapshot_id=snapshot_id,
            build_ids=engine_build_ids(self.cwd),
            validated_queries=validated_queries,
            coverage_policy=(
                f"deterministic workload generator (fixed seed), {mode.value} mode; proves the "
                "listed bindings per query, not every possible template value"
            ),
            data_planes=(plane,),
            dataset=self.dataset_name,
            validated_scale_factors=tuple(scale_factors),
            mode=mode.value,
            live_run=True,
            verdict=PASS if result.success else FAIL,
        )

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
        current_num_threads: int,
        force_live: bool = False,
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

        # Opt-in hard resident-memory ceiling via a per-runner cgroup v2 (A2). Uses
        # the same budget as the RLIMIT_AS value but enforces RSS, so a breach is
        # OOM-killed as a group instead of taking down the host. SYNNO_REQUIRE_CGROUP
        # makes a host without cgroup delegation fail closed rather than silently
        # dropping to RLIMIT_AS only. The policy suffix keeps the pool key distinct so
        # a warm runner from a different policy is never reused (see helper).
        cgroup_kwargs, cgroup_key_suffix = _cgroup_launch_policy(
            hp_kwargs["memory_limit_bytes"]
        )
        hp_kwargs.update(cgroup_kwargs)
        pool_key += cgroup_key_suffix

        # A libloader.so source change alters how the loader ingests its input, so
        # the engine's resident (loader-owned) input tables are stale - a defect
        # the in-process hotpatch cannot fix (the builder restart in pipeline.hpp
        # only refreshes libbuilder.so). Fingerprint the loader plugin's build-id
        # so the pool restarts the whole engine when it changes. read_build_id
        # returns None when the .so is not built yet, which the pool treats as
        # "no signal".
        loader_build_id = read_build_id(os.path.join(self.cwd, "build", "libloader.so"))

        runner = HotpatchPool.get(
            pool_key,
            factory=lambda: HotpatchProc(cmd, **hp_kwargs),
            fingerprint=loader_build_id,
        )

        # A DuckDB batch ships only ``subset.duckdb`` (no parquet), so EVERY execution path must
        # stage it into /dev/shm and point ``SYNNODB_SHM_INGEST`` at it - both the validated path
        # and the benchmark/no-validator path below. Otherwise the generated loader falls back to
        # reading ``<table>.parquet``, finds nothing, and the run fails instead of executing.
        # Staging is done lazily at execute time via ``_run_env_with_optional_shm_ingest`` (see
        # its docstring), so a cached replay stages nothing and the pid-scoped ingest path stays
        # out of the hashed ``batch.extra_env``.
        subset_db = _duckdb_subset_db(batch)

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

            def execute_fn(*, args_list, timeout_s):
                return call_hotpatch_proc(
                    runner=runner,
                    args_list=args_list,
                    timeout_s=timeout_s,
                    extra_env=_run_env_with_optional_shm_ingest(extra_env, subset_db),
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
                force_live=force_live,
            )

            msg = val_result.message
            success = val_result.success
            metrics = val_result.metrics
            replayed_from_cache = val_result.replayed_from_cache
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
            replayed_from_cache = False
            logger.warning(
                "No query validator provided, just executing the query without validation!"
            )

            args_list = [entry.query_args for entry in batch.query_list]

            run_result: ExecCallbackResult = call_hotpatch_proc(
                runner=runner,
                extra_env=_run_env_with_optional_shm_ingest(extra_env, subset_db),
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
        metrics["validation/replayed_from_cache"] = replayed_from_cache
        metrics["validation/compile_with_optimize"] = optimize
        metrics["validation/trace_mode"] = trace_mode
        metrics["validation/run_mode"] = run_tool_mode.value
        metrics["validation/external_call"] = external_call
        metrics["validation/parallelism"] = current_parallelism
        metrics["validation/core_ids"] = current_core_ids
        # The DuckDB baseline and the bespoke (SynnoDB) engine execute the batch at
        # the same resolved serving thread count. Log them as distinct per-engine
        # metrics so the live dashboard can surface the count for the fairness of the
        # comparison and flag any future drift between the two engines loudly.
        metrics["validation/duckdb_num_threads"] = current_num_threads
        metrics["validation/bespoke_num_threads"] = current_num_threads
        if self.run_stats_collector is not None:
            self.run_stats_collector.log_metrics_callback(
                metrics, log_and_increment=True
            )

            self.run_stats_collector.add_to_activity_summary(
                f"Run Tool called: {_run_outcome_label(success, metrics, stdout, stderr, msg)}"
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


def delete_result_files(workspace_path: Path):
    # Delete every prior result file - the exact Arrow result the engine writes now
    # (result_<req_id>.arrow) and the legacy CSV. Result names are keyed by request id and so
    # are stable across iterations, so an uncleaned file from a crashed run would otherwise be
    # silently validated as the current run's output. One recursive walk, filtered by suffix.
    result_files = [
        p for p in workspace_path.rglob("result*") if p.suffix in (".arrow", ".csv")
    ]
    if result_files:
        logger.info(f"Deleting existing result files ({len(result_files)} files).")
        for f in result_files:
            f.unlink()


def _duckdb_subset_db(batch: QueryBatch) -> Path | None:
    """The ``subset.duckdb`` a DuckDB-native batch should stage into ``/dev/shm``, or None for
    every other data source (whose loader reads parquet and needs no staging). Reads only - the
    staging itself happens lazily in the execute callback so a cached replay stages nothing, and
    the pid-scoped ingest path stays out of the hashed batch."""
    from synnodb.utils.utils import DataSource
    from synnodb.workloads.workload_spec import SUBSET_DUCKDB_FILENAME

    exec_settings = batch.exec_settings
    if getattr(exec_settings, "data_source", None) != DataSource.DUCKDB:
        return None
    return Path(exec_settings.parquet_dir) / SUBSET_DUCKDB_FILENAME  # type: ignore[attr-defined]


def _run_env_with_optional_shm_ingest(
    base_env: dict[str, str], subset_db: Path | None
) -> dict[str, str]:
    """The env for a single execution. For a non-DuckDB batch (``subset_db is None``) this is
    ``base_env`` unchanged. For a DuckDB-native batch it is ``base_env`` plus a freshly staged
    ``SYNNODB_SHM_INGEST`` pointing at the /dev/shm Arrow segment the generated loader maps
    zero-copy. Call once per execution: staging happens here (lazily, so a cached replay never
    stages) and the pid-scoped ingest path is added to the run env only, never to the hashed
    ``batch.extra_env``, keeping the validate cache replayable across processes."""
    if subset_db is None:
        return base_env
    from synnodb.cpp_runner.shm_stage import stage_subset_duckdb_to_shm

    ingest_dir = stage_subset_duckdb_to_shm(subset_db)
    return {**base_env, "SYNNODB_SHM_INGEST": str(ingest_dir)}


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
            # No "Ingest ms:" line and nothing cached. That is expected when the
            # ingest/build phase started and then failed: the failure is surfaced
            # to the agent to fix (ingest_time_ms stays -1). We only treat it as a
            # hard "should not happen" when there is no sign the pipeline ran at
            # all (e.g. the binary never started) — which indicates a harness bug.
            #
            # Detection is usecase-independent: every pipeline prints "<stage>
            # start" markers (OLAP "builder start"), and a stage whose plugin code throws is reported by
            # the framework stage runner as "<so> stage threw ...".
            combined = (out + "\n" + err).lower()
            pipeline_ran = (
                "builder start" in combined
                or "writer start" in combined
                or "loader start" in combined
                or "stage threw" in combined
            )
            assert pipeline_ran, (
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
