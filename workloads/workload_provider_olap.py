import enum

from workloads.dataset.gen_ceb.ceb_queries import ceb_templates
from workloads.dataset.gen_tpch.tpch_queries import tpc_h
from workloads.workload_provider import WorkloadProvider


class OLAPWorkload(enum.Enum):
    TPC_H = "tpch"
    CEB = "ceb"


class OLAPWorkloadProvider(WorkloadProvider):
    def __init__(self, benchmark: OLAPWorkload, **kwargs):
        self.benchmark = benchmark
        self.benchmark_name = benchmark.value
        self.query_ids = _get_all_query_ids(self.benchmark_name)
        self.sql_dict = self._get_sql_dict()

        super().__init__(**kwargs)

    def get_placeholders_fn(self):
        from workloads.dataset.query_gen_factory import get_placeholders_fn

        return get_placeholders_fn(
            benchmark=self.benchmark_name,
            do_not_cache=False,
            cache_dir=None,
        )

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
