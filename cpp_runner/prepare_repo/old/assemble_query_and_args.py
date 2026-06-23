from typing import Callable, List, Optional

from conversations.filenames import get_plan_filename


def build_query_and_args_files(
    benchmark_name: str,
    gen_placeholders_fn: Callable,
    query_list: List[str],
    query_impl_content: str,
    drop_os_caches_for_each_query: bool = False,
    storage_plan: Optional[str] = None,
    add_thread_pool_to_query_impl: bool = False,
    pin_to_core: int = 3,
    add_sample_trace_to_query_impl: bool = False,
) -> dict[str, str]:
    """Build query/args file contents without writing to disk.

    Args:
        query_impl_content: Contents of the query_impl.cpp template (from build_template_files).
    """
    benchmark_queries = get_sql_dict(benchmark_name)

    sql_template_list = [
        f"Query {q}:\n{benchmark_queries[f'Q{q}']}" for q in query_list
    ]
    qf_string = "\n\n".join(sql_template_list)

    args_str, _ = gen_args_str(
        query_list,
        use_fasttest_format=True,
        gen_placeholders_fn=gen_placeholders_fn,
    )

    result: dict[str, str] = {
        "queries.txt": qf_string,
        "args_parser.hpp": args_str,
        "query_impl.cpp": query_impl,
    }

    if storage_plan is not None:
        result[get_plan_filename("olap")] = storage_plan

    return result
