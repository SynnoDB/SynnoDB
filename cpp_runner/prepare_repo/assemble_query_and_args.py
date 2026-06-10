import string
from pathlib import Path
from typing import Callable, List, Optional

from workloads.dataset.gen_ceb.ceb_queries import ceb_templates
from workloads.dataset.gen_tpch.tpch_queries import tpc_h

_CPP_TYPE = {str: "std::string", int: "int", float: "float"}
_ARGS_PARSER_TEMPLATE = Path(__file__).parent / "templates" / "args_parser.hpp"


def get_sql_dict(benchmark_name: str):
    if benchmark_name == "tpch":
        benchmark_queries = tpc_h
    elif benchmark_name == "ceb":
        benchmark_queries = ceb_templates
    else:
        raise ValueError(f"Unknown benchmark name: {benchmark_name}")

    return benchmark_queries


def build_query_and_args_files(
    benchmark_name: str,
    gen_placeholders_fn: Callable,
    query_list: List[str],
    query_impl_content: str,
    drop_os_caches_for_each_query: bool = False,
    storage_plan: Optional[str] = None,
    add_thread_pool_to_query_impl: bool = False,
    pin_to_core: int = 3,
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

    impl_keyword = "// <<impl_fn_calls>>"
    assert impl_keyword in query_impl_content, (
        f"Keyword '{impl_keyword}' not found in query_impl.cpp template"
    )
    case_block, include_headers = gen_query_impl_ifelse_block(query_list)
    query_impl = query_impl_content.replace(impl_keyword, case_block)

    query_headers_kw = "// <<include_query_headers>>"
    assert query_headers_kw in query_impl, (
        f"Keyword '{query_headers_kw}' not found in query_impl.cpp template"
    )
    query_impl = query_impl.replace(query_headers_kw, include_headers)

    thread_pool_include_kw = "// <<thread_pool_include>>"
    thread_pool_placeholder_kw = "// <<get_thread_pool_placeholder>>"

    if add_thread_pool_to_query_impl:
        query_impl = query_impl.replace(
            thread_pool_include_kw, '#include "thread_pool.hpp"\n#include "trace.hpp"\n'
        )
        query_impl = query_impl.replace(
            thread_pool_placeholder_kw,
            """
// ── Shared thread pool (get_query_pool) ───────────────────────────────────────
// Initialized once at program start; warm-up dispatches a no-op parallel_for
// so all worker threads are alive and spinning before the first query arrives.
ThreadPool& get_query_pool() {
    static struct Holder {
        ThreadPool pool;
        Holder() {
            PROFILE_SCOPE("thread_pool_init");
            init_thread_pool(pool);
            pool.parallel_for([](int, int) {});  // warm-up
        }
    } h;
    return h.pool;
}
""",
        )
    else:
        query_impl = query_impl.replace(thread_pool_include_kw, "")
        query_impl = query_impl.replace(thread_pool_placeholder_kw, "")

    pin_thread_to_core_kw = "// <<pin_thread_to_core>>"
    assert pin_thread_to_core_kw in query_impl, (
        f"Keyword '{pin_thread_to_core_kw}' not found in query_impl.cpp template"
    )
    if add_thread_pool_to_query_impl:
        # multi-threading workers will be pinned. Not sure where to pin main thread to.
        query_impl = query_impl.replace(
            pin_thread_to_core_kw,
            """""",
        )
    else:
        query_impl = query_impl.replace(
            pin_thread_to_core_kw,
            f"""// Pin the process to CPU core {pin_to_core} for deterministic, low-noise performance measurements.
    pin_process_to_cpu({pin_to_core});""",
        )

    drop_caches_def_kw_start = "// <<drop_buffer_and_os_caches_def_start>>"
    drop_caches_def_kw_end = "// <<drop_buffer_and_os_caches_def_end>>"
    drop_caches_call_kw = "// <<drop_buffer_and_os_caches_call>>"
    buffer_pool_clear_kw = "// <<clear_buffer_pool_call>>"

    for kw in [
        drop_caches_def_kw_start,
        drop_caches_def_kw_end,
        drop_caches_call_kw,
        buffer_pool_clear_kw,
    ]:
        assert kw in query_impl, f"Keyword '{kw}' not found in query_impl.cpp template"

    if drop_os_caches_for_each_query:
        query_impl = query_impl.replace(
            drop_caches_call_kw, "\t\t\tdrop_buffer_and_os_caches(db);"
        )
        query_impl = query_impl.replace(buffer_pool_clear_kw, "\tdb->pool->clear();")

        # remove the start / end keywords since the function is now always defined
        query_impl = query_impl.replace(drop_caches_def_kw_start, "")
        query_impl = query_impl.replace(drop_caches_def_kw_end, "")

    else:
        query_impl = query_impl.replace(drop_caches_call_kw, "")
        query_impl = query_impl.replace(buffer_pool_clear_kw, "")

        # remove the entire function definition since it's not needed
        start_idx = query_impl.index(drop_caches_def_kw_start)
        end_idx = query_impl.index(drop_caches_def_kw_end) + len(drop_caches_def_kw_end)
        query_impl = query_impl[:start_idx] + query_impl[end_idx:]

    result: dict[str, str] = {
        "queries.txt": qf_string,
        "args_parser.hpp": args_str,
        "query_impl.cpp": query_impl,
    }

    if storage_plan is not None:
        result["storage_plan.txt"] = storage_plan

    return result


def gen_args_str(
    query_ids: List[str],
    gen_placeholders_fn: Callable,
    use_fasttest_format: bool = True,
):
    if not use_fasttest_format:
        raise Exception(
            "Non-fasttest format is outdated and no longer supported. E.g. this IN list parsing is not ported back."
        )

    query_blocks = "\n".join(
        _gen_query_block(q_id, gen_placeholders_fn(query_name=f"Q{q_id}"))
        for q_id in query_ids
    )
    out_str = string.Template(_ARGS_PARSER_TEMPLATE.read_text()).substitute(
        query_structs_and_parsers=query_blocks
    )
    return out_str, gen_parser_example_code(query_ids)


# --- per-query C++ generation helpers ---


def _field_decl(placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f"    std::vector<std::string> {placeholder};"
    return f"    {_CPP_TYPE[type(value)]} {placeholder};"


def _field_parser(q_id: str, placeholder: str, value) -> str:
    if isinstance(value, str) and value.startswith("("):
        return f"\targs.{placeholder} = parse_in_list(iss);"
    access = (
        f"std::quoted(args.{placeholder})"
        if isinstance(value, str)
        else f"args.{placeholder}"
    )
    return (
        f"\tif (!(iss >> {access})) {{\n"
        f'\t\tthrow std::runtime_error("Q{q_id}: failed to parse {placeholder}");\n'
        f"\t}}"
    )


def _gen_query_block(q_id: str, placeholders_dict: dict) -> str:
    qn = f"Q{q_id}"
    fields = "\n".join(_field_decl(p, v) for p, v in placeholders_dict.items())
    parsers = "\n".join(_field_parser(q_id, p, v) for p, v in placeholders_dict.items())
    return f"""\
//{qn}
struct {qn}Args {{
{fields}
}};

inline {qn}Args parse_{qn.lower()}(const QueryRequest& request) {{
    {qn}Args args;
    std::istringstream iss(request.line);

{parsers}

    return args;
}}
"""


def gen_parser_example_code(query_ids: List[str]) -> str:
    def case_block(i):
        return (
            f'//        case "{i}": {{\n'
            f"//            Q{i}Args args = parse_q{i}(req);\n"
            f"//            run_q{i}(db, args);\n"
            f"//            break;\n"
            f"//        }}"
        )

    first_cases = "\n".join(case_block(i) for i in query_ids[:2])
    last_case = case_block(query_ids[-1])
    return (
        "\n"
        "// Example code for how to use the parse functions together:\n"
        "//for (const auto& req : requests) {\n"
        "//    switch (req.query_id) {\n"
        f"{first_cases}\n"
        "//        ...\n"
        f"{last_case}\n"
        "//    }\n"
        "//}\n"
    )


def gen_query_impl_ifelse_block(query_ids: list[str]):
    """Emit the if/else dispatch chain that fills the <<impl_fn_calls>>
    placeholder in `query_impl.cpp`.

    The surrounding bookkeeping (try/catch, elapsed_ms, error capture, push_back)
    lives in the template, not here, so this function only produces the dispatch
    chain itself.  The placeholder sits at column 12 inside the template's
    try-block, so the first line emitted has no leading indent (the template
    provides it) and all subsequent lines carry their own 12-space indent.
    """
    indent = " " * 12
    body = " " * 16

    case_template = string.Template(
        '${prefix}${kw} (req.query_id == "${qid}") {\n'
        "${body}Q${qid}Args args = parse_q${qid}(req);\n"
        "${body}std::vector<std::vector<std::string>> rows;\n"
        "${body}auto start = std::chrono::steady_clock::now();\n"
        "${body}rows = run_q${qid}(db, args);\n"
        "${body}auto end = std::chrono::steady_clock::now();\n"
        "${body}elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();\n"
        '${body}const std::string filename = "result_" + req.req_id + ".csv";\n'
        "${body}write_csv(filename, rows);\n"
        "${indent}}"
    )

    cases = []
    for i, qid in enumerate(query_ids):
        cases.append(
            case_template.substitute(
                # First case starts at the placeholder's column (already indented).
                prefix="" if i == 0 else indent,
                kw="if" if i == 0 else "else if",
                qid=qid,
                body=body,
                indent=indent,
            )
        )
    cases.append(
        f"{indent}else {{\n"
        f'{body}throw std::runtime_error("Unsupported query id: " + req.query_id);\n'
        f"{indent}}}"
    )
    case_str = "\n".join(cases)

    include_query_impl_list = [f'#include "query{qid}.hpp"' for qid in query_ids]

    return case_str, "\n".join(include_query_impl_list)
