import functools
import logging
import os
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from synnodb import settings
from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils import utils
from synnodb.utils.gen_common import _parse_ceb_fuzzy_range
from synnodb.utils.sql_utils import extract_order_by_columns
from synnodb.utils.utils import (
    DataSource,
    DBStorage,
    is_persistent_storage,
)
from synnodb.workloads.system_factory import System
from synnodb.workloads.dataset.gen_ceb.ceb_queries import ceb_templates
from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h
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
from synnodb.ram_check import RamCheck
from synnodb.workloads.workload_spec import (
    WorkloadSpec,
    find_sf_dir,
    get_workload_spec,
    register_workload,
)

logger = logging.getLogger(__name__)


def _ceb_query_dir() -> Path:
    """CEB query-template directory, resolved lazily so importing this module
    needs no SYNNO_DATA_DIR (config is resolved on first use via settings)."""
    return settings.get_data_dir() / "workloads" / "ceb" / "queries"


# Fraction of memory_budget_mb that goes to the generated engine's paged
# frame pool. The remainder is implicit headroom for mmap_col regions and
# other working memory; both are bounded together by RLIMIT_AS.
FRAME_POOL_SHARE = 0.60


class OLAPWorkload(Workload):
    TPCH = "tpch"
    CEB = "ceb"


def allowed_data_sources(system: System, db_storage: DBStorage) -> set[DataSource]:
    """Data sources a given system can use on a given storage medium.

    DuckDB reads either flat (its native materialized tables) or, on disk, parquet views. The
    bespoke engine additionally has its own on-disk storage plan. In-memory rules out anything
    that needs disk (parquet views, the bespoke storage plan) - but the engine can still load
    flat in memory. Returns an empty set for systems whose data source is not modelled here.
    """
    persistent = is_persistent_storage(db_storage)
    if system == System.DUCKDB:
        return (
            {DataSource.FLAT, DataSource.PARQUET} if persistent else {DataSource.FLAT}
        )
    if system == System.BESPOKE:
        return (
            {DataSource.FLAT, DataSource.BESPOKE, DataSource.PARQUET}
            if persistent
            else {DataSource.FLAT, DataSource.BESPOKE}
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
        benchmark: OLAPWorkload,
        base_parquet_dir: Path,
        db_storage: DBStorage,
        bespoke_ssd_storage_dir: Path | None = None,
        query_cache_dir: Path | None = None,
        query_ids: list[str] | None = None,
        num_instantiations: int = DEFAULT_NUM_INSTANTIATIONS,
        **kwargs,
    ):
        # Accept either a built-in OLAPWorkload enum member or a registered workload
        # name / WorkloadId (bring-your-own). Normalize plain strings so `.value`
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

        extra_env = dict()
        # assemble storage dir path

        query_batch_list = []
        rnd = random.Random(42)
        for scale_factor in scale_factors:
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

            data_source = (
                DataSource.BESPOKE if storage_dir is not None else DataSource.FLAT
            )

            # assemble parquet path where data is loaded from - resolve the tier directory
            # under the parquet root (sampling-ratio ``ratio<f>`` or legacy ``sf<N>``)
            tier_dir = find_sf_dir(self.base_parquet_dir, scale_factor)
            if tier_dir is None:
                raise FileNotFoundError(
                    f"No tier directory for scale/ratio {scale_factor:g} under "
                    f"{self.base_parquet_dir} for workload {self.spec.name!r}."
                )
            parquet_dir = tier_dir.as_posix() + "/"
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
                        scale_factor, len(query_list)
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
    @staticmethod
    def _dataset_tables(benchmark: OLAPWorkload) -> list[str]:
        return list(get_workload_spec(benchmark.value).tables)

    @staticmethod
    def _get_dataset_name(benchmark: OLAPWorkload) -> str:
        return get_workload_spec(benchmark.value).dataset_name

    @staticmethod
    def _get_dataset_schema(benchmark: OLAPWorkload) -> str:
        return get_workload_spec(benchmark.value).schema()

    def _get_sql_dict(self, benchmark: OLAPWorkload):
        return get_workload_spec(benchmark.value).sql_dict()


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


# ============================================================================
# Built-in workloads (TPC-H, CEB) expressed as data. Adding a new workload means
# building + registering a WorkloadSpec — not editing the provider. The per-query
# parameter generators live in workloads/dataset/gen_{tpch,ceb} and are referenced
# here lazily so importing this module stays cheap.
# ============================================================================


def _tpch_schema() -> str:
    from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h_schema

    return tpc_h_schema


def _tpch_query_gen_factory(provider: "OLAPWorkloadProvider"):
    from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query

    return gen_query


def _tpch_placeholders_factory(
    provider: "OLAPWorkloadProvider", do_not_cache: bool = False
):
    from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query

    def gen_placeholder_tpch(**kwargs):
        # we only need the placeholders dict
        return gen_query(**kwargs)[2]

    return gen_placeholder_tpch


def _tpch_param_space_factory(provider: "OLAPWorkloadProvider | None"):
    """Per-query typed value-space for built-in TPC-H, from the declarative spec table.

    Used for live-UI widget metadata (slider/dropdown/date-picker). The run-time sampler
    stays ``gen_query`` (see ``_tpch_query_gen_factory``), so TPC-H run behavior is unchanged.
    """
    from synnodb.workloads.dataset.gen_tpch.tpch_param_specs import TPCH_PARAM_SPECS
    from synnodb.workloads.query_params import parse_param_space

    def get(query_name: str):
        qid = query_name[1:] if query_name.startswith("Q") else query_name
        section = TPCH_PARAM_SPECS.get(qid)
        if section is None:
            return None
        return parse_param_space(
            section.get("params"), section.get("param_groups"), tpc_h[f"Q{qid}"]
        )

    return get


def _ceb_schema() -> str:
    from synnodb.workloads.dataset.gen_ceb.imdb_schema import imdb_schema

    return imdb_schema


def _ceb_query_gen_factory(provider: "OLAPWorkloadProvider"):
    from synnodb.workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

    return functools.partial(gen_query_single_only, ceb_dir=_ceb_query_dir())


def _ceb_placeholders_factory(
    provider: "OLAPWorkloadProvider", do_not_cache: bool = False
):
    from synnodb.workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

    def gen_placeholder_ceb(**kwargs):
        # placeholders are loaded from disk; cache them per query to avoid re-reading
        hash_payload = {"benchmark": "ceb", "query_name": kwargs["query_name"]}
        stable_payload = utils.stable_json(hash_payload)
        hash = utils.sha256(stable_payload)

        if provider.query_cache_dir is None:
            cache_path = None
        else:
            utils.create_dir_and_set_permissions(provider.query_cache_dir)
            cache_path = _cache_path_for_hash(provider.query_cache_dir, hash)

        if cache_path is not None and cache_path.exists():
            cached: PlaceholdersCacheType | None = utils.load_pickle(
                cache_path, PlaceholdersCacheType
            )
            assert cached is not None
            logger.debug(f"Loaded placeholders from cache: {cache_path}")
            return cached.placeholders

        placeholders = gen_query_single_only(**kwargs, ceb_dir=_ceb_query_dir())[2]

        if cache_path is not None and not do_not_cache:
            utils.dump_pickle(
                cache_path,
                PlaceholdersCacheType(
                    placeholders=placeholders, hash_payload=stable_payload
                ),
                do_not_cache=do_not_cache,
            )

        return placeholders

    return gen_placeholder_ceb


TPCH_SPEC = WorkloadSpec(
    name="tpch",
    tables=(
        "customer",
        "lineitem",
        "nation",
        "orders",
        "part",
        "partsupp",
        "region",
        "supplier",
    ),
    dataset_name="tpch",
    all_query_ids=tuple(str(i) for i in range(1, 23)),
    benchmark_sf=20,
    fast_check_sfs=(1, 2),
    exhaustive_sfs=(1, 2, 20),
    ingest_sfs=(20,),
    example_query="Q42",
    example_query_params="42",
    schema_example_table="lineitem",
    sql_dict_factory=lambda: tpc_h,
    schema_factory=_tpch_schema,
    query_gen_factory=_tpch_query_gen_factory,
    placeholders_factory=_tpch_placeholders_factory,
    param_space_factory=_tpch_param_space_factory,
    large_check_sf=100,
)

CEB_SPEC = WorkloadSpec(
    name="ceb",
    tables=(
        "aka_name",
        "aka_title",
        "cast_info",
        "char_name",
        "comp_cast_type",
        "company_name",
        "company_type",
        "complete_cast",
        "info_type",
        "keyword",
        "kind_type",
        "link_type",
        "movie_companies",
        "movie_info",
        "movie_info_idx",
        "movie_keyword",
        "movie_link",
        "name",
        "person_info",
        "role_type",
        "title",
    ),
    dataset_name="imdb",
    all_query_ids=(
        "1a",
        "2a",
        "2b",
        "2c",
        "3a",
        "3b",
        "4a",
        "5a",
        "6a",
        "7a",
        "8a",
        "9a",
        "9b",
        "10a",
        "11a",
        "11b",
    ),
    benchmark_sf=5,
    fast_check_sfs=(0.25, 0.5),
    exhaustive_sfs=(0.25, 0.5, 5),
    ingest_sfs=(5,),
    example_query="Q42a",
    example_query_params="42a",
    schema_example_table="title",
    sql_dict_factory=lambda: ceb_templates,
    schema_factory=_ceb_schema,
    query_gen_factory=_ceb_query_gen_factory,
    placeholders_factory=_ceb_placeholders_factory,
    # CEB's dataset was regenerated; bump to invalidate stale cache entries. (Previously
    # a dead `if args.benchmark == "ceb"` branch in main.py that never fired because the
    # benchmark is an enum, not a str — folding it onto the spec also fixes that bug.)
    dataset_version="3",
    large_check_sf=10,
    query_range_expander=_parse_ceb_fuzzy_range,
)

register_workload(TPCH_SPEC)
register_workload(CEB_SPEC)


def approx_timeout_for_validation(
    scale_factor: float,
    num_executions: int,
) -> int:
    # approximate a timeout for validation based on scale factor and number of queries
    timeout = (
        scale_factor * num_executions * 2
    )  # 2 seconds per query with sf=1 as a rough estimate, can be adjusted as needed
    timeout = max(timeout, 120)  # at least 1 minute total timeout
    timeout = min(
        timeout, 1200
    )  # at most 20 minutes total timeout - for sf100 or similar this might take long

    # round up to minutes
    timeout = ((timeout + 59) // 60) * 60

    return int(timeout)
