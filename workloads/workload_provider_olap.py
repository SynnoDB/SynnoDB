import enum
import functools
import logging
from pathlib import Path

from utils import utils
from workloads.dataset.gen_ceb.ceb_queries import ceb_templates
from workloads.dataset.gen_tpch.tpch_queries import tpc_h
from workloads.workload_provider import WorkloadProvider

logger = logging.getLogger(__name__)

CEB_DIR = Path("/mnt/labstore/bespoke_olap/datasets/ceb/imdb")
CEB_DIR = Path(__file__).parent.parent / "data" / "ceb" / "imdb"


class OLAPWorkload(enum.Enum):
    TPC_H = "tpch"
    CEB = "ceb"


class OLAPWorkloadProvider(WorkloadProvider):
    def __init__(
        self, benchmark: OLAPWorkload, query_cache_dir: Path | None = None, **kwargs
    ):
        self.benchmark = benchmark
        self.benchmark_name = benchmark.value
        self.query_ids = _get_all_query_ids(self.benchmark_name)
        self.sql_dict = self._get_sql_dict()
        self.query_cache_dir = query_cache_dir

        self.dataset_tables = self._dataset_tables()
        self.dataset_name = self._get_dataset_name()
        self.dataset_schema = self._get_dataset_schema()

        super().__init__(**kwargs)

    def get_query_gen_fn(self):
        # prepare query gen
        if self.benchmark == OLAPWorkload.TPC_H:
            from workloads.dataset.gen_tpch.gen_tpch_query import gen_query

            gen_query_fn = gen_query
        elif self.benchmark == OLAPWorkload.CEB:
            from workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

            gen_query_fn = functools.partial(gen_query_single_only, ceb_dir=CEB_DIR)
        else:
            raise ValueError(f"Unknown benchmark: {self.benchmark}")

        return gen_query_fn

    def get_placeholders_fn(self, do_not_cache: bool = False):
        # prepare query gen
        gen_fn = None
        if self.benchmark == OLAPWorkload.TPC_H:
            from workloads.dataset.gen_tpch.gen_tpch_query import gen_query

            def gen_placeholder_tpch(**kwargs):
                # we only need the placeholders dict
                return gen_query(**kwargs)[2]

            gen_fn = gen_placeholder_tpch

        elif self.benchmark == OLAPWorkload.CEB:
            from workloads.dataset.gen_ceb.gen_ceb_query import gen_query_single_only

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
                placeholders = gen_query_single_only(**kwargs, ceb_dir=CEB_DIR)[2]

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

    def _dataset_tables(self) -> list[str]:
        tables_lists = {
            OLAPWorkload.TPC_H: [
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
        if self.benchmark not in tables_lists:
            raise ValueError(f"Unknown benchmark {self.benchmark}")
        return tables_lists[self.benchmark]

    def _get_dataset_name(self) -> str:
        if self.benchmark == OLAPWorkload.TPC_H:
            return "tpch"
        elif self.benchmark == OLAPWorkload.CEB:
            return "imdb"
        else:
            raise ValueError(f"Unknown benchmark {self.benchmark}")

    def _get_dataset_schema(self) -> str:
        if self.benchmark == OLAPWorkload.TPC_H:
            from workloads.dataset.gen_tpch.tpch_queries import tpc_h_schema

            return tpc_h_schema
        elif self.benchmark == OLAPWorkload.CEB:
            from workloads.dataset.gen_ceb.imdb_schema import imdb_schema

            return imdb_schema
        else:
            raise ValueError(f"Unknown benchmark {self.benchmark}")

    def _get_sql_dict(self):
        if self.benchmark == OLAPWorkload.TPC_H:
            return tpc_h
        elif self.benchmark == OLAPWorkload.CEB:
            return ceb_templates
        else:
            raise ValueError(f"Unknown benchmark: {self.benchmark}")


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


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Path:
    return cache_dir / f"{hash}.pkl"


class PlaceholdersCacheType:
    def __init__(self, placeholders: dict, hash_payload: str):
        self.placeholders = placeholders
        self.hash_payload = hash_payload
