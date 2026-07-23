import logging
import os
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from synnodb.ram_check import RamCheck
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils.sql_utils import extract_order_by_columns
from synnodb.utils.utils import (
    DataSource,
    DBStorage,
    ServeFrom,
    is_persistent_storage,
)
from synnodb.workloads.system_factory import System
from synnodb.workloads.workload_provider import (
    DEFAULT_NUM_INSTANTIATIONS,
    ExecSettings,
    GeneralSystemConfig,
    QueryBatch,
    QueryEntry,
    Workload,
    WorkloadId,
    WorkloadProvider,
    format_args_element,
)
from synnodb.workloads.workload_spec import (
    find_sf_dir,
    get_workload_spec,
)

logger = logging.getLogger(__name__)


# Fraction of memory_budget_mb that goes to the generated engine's paged
# frame pool. The remainder is implicit headroom for mmap_col regions and
# other working memory; both are bounded together by RLIMIT_AS.
FRAME_POOL_SHARE = 0.60


def allowed_data_sources(system: System, db_storage: DBStorage) -> set[DataSource]:
    """Data sources a given system can use on a given storage medium.

    DuckDB reads either flat (its native materialized tables) or, on disk, parquet views. The
    bespoke engine additionally has its own on-disk storage plan. In-memory rules out anything
    that needs disk (parquet views, the bespoke storage plan) - but the engine can still load
    flat in memory. ``DUCKDB`` (DuckDB-native subsets, ingested over the shm plane / materialized
    flat from a ``subset.duckdb``) is an in-memory-only representation for both systems - the SSD
    plane has no shm ingest branch yet. Returns an empty set for systems not modelled here.
    """
    persistent = is_persistent_storage(db_storage)
    if system == System.DUCKDB:
        return (
            {DataSource.FLAT, DataSource.PARQUET}
            if persistent
            else {DataSource.FLAT, DataSource.DUCKDB}
        )
    if system == System.BESPOKE:
        return (
            {DataSource.FLAT, DataSource.BESPOKE, DataSource.PARQUET}
            if persistent
            else {DataSource.FLAT, DataSource.BESPOKE, DataSource.DUCKDB}
        )
    return set()


def validate_storage_combo(
    system: System, db_storage: DBStorage, data_source: DataSource
) -> None:
    """Reject a (system, storage medium, data source) triple the system cannot run.

    Validity is system-specific: e.g. DuckDB in-memory can only be flat, whereas the bespoke
    engine in-memory can be flat or bespoke.
    """
    allowed = allowed_data_sources(system, db_storage)
    if data_source not in allowed:
        raise ValueError(
            f"{system} on db_storage={db_storage.value} cannot use "
            f"data_source={data_source.value}; allowed: "
            f"{sorted(s.value for s in allowed)}."
        )


@dataclass
class OLAPExecSettings(ExecSettings):
    scale_factor: float
    db_storage: DBStorage
    parquet_dir: Path
    disk_db_dir: Path | None
    data_source: DataSource

    def __post_init__(self) -> None:
        # These settings drive a bespoke-engine run, so validate the source against that system.
        validate_storage_combo(System.BESPOKE, self.db_storage, self.data_source)


class OLAPWorkloadProvider(WorkloadProvider):
    def __init__(
        self,
        benchmark: "Workload | WorkloadId | str",
        base_parquet_dir: Path,
        db_storage: DBStorage,
        bespoke_ssd_storage_dir: Path | None = None,
        query_cache_dir: Path | None = None,
        query_ids: list[str] | None = None,
        num_instantiations: int = DEFAULT_NUM_INSTANTIATIONS,
        **kwargs,
    ):
        # Accept a registered workload name / WorkloadId, an enum member from a
        # workload package, or a plain string. Normalize plain strings so `.value`
        # resolves the spec uniformly.
        if isinstance(benchmark, str) and not isinstance(benchmark, WorkloadId):
            benchmark = WorkloadId(benchmark)
        self.benchmark = benchmark
        # The workload as data. All per-workload values (tables, schema, sql, query
        # catalog, scale factors, parameter generation) are read from this spec rather
        # than switched on the benchmark enum — so a new workload is a registered spec.
        self.spec = get_workload_spec(self.benchmark.value)
        self.query_cache_dir = query_cache_dir
        self.base_parquet_dir = base_parquet_dir
        self.db_storage = db_storage
        self.dataset_tables = list(self.spec.tables)
        self.dataset_name = self.spec.dataset_name
        self.dataset_schema = self.spec.schema()
        self.bespoke_ssd_storage_dir = bespoke_ssd_storage_dir

        # Scope the provider to the requested query subset (e.g. ["1"]). Everything
        # downstream — scaffolding (queryX files, queries.md, query_impl, args_parser)
        # and the run/validate defaults — reads provider.query_ids, so this is the
        # single place that confines a run to exactly the requested queries. When no
        # subset is given, default to the workload's full catalog.
        scoped_query_ids = _resolve_query_subset(
            all_ids=list(self.spec.all_query_ids),
            requested=query_ids,
            benchmark=self.benchmark.value,
        )

        # Scale factor used for BENCHMARK-mode runs. Conversations can override this
        # via set_benchmark_sf to drive perf/large-scale checks off the workload
        # provider (exec-config) rather than passing fixed scale factors around.
        self.benchmark_sf: float = self.spec.benchmark_sf

        # Number of parameter instantiations generated per query for the correctness
        # sweep (FAST_CHECK / EXHAUSTIVE). Defaults to DEFAULT_NUM_INSTANTIATIONS.
        self.num_instantiations: int = num_instantiations

        # BENCHMARK-mode knobs (distinct parameter sets and repetitions per query),
        # configurable from the benchmarker CLI via set_benchmark_instantiations /
        # set_benchmark_repetitions. The correctness sweep above uses
        # num_instantiations instead; these only affect BENCHMARK runs.
        self.benchmark_instantiations: int = 1
        self.benchmark_repetitions: int = 3

        super().__init__(
            benchmark_name=self.benchmark.value,
            query_ids=scoped_query_ids,
            sql_dict=self.spec.sql_dict(),
            **kwargs,
        )

    def set_benchmark_sf(self, sf: float) -> None:
        """Override the scale factor emitted for BENCHMARK-mode workloads."""
        self.benchmark_sf = sf

    def set_num_instantiations(self, n: int) -> None:
        """Override the number of parameter instantiations per query in the sweep."""
        self.num_instantiations = n

    def set_benchmark_instantiations(self, instantiations: int) -> None:
        """Override the number of distinct parameter sets emitted in BENCHMARK mode."""
        self.benchmark_instantiations = instantiations

    def set_benchmark_repetitions(self, repetitions: int) -> None:
        """Override the number of repetitions per query emitted in BENCHMARK mode."""
        self.benchmark_repetitions = repetitions

    def preflight_ram_check(self) -> RamCheck | None:
        """Measure the largest scale-factor dataset this workload could load into RAM.

        Only in-memory runs ingest the parquet fully; disk-backed storage has
        nothing to gate. An in-memory run holds one scale factor at a time, so
        the peak requirement is the largest of the workload's own scale factors
        whose dataset is present on disk. Candidate scale factors come from the
        workload spec (the per-mode ladders, the benchmark SF, and the large-scale
        check SF) - a stray ``sf*`` directory the spec does not reference is not
        something a run would load, so it is not gated on. Scale factors whose
        parquet is not on disk cannot be measured and are ignored."""
        if self.db_storage != DBStorage.IN_MEMORY:
            return None
        datasets = list(self._datasets_on_disk())
        if not datasets:
            logger.warning(
                "RAM preflight skipped: none of the workload's scale-factor "
                "datasets are present under %s",
                self.base_parquet_dir,
            )
            return None
        label, paths = max(
            datasets, key=lambda dp: sum(p.stat().st_size for p in dp[1])
        )
        return RamCheck.measure(label, paths)

    def prepare(self) -> None:
        """Lazily downscale any fractional subsets this workload needs but does not yet have.

        For a DuckDB-sourced workload the spec carries a :class:`DuckDBSubsetSource` describing how
        to downscale from the frozen source. Sync materializes only the full ``fraction1`` benchmark
        subset; the fractional fast-check rungs are built here, on demand at run start, from that
        retained snapshot - so a plain re-ingest never re-downscales. Idempotent: fractions already
        present (and current) are skipped, so calling it every run is cheap. A no-op for built-ins
        and plain BYO-parquet, whose subsets are already on disk (``duckdb_source is None``)."""
        source = self.spec.duckdb_source
        if source is None:
            return

        # A subset dir left from a different source version is stale; rebuild every needed fraction
        # in that case. On the happy path sync keeps the manifest and spec versions equal, so this
        # falls through to the plain presence check below.
        from synnodb.workloads.byo_workload import (
            materialize_duckdb_subsets,
            read_manifest_dataset_version,
        )

        stale = (
            self.spec.dataset_version is not None
            and read_manifest_dataset_version(self.base_parquet_dir)
            != self.spec.dataset_version
        )

        missing: list[float] = []
        for sf in sorted(f for f in self._candidate_sfs() if f < 1.0):
            sf_dir = find_sf_dir(self.base_parquet_dir, sf)
            present = sf_dir is not None and all(
                p.exists() for p in self.spec.subset_files(sf_dir)
            )
            if stale or not present:
                missing.append(sf)
        if not missing:
            return

        logger.info(
            "Preparing workload %s: downscaling fractional subsets %s on demand",
            self.spec.name,
            ", ".join(f"{f:g}" for f in missing),
        )
        materialize_duckdb_subsets(
            source, missing, self.base_parquet_dir, self.spec.serve_from
        )

    def _candidate_sfs(self) -> set[float]:
        """Every scale factor the workload could load across its run modes: the
        per-mode ladders, the (possibly overridden) benchmark SF, and the
        large-scale check SF."""
        sfs = {
            self.benchmark_sf,
            *self.spec.fast_check_sfs,
            *self.spec.exhaustive_sfs,
            *self.spec.ingest_sfs,
        }
        if self.spec.large_check_sf is not None:
            sfs.add(self.spec.large_check_sf)
        return sfs

    def _datasets_on_disk(self) -> Iterator[tuple[str, list[Path]]]:
        """``(label, parquet paths)`` for every candidate scale factor whose
        dataset is fully present under the parquet root - a directory holding a
        parquet file for every table the workload loads. Scale factors with no
        directory, or an incomplete one, are skipped."""
        for sf in sorted(self._candidate_sfs()):
            sf_dir = find_sf_dir(self.base_parquet_dir, sf)
            if sf_dir is None:
                continue
            if self.spec.serve_from == ServeFrom.DUCKDB:
                # DuckDB-native subset: a single ``subset.duckdb`` stands in for the parquet files.
                subset_db = sf_dir / "subset.duckdb"
                if subset_db.exists():
                    yield sf_dir.name, [subset_db]
                continue
            paths = [sf_dir / f"{table}.parquet" for table in self.spec.tables]
            if all(p.exists() for p in paths):
                yield sf_dir.name, paths

    def produce_workload(
        self,
        run_mode: RunToolMode,
        query_ids: list[str] | None,
        num_threads: int,
        core_ids: list[int] | None,
    ) -> list[QueryBatch]:
        # Ensure lazily-downscaled subsets exist before any fraction is resolved below. Idempotent
        # and cheap once present; a safety net for entry points that build a provider without going
        # through main()'s run-start prepare() (benchmark CLI, optimize, publish-zip).
        self.prepare()

        if query_ids is None or len(query_ids) == 0:
            queries_to_generate = self.query_ids
        else:
            queries_to_generate = query_ids

        if run_mode == RunToolMode.FAST_CHECK:
            instantiations = self.num_instantiations
            repetitions = (
                1  # each parameter instantiation runs once (only BENCHMARK repeats)
            )
            scale_factors = self.spec.scale_factors_for(run_mode)

        elif run_mode == RunToolMode.EXHAUSTIVE:
            instantiations = self.num_instantiations
            repetitions = (
                1  # each parameter instantiation runs once (only BENCHMARK repeats)
            )
            scale_factors = self.spec.scale_factors_for(run_mode)

            if scale_factors[-1] != self.benchmark_sf:
                scale_factors.append(self.benchmark_sf)

        elif run_mode == RunToolMode.BENCHMARK:
            # instantiations / repetitions / SF are configurable (exec-config driven)
            instantiations = self.benchmark_instantiations
            repetitions = self.benchmark_repetitions
            scale_factors = [self.benchmark_sf]
        elif run_mode == RunToolMode.INGEST:
            instantiations = self.spec.ingest_instantiations
            repetitions = (
                1  # each parameter instantiation runs once (only BENCHMARK repeats)
            )
            scale_factors = self.spec.scale_factors_for(run_mode)

        else:
            raise ValueError(f"Unknown run mode: {run_mode}")

        query_batch_list = []
        rnd = random.Random(42)
        for scale_factor in scale_factors:
            # Fresh per scale factor: each batch owns its env. A single shared dict would leave
            # every batch pointing at the last scale factor's STORAGE_DIR (they hold the same
            # object, mutated in place each iteration).
            extra_env: dict[str, str] = {}
            if self.db_storage in [DBStorage.SSD, DBStorage.LABSTORE]:
                assert self.bespoke_ssd_storage_dir is not None
                storage_dir = self.bespoke_ssd_storage_dir / f"sf{scale_factor}"
                extra_env["STORAGE_DIR"] = str(storage_dir) + os.sep
                if self.memory_limit_mb is not None:
                    # Apply the frame-pool / mmap-headroom split here so the generated
                    # C++ only sees its directly-usable frame budget.
                    buffer_pool_mb = int(self.memory_limit_mb * FRAME_POOL_SHARE)
                    extra_env["BUFFER_POOL_MB"] = str(buffer_pool_mb)

                storage_dir.mkdir(parents=True, exist_ok=True)
                # create sentinel file to indicate that this is a bespoke storage dir (so that it can be cleaned up without accidentally deleting other files)
                (storage_dir / ".bespoke_storage_dir").touch()
            else:
                storage_dir = None

            if storage_dir is not None:
                # SSD/persistent: the bespoke engine's on-disk storage plan. DuckDB-native subsets
                # have no shm/parquet on the SSD plane yet, so reject that combination clearly.
                if self.spec.serve_from == ServeFrom.DUCKDB:
                    raise ValueError(
                        f"Workload {self.spec.name!r} uses DuckDB-native subsets, which are "
                        "in-memory only (the SSD plane has no shm ingest branch). Run it with "
                        "in_memory storage, or register it via the parquet fallback for SSD."
                    )
                data_source = DataSource.BESPOKE
            elif self.spec.serve_from == ServeFrom.DUCKDB:
                # In-memory DuckDB-native: the subset is a ``subset.duckdb`` in the subset dir; the
                # engine ingests it over shm and the oracle materializes flat tables from it.
                data_source = DataSource.DUCKDB
            else:
                data_source = DataSource.FLAT

            # assemble parquet path where data is loaded from - resolve the subset directory
            # under the parquet root (sampling-fraction ``fraction<f>`` or legacy ``sf<N>``)
            subset_dir = find_sf_dir(self.base_parquet_dir, scale_factor)
            if subset_dir is None:
                raise FileNotFoundError(
                    f"No subset directory for fraction/SF {scale_factor:g} under "
                    f"{self.base_parquet_dir} for workload {self.spec.name!r}."
                )
            parquet_dir = subset_dir.as_posix() + "/"
            assert parquet_dir.endswith("/"), (
                f"Parquet directory must end with '/': {parquet_dir}"
            )
            cli_call_args_str = f"{parquet_dir}"

            query_list = []
            sql_set = (
                set()
            )  # for debugging - track generated SQL queries to check for duplicates

            gen_attempts = 100

            for inst_idx in range(instantiations):
                for query_id in queries_to_generate:
                    for _ in range(
                        gen_attempts
                    ):  # try up to 100 times to generate a unique query (in case of random generation leading to duplicates)
                        _, sql, placeholders = self._get_query_gen_fn()(
                            query_name=f"Q{query_id}", rnd=rnd
                        )

                        if sql in sql_set:
                            continue
                        else:
                            sql_set.add(sql)
                            break
                    else:
                        logger.debug(
                            f"Failed to generate unique SQL for query_id={query_id} (inst_idx={inst_idx}) after {gen_attempts} attempts, skipping this instantiation"
                        )
                        continue

                    # Extract order by information
                    order_by_info = extract_order_by_columns(sql)

                    query_entry = QueryEntry(
                        query_id=str(query_id),
                        sql=sql,
                        benchmark=self.benchmark,
                        query_args="",
                        placeholders=placeholders,
                        order_by_info=order_by_info,
                        num_reps=repetitions,
                    )

                    for rep in range(repetitions):
                        # distinct rep_index per repetition so each gets its own
                        # (deterministic) query-execution-cache entry / runtime. A fresh
                        # query_args per rep gives each its own req_id (and result file).
                        query_list.append(
                            replace(
                                query_entry,
                                query_args=format_args_element(
                                    str(query_id),
                                    placeholders,
                                    request_disambiguator=rep,
                                ),
                                rep_index=rep,
                            )
                        )

            query_batch_list.append(
                QueryBatch(
                    query_list=query_list,
                    benchmark=self.benchmark,
                    cli_call_args=cli_call_args_str,
                    extra_env=extra_env,
                    general_system_config=GeneralSystemConfig(
                        memory_limit_mb=self.memory_limit_mb,
                        num_threads=num_threads,
                        core_ids=core_ids,
                    ),
                    timeout_s=approx_timeout_for_validation(
                        scale_factor,
                        len(query_list),
                        subset_bytes=sum(
                            f.stat().st_size
                            for f in self.spec.subset_files(subset_dir)
                            if f.exists()
                        ),
                    ),
                    exec_settings=OLAPExecSettings(
                        scale_factor=scale_factor,
                        db_storage=self.db_storage,
                        parquet_dir=Path(parquet_dir),
                        disk_db_dir=storage_dir,
                        data_source=data_source,
                    ),
                )
            )

        return query_batch_list

    def _get_query_gen_fn(self):
        return self.spec.query_gen_factory(self)

    def get_placeholders_fn(self, do_not_cache: bool = False):
        return self.spec.placeholders_factory(self, do_not_cache)

    # --- registry-backed accessors (kept for external callers; resolve via spec) ---
    # ``str(benchmark)`` normalizes an enum member, a WorkloadId, or a plain name to the
    # registry key uniformly.
    @staticmethod
    def _dataset_tables(benchmark: "Workload | WorkloadId | str") -> list[str]:
        return list(get_workload_spec(str(benchmark)).tables)

    @staticmethod
    def _get_dataset_name(benchmark: "Workload | WorkloadId | str") -> str:
        return get_workload_spec(str(benchmark)).dataset_name

    @staticmethod
    def _get_dataset_schema(benchmark: "Workload | WorkloadId | str") -> str:
        return get_workload_spec(str(benchmark)).schema()

    def _get_sql_dict(self, benchmark: "Workload | WorkloadId | str"):
        return get_workload_spec(str(benchmark)).sql_dict()


def _resolve_query_subset(
    all_ids: list[str], requested: list[str] | None, benchmark: str
) -> list[str]:
    """Intersect a requested query subset with the workload's full catalog.

    Returns the full catalog when nothing is requested. Otherwise validates every
    requested id against the catalog (raising on unknown ids, to fail fast on typos
    instead of silently scaffolding the wrong set) and returns them in canonical
    catalog order, de-duplicated.
    """
    if not requested:
        return all_ids

    all_set = set(all_ids)
    unknown = [q for q in requested if q not in all_set]
    if unknown:
        raise ValueError(
            f"Requested query ids {unknown} are not valid for benchmark "
            f"'{benchmark}'. Valid ids: {all_ids}"
        )

    requested_set = set(requested)
    return [q for q in all_ids if q in requested_set]


def _get_all_query_ids(benchmark: str) -> list[str]:
    return list(get_workload_spec(benchmark).all_query_ids)


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Path:
    return cache_dir / f"{hash}.pkl"


class PlaceholdersCacheType:
    def __init__(self, placeholders: dict, hash_payload: str):
        self.placeholders = placeholders
        self.hash_payload = hash_payload


def approx_timeout_for_validation(
    scale_factor: float,
    num_executions: int,
    subset_bytes: int = 0,
) -> int:
    # approximate a timeout for validation based on data volume and number of queries. The scale
    # factor is a decent ~GB proxy for the classic ``sf<N>`` convention, but a DuckDB-native
    # fraction ladder (0.02..1.0) does not reflect the source's real size, so also derive a volume
    # estimate from the subset's on-disk bytes and use whichever is larger (never shrink a timeout).
    volume = max(scale_factor, subset_bytes / 1e9)
    timeout = (
        volume * num_executions * 2
    )  # 2 seconds per query at ~1GB as a rough estimate, can be adjusted as needed
    timeout = max(timeout, 120)  # at least 1 minute total timeout
    timeout = min(
        timeout, 1200
    )  # at most 20 minutes total timeout - for sf100 or similar this might take long

    # round up to minutes
    timeout = ((timeout + 59) // 60) * 60

    return int(timeout)
