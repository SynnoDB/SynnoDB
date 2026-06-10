tables_lists = {
    "tpch": [
        "customer",
        "lineitem",
        "nation",
        "orders",
        "part",
        "partsupp",
        "region",
        "supplier",
    ],
    "ceb": [
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


def get_tables_for_benchmark(benchmark: str):
    if benchmark not in tables_lists:
        raise ValueError(f"Unknown benchmark {benchmark}")
    return tables_lists[benchmark]


def get_dataset_name(benchmark: str) -> str:
    if benchmark == "tpch":
        return "tpch"
    elif benchmark == "ceb":
        return "imdb"
    else:
        raise ValueError(f"Unknown benchmark {benchmark}")


def get_benchmark_schema(benchmark: str) -> str:
    if benchmark == "tpch":
        from workloads.dataset.gen_tpch.tpch_queries import tpc_h_schema

        return tpc_h_schema
    elif benchmark == "ceb":
        from workloads.dataset.gen_ceb.imdb_schema import imdb_schema

        return imdb_schema
    else:
        raise ValueError(f"Unknown benchmark {benchmark}")
