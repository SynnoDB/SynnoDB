import functools
import logging
import os
import random
from dataclasses import dataclass, replace
from pathlib import Path

from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.utils import utils
from synnodb.utils.sql_utils import extract_order_by_columns
from synnodb.utils.utils import DBStorage
from synnodb.workloads.dataset.gen_ceb.ceb_queries import ceb_templates
from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h
from synnodb.workloads.workload_provider import (
<<<<<<< HEAD
    DEFAULT_NUM_INSTANTIATIONS,
=======
>>>>>>> main
    ExecSettings,
    GeneralSystemConfig,
    QueryBatch,
    QueryEntry,
    Workload,
<<<<<<< HEAD
    WorkloadId,
    WorkloadProvider,
    format_args_element,
)
from synnodb.workloads.workload_spec import (
    WorkloadSpec,
    get_workload_spec,
    register_workload,
)
=======
    WorkloadProvider,
    format_args_element,
)
>>>>>>> main

logger = logging.getLogger(__name__)


<<<<<<< HEAD
# Resolved lazily: importing this module (e.g. just to read the OLAPWorkload
# enum) must not require SYNNO_DATA_DIR to be configured. Stays None until the
# env is set; only the CEB runtime paths below actually need it.
_SYNNO_DATA_DIR_ENV = os.getenv("SYNNO_DATA_DIR", default=None)
SYNNO_DATA_DIR = Path(_SYNNO_DATA_DIR_ENV) if _SYNNO_DATA_DIR_ENV else None

CEB_QUERY_DIR = (
    SYNNO_DATA_DIR / "workloads" / "ceb" / "queries"
    if SYNNO_DATA_DIR is not None
    else None
)
=======
SYNNO_DATA_DIR = os.getenv("SYNNO_DATA_DIR", default=None)
assert SYNNO_DATA_DIR is not None, "SYNNO_DATA_DIR environment variable is not set"
SYNNO_DATA_DIR = Path(SYNNO_DATA_DIR)

CEB_QUERY_DIR = SYNNO_DATA_DIR / "workloads" / "ceb" / "queries"
>>>>>>> main

# Fraction of memory_budget_mb that goes to the generated engine's paged
# frame pool. The remainder is implicit headroom for mmap_col regions and
# other working memory; both are bounded together by RLIMIT_AS.
FRAME_POOL_SHARE = 0.60


class OLAPWorkload(Workload):
    TPCH = "tpch"
    CEB = "ceb"


@dataclass
class OLAPExecSettings(ExecSettings):
    scale_factor: float
    db_storage: DBStorage
    parquet_dir: Path
    disk_db_dir: Path | None


class OLAPWorkloadProvider(WorkloadProvider):
    def __init__(
        self,
        benchmark: OLAPWorkload,
        base_parquet_dir: Path,
        db_storage: DBStorage,
        bespoke_ssd_storage_dir: Path | None = None,
        query_cache_dir: Path | None = None,
<<<<<<< HEAD
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

        super().__init__(
            benchmark_name=self.benchmark.value,
            query_ids=scoped_query_ids,
            sql_dict=self.spec.sql_dict(),
=======
        **kwargs,
    ):
        self.benchmark = benchmark
        self.query_cache_dir = query_cache_dir
        self.base_parquet_dir = base_parquet_dir
        self.db_storage = db_storage
        self.dataset_tables = self._dataset_tables(self.benchmark)
        self.dataset_name = self._get_dataset_name(self.benchmark)
        self.dataset_schema = self._get_dataset_schema(self.benchmark)
        self.bespoke_ssd_storage_dir = bespoke_ssd_storage_dir

        # Scale factor used for BENCHMARK-mode runs. Conversations can override this
        # via set_benchmark_sf to drive perf/large-scale checks off the workload
        # provider (exec-config) rather than passing fixed scale factors around.
        self.benchmark_sf: float = 20 if self.benchmark == OLAPWorkload.TPCH else 5
        # Number of distinct parameter sets / repetitions emitted in BENCHMARK
        # mode. Defaults match the historical BENCHMARK behaviour but can be
        # overridden (e.g. by the benchmarker CLI's --instantiations/--repetitions).
        self.benchmark_instantiations: int = 1
        self.benchmark_repetitions: int = 3

        super().__init__(
            benchmark_name=self.benchmark.value,
            query_ids=_get_all_query_ids(self.benchmark.value),
            sql_dict=self._get_sql_dict(self.benchmark),
>>>>>>> main
            **kwargs,
        )

    def set_benchmark_sf(self, sf: float) -> None:
        """Override the scale factor emitted for BENCHMARK-mode workloads."""
        self.benchmark_sf = sf

<<<<<<< HEAD
    def set_num_instantiations(self, n: int) -> None:
        """Override the number of parameter instantiations per query in the sweep."""
        self.num_instantiations = n
=======
    def set_benchmark_instantiations(self, instantiations: int) -> None:
        """Override the number of distinct parameter sets emitted in BENCHMARK mode."""
        self.benchmark_instantiations = instantiations

    def set_benchmark_repetitions(self, repetitions: int) -> None:
        """Override the number of repetitions per query emitted in BENCHMARK mode."""
        self.benchmark_repetitions = repetitions
>>>>>>> main

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
<<<<<<< HEAD
            instantiations = self.num_instantiations
            repetitions = 1
            scale_factors = self.spec.scale_factors_for(run_mode)

        elif run_mode == RunToolMode.EXHAUSTIVE:
            instantiations = self.num_instantiations
            repetitions = 1
            scale_factors = self.spec.scale_factors_for(run_mode)
=======
            instantiations = 20
            repetitions = 1

            if self.benchmark == OLAPWorkload.TPCH:
                scale_factors = [1, 2]
            elif self.benchmark == OLAPWorkload.CEB:
                scale_factors = [0.25, 0.5]
            else:
                raise ValueError(f"Unknown benchmark: {self.benchmark}")

        elif run_mode == RunToolMode.EXHAUSTIVE:
            instantiations = 20
            repetitions = 1

            if self.benchmark == OLAPWorkload.TPCH:
                scale_factors: list[float] = [1, 2, 20]
            elif self.benchmark == OLAPWorkload.CEB:
                scale_factors: list[float] = [0.25, 0.5, 5]
            else:
                raise ValueError(f"Unknown benchmark: {self.benchmark}")
>>>>>>> main

            if scale_factors[-1] != self.benchmark_sf:
                scale_factors.append(self.benchmark_sf)

        elif run_mode == RunToolMode.BENCHMARK:
<<<<<<< HEAD
            instantiations = 1
            repetitions = 3
            # benchmark SF is configurable via set_benchmark_sf (exec-config driven)
=======
            # instantiations / repetitions / SF are configurable (exec-config driven)
            instantiations = self.benchmark_instantiations
            repetitions = self.benchmark_repetitions
>>>>>>> main
            scale_factors = [self.benchmark_sf]
        elif run_mode == RunToolMode.INGEST:
            instantiations = 3
            repetitions = 1
<<<<<<< HEAD
            scale_factors = self.spec.scale_factors_for(run_mode)
=======

            if self.benchmark == OLAPWorkload.TPCH:
                scale_factors = [20]
            elif self.benchmark == OLAPWorkload.CEB:
                scale_factors = [5]
>>>>>>> main

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

            # assemble parquet path where data is loaded from
            parquet_dir = (self.base_parquet_dir / f"sf{scale_factor}").as_posix() + "/"
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
<<<<<<< HEAD
                        query_args=format_args_element(str(query_id), placeholders),
=======
                        query_args="",
>>>>>>> main
                        placeholders=placeholders,
                        order_by_info=order_by_info,
                        num_reps=repetitions,
                    )

                    for rep in range(repetitions):
                        # distinct rep_index per repetition so each gets its own
                        # (deterministic) query-execution-cache entry / runtime
<<<<<<< HEAD
                        query_list.append(replace(query_entry, rep_index=rep))
=======
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
>>>>>>> main

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
                    ),
                )
            )

        return query_batch_list

    def _get_query_gen_fn(self):
<<<<<<< HEAD
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
=======
        # prepare query gen
        if self.benchmark == OLAPWorkload.TPCH:
            from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query

            gen_query_fn = gen_query
        elif self.benchmark == OLAPWorkload.CEB:
            from synnodb.workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

            gen_query_fn = functools.partial(
                gen_query_single_only, ceb_dir=CEB_QUERY_DIR
            )
        else:
            raise ValueError(f"Unknown benchmark: {self.benchmark}")

        return gen_query_fn

    def get_placeholders_fn(self, do_not_cache: bool = False):
        # prepare query gen
        gen_fn = None
        if self.benchmark == OLAPWorkload.TPCH:
            from synnodb.workloads.dataset.gen_tpch.gen_tpch_query import gen_query

            def gen_placeholder_tpch(**kwargs):
                # we only need the placeholders dict
                return gen_query(**kwargs)[2]

            gen_fn = gen_placeholder_tpch

        elif self.benchmark == OLAPWorkload.CEB:
            from synnodb.workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

            # load placeholders from disk

            def gen_placeholder_ceb(**kwargs):
                # check cache first
                hash_payload = {
                    "benchmark": "ceb",
                    "query_name": kwargs["query_name"],
                }
                stable_payload = utils.stable_json(hash_payload)

                hash = utils.sha256(stable_payload)

                if self.query_cache_dir is None:
                    cache_path = None
                else:
                    # create cache dir if needed
                    utils.create_dir_and_set_permissions(self.query_cache_dir)
                    cache_path = _cache_path_for_hash(self.query_cache_dir, hash)

                # check compile cache - replay compile result from cache if available
                if cache_path is not None and cache_path.exists():
                    cached: PlaceholdersCacheType | None = utils.load_pickle(
                        cache_path, PlaceholdersCacheType
                    )
                    assert cached is not None
                    logger.debug(f"Loaded placeholders from cache: {cache_path}")

                    return cached.placeholders

                # we only need the placeholders dict
                placeholders = gen_query_single_only(**kwargs, ceb_dir=CEB_QUERY_DIR)[2]

                # store output in cache
                if cache_path is not None and not do_not_cache:
                    utils.dump_pickle(
                        cache_path,
                        PlaceholdersCacheType(
                            placeholders=placeholders, hash_payload=stable_payload
                        ),
                        do_not_cache=do_not_cache,
                    )

                return placeholders

            gen_fn = gen_placeholder_ceb

        else:
            raise ValueError(f"Unknown benchmark: {self.benchmark}")

        return gen_fn

    @staticmethod
    def _dataset_tables(benchmark: OLAPWorkload) -> list[str]:
        tables_lists = {
            OLAPWorkload.TPCH: [
                "customer",
                "lineitem",
                "nation",
                "orders",
                "part",
                "partsupp",
                "region",
                "supplier",
            ],
            OLAPWorkload.CEB: [
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
            ],
        }
        if benchmark not in tables_lists:
            raise ValueError(f"Unknown benchmark {benchmark}")
        return tables_lists[benchmark]

    @staticmethod
    def _get_dataset_name(benchmark: OLAPWorkload) -> str:
        if benchmark == OLAPWorkload.TPCH:
            return "tpch"
        elif benchmark == OLAPWorkload.CEB:
            return "imdb"
        else:
            raise ValueError(f"Unknown benchmark {benchmark}")

    @staticmethod
    def _get_dataset_schema(benchmark: OLAPWorkload) -> str:
        if benchmark == OLAPWorkload.TPCH:
            from synnodb.workloads.dataset.gen_tpch.tpch_queries import tpc_h_schema

            return tpc_h_schema
        elif benchmark == OLAPWorkload.CEB:
            from synnodb.workloads.dataset.gen_ceb.imdb_schema import imdb_schema

            return imdb_schema
        else:
            raise ValueError(f"Unknown benchmark {benchmark}")

    def _get_sql_dict(self, benchmark: OLAPWorkload):
        if benchmark == OLAPWorkload.TPCH:
            return tpc_h
        elif benchmark == OLAPWorkload.CEB:
            return ceb_templates
        else:
            raise ValueError(f"Unknown benchmark: {benchmark}")


def _get_all_query_ids(benchmark: str) -> list[str]:
    if benchmark == "tpch":
        query_ids = [str(i) for i in range(1, 23)]
    elif benchmark == "ceb":
        query_ids = [
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
        ]
    else:
        raise ValueError(f"Unknown benchmark: {benchmark}")

    return query_ids
>>>>>>> main


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Path:
    return cache_dir / f"{hash}.pkl"


class PlaceholdersCacheType:
    def __init__(self, placeholders: dict, hash_payload: str):
        self.placeholders = placeholders
        self.hash_payload = hash_payload


<<<<<<< HEAD
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


def _ceb_schema() -> str:
    from synnodb.workloads.dataset.gen_ceb.imdb_schema import imdb_schema

    return imdb_schema


def _ceb_query_gen_factory(provider: "OLAPWorkloadProvider"):
    from synnodb.workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

    return functools.partial(gen_query_single_only, ceb_dir=CEB_QUERY_DIR)


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

        placeholders = gen_query_single_only(**kwargs, ceb_dir=CEB_QUERY_DIR)[2]

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
)

register_workload(TPCH_SPEC)
register_workload(CEB_SPEC)


=======
>>>>>>> main
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
